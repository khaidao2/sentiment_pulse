#!/usr/bin/env bash
# Initialise Vault once, seal its unseal keys so the unsealer can read them, and
# configure the Kubernetes auth method that External Secrets Operator logs in
# with.
#
# RUN THIS ONCE, BY HAND. `vault operator init` is the one step in this platform
# that creates material nothing can recreate. If the unseal keys and the root
# token are both lost, every credential stored in Vault is unrecoverable. No
# amount of GitOps fixes that, which is why this is a script an operator runs
# with its output in front of them and not a Job that runs itself.
#
# WHAT IT REFUSES TO DO. It will not re-initialise an already-initialised Vault,
# because that would orphan the existing Raft data behind keys nobody holds. Run
# it twice and the second run reports the state and exits 0.
#
# THE TRADEOFF YOU ARE ACCEPTING. Three of the five unseal keys are sealed into
# vault/vault-unseal-keys so the unsealer can bring Vault back after a reboot
# without a human. Those keys are then protected by the sealed-secrets
# controller key, the same key that already protects every other credential
# here. So Vault buys rotation and an audit log of credential reads. It does not
# buy stronger secrecy at rest than this platform already had. Manual unseal
# would, at the cost of the platform being down after every reboot.
#
# KEEP ALL FIVE KEYS AND THE ROOT TOKEN OFFLINE. The sealed copy covers a
# reboot. It does not cover losing the sealed-secrets controller key, and that
# is the case where your offline copy is the only way back.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
MF=platform-gitops/manifests
OUT="$MF/vault-unseal-keys-sealed-secret.yaml"
INV=platform-gitops/secrets-inventory.tsv
NS=vault
# A sealed Vault fails its readiness probe, so the `vault` Service has no
# endpoints and only the headless one resolves the pod. Talking to the Service
# here is the mistake that makes this script hang on a Vault it could have
# reached.
POD_ADDR="http://vault-0.vault-internal.${NS}.svc.cluster.local:8200"

k(){ kubectl --context sentpul "$@"; }
# kubeseal fetches the sealing certificate from the controller, so it needs the
# same context kubectl is pinned to. Sealing against the wrong cluster produces
# ciphertext this controller can never decrypt, with no error at seal time.
seal_stdin(){ kubeseal --context sentpul --format yaml; }
# Run a vault CLI command inside the server pod, against the pod's own address.
v(){ k -n "$NS" exec -i vault-0 -- env VAULT_ADDR="$POD_ADDR" vault "$@"; }

