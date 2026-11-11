#!/usr/bin/env bash
# Rotate one Keycloak client secret and reseal it, without ever printing it.
#
# WHY A SEPARATE SCRIPT FROM rotate_platform_secrets.sh. That one is a one-shot
# migration: it walks a fixed list of services, commits, opens a PR and merges,
# and each phase skips work it already did. It is the right tool for the sweep
# it was written for and the wrong tool for "rotate exactly this one client
# again", which is what is needed whenever a single secret is exposed.
#
# WHY NOT keycloak_common.sh's kc_print_secret. Because it prints. That habit is
# how grafana's live client secret ended up in a terminal transcript on
# 2026-11-11. Nothing here echoes the value: it moves from Keycloak into a
# file, into kubeseal, and nowhere else.
#
# IT DOES NOT APPLY TO THE CLUSTER, on purpose. Every SealedSecret under
# platform-gitops/manifests/ is served by an ArgoCD Application with
# selfHeal: true, which reverts a hand-applied one within about a second. That
# is how a database role and its Secret came apart earlier the same day. Git is
# the delivery path.
#
# EXPECT A SHORT AUTH OUTAGE. Regenerating a client secret invalidates the old
# one the instant it happens, while the running workload keeps using the old
# value until the merge reaches it. Minimise the window by merging promptly, and
# make sure the workload actually reloads: a secret consumed through env vars
# needs a pod restart, which is what the Reloader annotation is for.
#
# Usage:
#   scripts/rotate_keycloak_client_secret.sh \
#     --client grafana --namespace airflow \
#     --secret grafana-oauth-sealed --key GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

CLIENT= NS= SECRET= KEY= MANIFEST= REALM=data-platform
KC_URL="${KC_URL:-https://auth.sentpul.click}"
CTX="${KC_KUBE_CONTEXT:-sentpul}"

while [ $# -gt 0 ]; do
  case "$1" in
    --client) CLIENT="$2"; shift 2;;
    --namespace) NS="$2"; shift 2;;
    --secret) SECRET="$2"; shift 2;;
    --key) KEY="$2"; shift 2;;
    --manifest) MANIFEST="$2"; shift 2;;
    --realm) REALM="$2"; shift 2;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

C_G=$'\e[32m'; C_Y=$'\e[33m'; C_0=$'\e[0m'
ok(){ printf '  %sok%s %s\n' "$C_G" "$C_0" "$*"; }
die(){ printf '\n%sFAILED:%s %s\n' $'\e[31m' "$C_0" "$*" >&2; exit 1; }

for v in CLIENT NS SECRET KEY; do
  [ -n "${!v}" ] || die "--${v,,} is required (try --help)"
done
# Manifest naming in this repo is not uniform: a Secret already called
# "<x>-sealed" is declared in "<x>-sealed-secret.yaml", while a plainly named
# one gets "<x>-sealed-secret.yaml" too. Deriving it blindly produced
# "grafana-oauth-sealed-sealed-secret.yaml", which the existence check below
# caught before anything was regenerated.
if [ -z "$MANIFEST" ]; then
  case "$SECRET" in
    *-sealed) MANIFEST="platform-gitops/manifests/${SECRET}-secret.yaml";;
    *)        MANIFEST="platform-gitops/manifests/${SECRET}-sealed-secret.yaml";;
  esac
fi
[ -f "$MANIFEST" ] || die "$MANIFEST does not exist, so this is a new credential rather than a rotation"

for t in kubectl kubeseal curl python3; do command -v $t >/dev/null || die "missing tool: $t"; done
k(){ kubectl --context "$CTX" "$@"; }
k get nodes >/dev/null 2>&1 || die "kube context '$CTX' unreachable"

d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT; umask 077

k -n keycloak get secret keycloak-admin -o jsonpath='{.data.admin-password}' | base64 -d > "$d/adminpw"
[ -s "$d/adminpw" ] || die "could not read keycloak/keycloak-admin"

TOKEN="$(curl -sf -d client_id=admin-cli -d grant_type=password -d username=admin \
  --data-urlencode "password@$d/adminpw" \
  "$KC_URL/realms/master/protocol/openid-connect/token" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
[ -n "$TOKEN" ] || die "Keycloak admin login failed"
ok "authenticated to Keycloak"

UUID="$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients?clientId=$CLIENT" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d else "")')"
[ -n "$UUID" ] || die "no client '$CLIENT' in realm '$REALM'"
ok "found client $CLIENT"

# Let Keycloak generate the value. Its RNG is the one the realm already trusts,
# and a value this script invents would have to be transported to Keycloak,
# which is one more place it could leak.
curl -sf -X POST -H "Authorization: Bearer $TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients/$UUID/client-secret" \
  | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["value"])' > "$d/new"
[ -s "$d/new" ] || die "Keycloak did not return a new secret"
ok "regenerated, $(wc -c < "$d/new") chars, sha $(sha256sum < "$d/new" | cut -c1-10)"

# Read it back independently. If what Keycloak now stores is not what we are
# about to seal, the workload would authenticate with a value nothing holds.
curl -sf -H "Authorization: Bearer $TOKEN" \
  "$KC_URL/admin/realms/$REALM/clients/$UUID/client-secret" \
  | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["value"])' > "$d/readback"
[ "$(sha256sum < "$d/new" | cut -d' ' -f1)" = "$(sha256sum < "$d/readback" | cut -d' ' -f1)" ] \
  || die "Keycloak stores a different value than it returned, do NOT commit"
ok "Keycloak stores exactly what will be sealed"

# Every other key in the live Secret is carried across unchanged, so a Secret
# holding more than this one credential does not lose the rest.
args=( --from-file="$KEY=$d/new" )
carried=0
for existing in $(k -n "$NS" get secret "$SECRET" -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}' 2>/dev/null); do
  [ "$existing" = "$KEY" ] && continue
  k -n "$NS" get secret "$SECRET" -o go-template="{{index .data \"$existing\" | base64decode}}" > "$d/carry-$existing"
  args+=( --from-file="$existing=$d/carry-$existing" )
  carried=$((carried+1))
done
[ "$carried" -gt 0 ] && ok "carrying $carried other key(s) in $SECRET across unchanged"

k create secret generic "$SECRET" -n "$NS" "${args[@]}" --dry-run=client -o yaml \
  | k annotate --local -f - sealedsecrets.bitnami.com/managed=true -o yaml \
  | kubeseal --context "$CTX" --format yaml > "$d/s.yaml"
grep -q 'kind: SealedSecret' "$d/s.yaml" || die "kubeseal produced no SealedSecret"

{ echo "# Generated by scripts/rotate_keycloak_client_secret.sh on $(date -Is)."
  echo "# Rotated the '$CLIENT' Keycloak client secret in realm '$REALM'."
  echo "#"
  echo "# Ciphertext is safe to commit: only the in-cluster sealed-secrets"
  echo "# controller holds the private key. The old secret stopped working the"
  echo "# moment Keycloak regenerated it, so merge this promptly and make sure"
  echo "# the workload reloads, or it keeps presenting a dead credential."
  grep -v '^\s*creationTimestamp' "$d/s.yaml"; } > "$MANIFEST"
ok "wrote $MANIFEST"

printf '\n%sNot applied to the cluster on purpose.%s ArgoCD selfHeal reverts a hand\n' "$C_Y" "$C_0"
printf 'applied SealedSecret to git ciphertext within a second. Commit and merge:\n\n'
printf '  git add %s && git commit\n\n' "$MANIFEST"
printf 'The old secret is already dead, so until that merge lands and the workload\n'
printf 'restarts, logins through the %s client will fail.\n' "$CLIENT"
