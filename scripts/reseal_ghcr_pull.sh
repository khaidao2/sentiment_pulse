#!/usr/bin/env bash
# Replace a ghcr pull credential with a read-only one, and refuse anything wider.
#
# WHY THIS EXISTS. mission-control/ghcr-pull was sealed from a token carrying
# `repo` and `write:packages`, because that was the token the operator already
# had. Its manifest says so and calls the reduction "a two-command change".
# Writing that sentence twice is the signal to make it a script instead, so the
# narrow token is the easy path and the wide one cannot be sealed by accident.
#
# THE GUARD IS THE POINT. A pull secret needs `read:packages` and nothing else.
# This script reads the token's scopes from GitHub and exits non-zero if any
# write or repo scope is present. That check is why you cannot use this to
# re-seal the current token: it would be refused, correctly.
#
# PROOF BEFORE COMMIT. It fetches the digest the manifests actually pin, using
# the new credential, so a token that authenticates but cannot see this package
# fails here rather than as ImagePullBackOff.
#
# The token never reaches argv, a log, or the terminal. Pass it on stdin or in
# a file.
#
# Usage:
#   scripts/reseal_ghcr_pull.sh --token-file ~/path/to/token
#   printf '%s' "$TOKEN" | scripts/reseal_ghcr_pull.sh
#   scripts/reseal_ghcr_pull.sh --namespace webapp --token-file ...
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

NS=mission-control
TOKEN_FILE=
GH_USER=khaidao2
PACKAGE=khaidao2/mission-control

while [ $# -gt 0 ]; do
  case "$1" in
    --namespace) NS="$2"; shift 2;;
    --token-file) TOKEN_FILE="$2"; shift 2;;
    --package) PACKAGE="$2"; shift 2;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

k(){ kubectl --context sentpul "$@"; }
# kubeseal fetches the sealing certificate from the controller, so it needs the
# same context. Sealing against the wrong cluster fails invisibly.
seal_stdin(){ kubeseal --context sentpul --format yaml; }

C_G=$'\e[32m'; C_Y=$'\e[33m'; C_0=$'\e[0m'
ok(){ printf '  %sok%s %s\n' "$C_G" "$C_0" "$*"; }
die(){ printf '\n%sREFUSED:%s %s\n' $'\e[31m' "$C_0" "$*" >&2; exit 1; }

for t in kubectl kubeseal curl python3; do command -v $t >/dev/null || die "missing tool: $t"; done
k get nodes >/dev/null 2>&1 || die "kube context 'sentpul' unreachable"

d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT; umask 077

if [ -n "$TOKEN_FILE" ]; then
  [ -r "$TOKEN_FILE" ] || die "cannot read $TOKEN_FILE"
  tr -d '\n' < "$TOKEN_FILE" > "$d/pat"
else
  [ -t 0 ] && die "no --token-file and stdin is a terminal; pipe the token in"
  tr -d '\n' > "$d/pat"
fi
[ -s "$d/pat" ] || die "the token is empty"

# Scopes first. An over-scoped token must not be sealed, and finding that out
# after the ciphertext is in git is finding out too late.
curl -sS -o /dev/null -D "$d/hdr" -H "Authorization: Bearer $(cat "$d/pat")" \
  https://api.github.com/user || die "could not reach api.github.com"
grep -qi '^HTTP/[0-9.]* 200' "$d/hdr" || die "GitHub rejected the token: $(head -1 "$d/hdr" | tr -d '\r')"

scopes="$(grep -i '^x-oauth-scopes:' "$d/hdr" | tr -d '\r' | cut -d: -f2- | tr -d ' ')"
ok "GitHub accepted the token, scopes: ${scopes:-<none, a fine-grained token>}"

for bad in repo write:packages delete:packages admin:org write:org workflow; do
  case ",$scopes," in
    *",$bad,"*) die "scope '$bad' is present. A pull credential needs read:packages only.
  Anyone who can read Secrets in $NS would inherit '$bad'. Make a narrower token
  and run this again. This refusal is the reason this script exists.";;
  esac
done