C_G=$'\e[32m'; C_Y=$'\e[33m'; C_0=$'\e[0m'
ok(){ printf '  %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn(){ printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die(){ printf '\n%sFAILED:%s %s\n' $'\e[31m' "$C_0" "$*" >&2; exit 1; }

for t in kubectl kubeseal python3; do command -v $t >/dev/null || die "missing tool: $t"; done
k get nodes >/dev/null 2>&1 || die "kube context 'sentpul' unreachable"
k -n "$NS" get pod vault-0 >/dev/null 2>&1 \
  || die "vault-0 is not in namespace $NS. Merge the vault Application first and let ArgoCD sync it."

d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT; umask 077

# ---------------------------------------------------------------- already up? --
# `vault status` exits 0 unsealed, 2 sealed, 1 unreachable. Initialised state
# comes from the status text rather than the exit code, which does not
# distinguish "sealed and initialised" from "sealed and empty".
set +e
status_json="$(v status -format=json 2>/dev/null)"
set -e
if printf %s "$status_json" | python3 -c "
import json,sys
raw=sys.stdin.read().strip()
sys.exit(0 if raw and json.loads(raw).get('initialized') else 1)
" 2>/dev/null; then
  sealed="$(printf %s "$status_json" | python3 -c "
import json,sys
print('yes' if json.load(sys.stdin).get('sealed') else 'no')
")"
  ok "vault is already initialised (sealed: $sealed)"
  [ "$sealed" = no ] || warn "it is sealed. The unsealer should open it within 15s. Check: kubectl -n $NS logs deploy/vault-unsealer"
  echo
  echo "Nothing to do. This script never re-initialises an initialised Vault,"
  echo "because that would orphan the Raft data behind keys nobody holds."
  exit 0
fi

# -------------------------------------------------------------------- init it --
echo "Initialising Vault with 5 key shares and a threshold of 3."
v operator init -key-shares=5 -key-threshold=3 -format=json > "$d/init.json" \
  || die "vault operator init failed"
[ -s "$d/init.json" ] || die "vault operator init produced no output"

python3 - "$d" <<'PY'
import json, pathlib, sys
d = pathlib.Path(sys.argv[1])
init = json.loads(d.joinpath("init.json").read_text())
keys = init["unseal_keys_b64"]
if len(keys) != 5:
    raise SystemExit(f"expected 5 unseal keys, got {len(keys)}")
# Three keys go to the unsealer, which is the threshold. No trailing newline:
# the bytes of each file are the key.
for i, key in enumerate(keys[:3], start=1):
    d.joinpath(f"key{i}").write_text(key)
d.joinpath("root_token").write_text(init["root_token"])
d.joinpath("all_keys.txt").write_text("\n".join(keys) + "\n")
PY
ok "initialised, and the threshold keys are staged for sealing"

# --------------------------------------------------------- seal the keys away --
k create secret generic vault-unseal-keys -n "$NS" \
    --from-file="key1=$d/key1" --from-file="key2=$d/key2" --from-file="key3=$d/key3" \
    --dry-run=client -o yaml \
  | seal_stdin > "$d/s.yaml"
grep -q 'kind: SealedSecret' "$d/s.yaml" || die "kubeseal produced no SealedSecret"

{ echo "# Generated by scripts/vault_bootstrap.sh on $(date -Is)."
  echo "# Three of Vault's five unseal keys, which is its threshold. The"
  echo "# vault-unsealer Deployment mounts them so Vault reopens after a reboot"
  echo "# without a human. Only the in-cluster sealed-secrets controller can"
  echo "# decrypt this, so committing the ciphertext is safe."
  echo "#"
  echo "# This file is NOT a backup. Losing the controller key makes it useless,"
  echo "# and the operator's offline copy of all five keys is the way back."
  grep -v '^\s*creationTimestamp' "$d/s.yaml"; } > "$OUT"
k apply -f "$OUT" >/dev/null
for _ in $(seq 1 30); do
  k -n "$NS" get secret vault-unseal-keys >/dev/null 2>&1 && break
  sleep 1
done
k -n "$NS" get secret vault-unseal-keys >/dev/null 2>&1 \
  || die "vault/vault-unseal-keys never materialised, so do NOT commit $OUT"
ok "sealed, applied and materialised: vault/vault-unseal-keys -> $OUT"

# ------------------------------------------------------------------- unseal it --
for i in 1 2 3; do
  v operator unseal "$(cat "$d/key$i")" >/dev/null 2>&1 || true
done
set +e
v status >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -eq 0 ] || die "vault is still sealed after three keys. Check: kubectl -n $NS logs vault-0"
ok "vault is unsealed"

# --------------------------------------------------- configure for ESO access --
# The root token is used here and nowhere else. It is never written into the
# repo and never stored in the cluster. It does reach this kubectl invocation's
# argv, so the script tells you to revoke it at the end.
ROOT="$(cat "$d/root_token")"
vr(){ k -n "$NS" exec -i vault-0 -- env VAULT_ADDR="$POD_ADDR" VAULT_TOKEN="$ROOT" vault "$@"; }

vr secrets enable -path=platform kv-v2 >/dev/null 2>&1 \
  && ok "enabled the kv-v2 mount at platform/" \
  || ok "kv-v2 mount at platform/ already enabled"

vr auth enable kubernetes >/dev/null 2>&1 \
  && ok "enabled the kubernetes auth method" \
  || ok "kubernetes auth method already enabled"

# The token reviewer is the pod's own projected service-account token, so this
# needs no static credential and nothing to rotate. authDelegator in the Helm
# values is what lets it call TokenReview.
#
# The literal in-cluster API address, not $KUBERNETES_SERVICE_HOST. `kubectl
# exec` passes argv with no shell in the pod, so the variable would be stored
# verbatim and the auth method would point at a host that does not resolve.
vr write auth/kubernetes/config \
    kubernetes_host="https://kubernetes.default.svc:443" >/dev/null \
  && ok "pointed the kubernetes auth method at the in-cluster API"

printf 'path "platform/data/*" { capabilities = ["read"] }\npath "platform/metadata/*" { capabilities = ["read","list"] }\n' \
  | k -n "$NS" exec -i vault-0 -- env VAULT_ADDR="$POD_ADDR" VAULT_TOKEN="$ROOT" \
      vault policy write eso-read - >/dev/null \
  && ok "wrote the eso-read policy, which is read-only on platform/"

vr write auth/kubernetes/role/eso \
    bound_service_account_names=external-secrets \
    bound_service_account_namespaces=external-secrets \
    policies=eso-read ttl=1h >/dev/null \
  && ok "bound the eso role to the external-secrets service account"

# ------------------------------------------------------------------ hand back --
echo
printf '%sSTORE THESE OFFLINE NOW. They are shown once and exist nowhere else.%s\n' "$C_Y" "$C_0"
echo
echo "  Unseal keys, all five:"
sed 's/^/    /' "$d/all_keys.txt"
echo "  Root token:"
echo "    $(cat "$d/root_token")"
echo
printf '%sThen:%s\n' "$C_Y" "$C_0"
echo "  1. Put them in your password manager. The sealed copy in git covers a"
echo "     reboot, not the loss of the sealed-secrets controller key."
echo "  2. Commit $OUT."
echo "  3. Add this row to $INV, because the gate fails on a credential with no row:"
printf '       vault\tvault-unseal-keys\tpersistent\tsealed\t\n'
echo "  4. Revoke the root token once you have stored it:"
echo "       kubectl --context sentpul -n $NS exec -i vault-0 -- env VAULT_ADDR=$POD_ADDR VAULT_TOKEN=<root> vault token revoke -self"
echo "     Vault's own docs call a long-lived root token the thing to avoid, and"
echo "     'vault operator generate-root' with three keys is the way back in."