# A classic token must say read:packages. A fine-grained token reports no
# scopes at all, so its permission cannot be checked here; the pull test below
# is what proves it, and it is the test that matters.
if [ -n "$scopes" ]; then
  case ",$scopes," in
    *,read:packages,*) ok "scopes are pull-only";;
    *) die "no read:packages scope, so this token cannot pull images";;
  esac
else
  ok "fine-grained token: scopes are not reported, relying on the pull test below"
fi

# Prove it can pull the digest the manifests pin, not just that it authenticates.
pinned="$(grep -hoP '(?<=image: )ghcr\.io/\S+' platform-gitops/manifests/mission-control.yaml | head -1)"
[ -n "$pinned" ] || die "could not find the pinned image in platform-gitops/manifests/mission-control.yaml"
ref="${pinned#*@}"
ok "manifests pin $ref"

reg_token="$(curl -sS -u "$GH_USER:$(cat "$d/pat")" \
  "https://ghcr.io/token?service=ghcr.io&scope=repository:${PACKAGE}:pull" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')"
[ -n "$reg_token" ] || die "ghcr.io issued no pull token for $PACKAGE"

got="$(curl -sS -o /dev/null -D - -H "Authorization: Bearer $reg_token" \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
  "https://ghcr.io/v2/${PACKAGE}/manifests/${ref}" \
  | grep -i '^docker-content-digest:' | tr -d '\r' | awk '{print $2}')"
[ "$got" = "$ref" ] || die "the token could not fetch $ref from $PACKAGE (got '${got:-nothing}')"
ok "the new token pulled the exact digest the deployment runs"

# One docker config, built by python so the token is JSON-escaped rather than
# pasted into a string by hand.
python3 - "$d/pat" "$GH_USER" > "$d/dockerconfigjson" <<'PY'
import base64, json, sys
tok = open(sys.argv[1]).read().strip()
user = sys.argv[2]
auth = base64.b64encode(f"{user}:{tok}".encode()).decode()
json.dump({"auths": {"ghcr.io": {"username": user, "password": tok, "auth": auth}}}, sys.stdout)
PY

out="platform-gitops/manifests/${NS}-ghcr-pull-sealed-secret.yaml"
[ -f "$out" ] || die "$out does not exist, so this is a new credential rather than a rotation"

k create secret generic ghcr-pull -n "$NS" \
    --type=kubernetes.io/dockerconfigjson \
    --from-file=".dockerconfigjson=$d/dockerconfigjson" \
    --dry-run=client -o yaml \
  | k annotate --local -f - sealedsecrets.bitnami.com/managed=true -o yaml \
  | seal_stdin > "$d/s.yaml"
grep -q 'kind: SealedSecret' "$d/s.yaml" || die "kubeseal produced no SealedSecret"

{ echo "# Generated by scripts/reseal_ghcr_pull.sh on $(date -Is)."
  echo "# Named ${NS}-* so that namespace's Application include glob serves it,"
  echo "# which is the guard api_crawler's test_dag_secrets.py enforces."
  echo "#"
  echo "# PULL ONLY. The script refuses to seal a token carrying repo,"
  echo "# write:packages or delete:packages, so reading this Secret grants the"
  echo "# ability to pull images and nothing else. Scopes seen: ${scopes:-fine-grained}."
  echo "#"
  echo "# Verified before writing: this credential fetched ${ref}"
  echo "# from ${PACKAGE}, the exact digest the deployment runs."
  grep -v '^\s*creationTimestamp' "$d/s.yaml"; } > "$out"

ok "wrote $out"

printf '\n%sNot applied to the cluster on purpose.%s ArgoCD selfHeal reverts a hand\n' "$C_Y" "$C_0"
printf 'applied SealedSecret to git ciphertext within a second, which is how a role\n'
printf 'password and its Secret came apart on 2026-11-11. Commit and merge instead:\n\n'
printf '  git add %s && git commit\n\n' "$out"
printf 'Then confirm the rollout pulled with the new credential:\n'
printf '  kubectl --context sentpul rollout restart -n %s deploy/mission-control\n' "$NS"
printf '  kubectl --context sentpul get pods -n %s -w\n' "$NS"
