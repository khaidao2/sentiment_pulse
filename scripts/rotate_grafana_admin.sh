#!/usr/bin/env bash
# Rotate the Grafana admin password -- for real this time.
#
# WHY A SEPARATE SCRIPT. rotate_platform_secrets.sh resealed
# grafana-admin-sealed and restarted the pod, and reported success. It was
# wrong: Grafana writes the admin password into its OWN DATABASE on first boot,
# and GF_SECURITY_ADMIN_PASSWORD only SEEDS it. On every later start Grafana
# ignores that variable, so the sealed value was never in effect and the
# password committed to the public repo was still live. Verified against the
# live API: old value -> HTTP 200, sealed value -> HTTP 401.
#
# Same class of trap as the Keycloak admin user (DB-backed, Helm value only
# seeds it) and the opposite of the oauth2-proxy client secrets (env-backed, so
# resealing really did rotate those).
#
# So this changes the password THROUGH THE GRAFANA API, which is the only thing
# that touches the database, and then reseals so a rebuilt Grafana seeds with
# the same value instead of silently reverting to something else.
#
# Usage:
#   bash scripts/rotate_grafana_admin.sh            # prompts once
#   bash scripts/rotate_grafana_admin.sh --dry-run  # check everything, change nothing
#   bash scripts/rotate_grafana_admin.sh --yes      # no prompt
#   CURRENT_PW='...' bash scripts/rotate_grafana_admin.sh   # if the stored one is stale

set -euo pipefail

DRY=0; ASSUME_YES=0
for a in "$@"; do case "$a" in
  --dry-run) DRY=1 ;;
  --yes|-y)  ASSUME_YES=1 ;;
  -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
  *) echo "unknown flag: $a" >&2; exit 2 ;;
esac; done

C_R=$'\e[31m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_B=$'\e[1m'; C_0=$'\e[0m'
step(){ printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_0"; }
ok(){   printf '  %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn(){ printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die(){  printf '\n%sFAILED:%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

# ---- fixed facts about this platform -----------------------------------------
KCTX=sentpul                 # kube context, pinned: never act on another cluster
NS=airflow                   # grafana's namespace (it rides with the airflow app)
SECRET=grafana-admin-sealed
DEPLOY=grafana
URL=https://grafana.sentpul.click
CREDS="$HOME/.config/keys/sentpul_credentials.txt"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="platform-gitops/manifests/grafana-admin-sealed-secret.yaml"

k(){ kubectl --context "$KCTX" "$@"; }       # every call context-pinned

cd "$REPO"
[ -d platform-gitops ] || die "not the sentiment_pulse repo: $REPO"

# ---- preflight ---------------------------------------------------------------
step "Preflight"
for t in kubectl kubeseal git curl openssl jq; do
  command -v "$t" >/dev/null || die "missing required tool: $t"
done
ok "tools present"

k config get-contexts "$KCTX" >/dev/null 2>&1 || die "kube context '$KCTX' does not exist"
k get nodes >/dev/null 2>&1 || die "context '$KCTX' unreachable"
ok "context $KCTX reachable (pinned; your current-context is left alone)"

k -n kube-system get pods 2>/dev/null | grep -q sealed-secrets \
  || die "sealed-secrets controller not found; kubeseal output would be undecryptable"
ok "sealed-secrets controller present"

k -n "$NS" get deploy "$DEPLOY" >/dev/null 2>&1 || die "deploy $NS/$DEPLOY not found"
ok "grafana deployment found"

[ -f "$CREDS" ] || die "credential file missing: $CREDS"
[ "$(stat -c %a "$CREDS")" = 600 ] || warn "$CREDS is mode $(stat -c %a "$CREDS"), expected 600"
ok "credential file present"

# The password that works RIGHT NOW. Try, in order: an explicit override, the
# value recorded in the credential file, the sealed secret, and the value that
# leaked via git. Whichever authenticates is the one we must present to the API.
api(){ curl -s -o /dev/null -w '%{http_code}' --max-time 20 -u "admin:$1" "$URL/api/org"; }

CUR=""; SRC=""
try(){ [ -z "$CUR" ] || return 0; [ -n "$2" ] || return 0
       if [ "$(api "$2")" = 200 ]; then CUR="$2"; SRC="$1"; fi; }
try "CURRENT_PW env"      "${CURRENT_PW:-}"
try "$CREDS"              "$(sed -n 's/^grafana admin (user: admin)  *//p' "$CREDS" 2>/dev/null | tr -d '\n')"
try "sealed secret"       "$(k -n "$NS" get secret "$SECRET" -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)"
try "the leaked git value" "adminpassword"

[ -n "$CUR" ] || die "no known password authenticates against $URL. Pass CURRENT_PW='...' explicitly."
ok "authenticated against the Grafana API using: $SRC"
[ "$SRC" = "the leaked git value" ] && \
  warn "the password still in effect is the one that leaked publicly -- this rotation is overdue"

ADMIN_ID="$(curl -s --max-time 20 -u "admin:$CUR" "$URL/api/users/lookup?loginOrEmail=admin" | jq -r '.id // empty')"
[ -n "$ADMIN_ID" ] || die "could not resolve the admin user id via the API"
ok "admin user id: $ADMIN_ID"

cat <<PLAN

${C_B}Plan${C_0}
  1. generate a new 32-char password
  2. change it THROUGH THE API (PUT /api/admin/users/$ADMIN_ID/password) -- the
     only path that updates Grafana's database
  3. verify: new password works, old one is rejected
  4. reseal $NS/$SECRET so a rebuilt Grafana seeds with the same value
  5. update $CREDS in place (mode preserved)
  6. commit + push the resealed manifest
  No pod restart is needed: the change is in Grafana's database, not its env.
PLAN

if [ "$DRY" = 1 ]; then printf '\n%sdry-run: everything above checks out; nothing changed.%s\n' "$C_Y" "$C_0"; exit 0; fi
if [ "$ASSUME_YES" != 1 ]; then
  printf '\nRotate the Grafana admin password now? [y/N] '
  read -r r; case "$r" in y|Y|yes) ;; *) echo "aborted."; exit 1 ;; esac
fi

# ---- rotate ------------------------------------------------------------------
step "1/5  Change the password via the API"
umask 077
NEW="$(openssl rand -base64 36 | tr -d '\n=+/' | cut -c1-32)"
[ ${#NEW} -eq 32 ] || die "password generation produced ${#NEW} chars"

BODY="$(mktemp)"; trap 'rm -f "$BODY"' EXIT
jq -n --arg p "$NEW" '{password:$p}' > "$BODY"
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X PUT \
  -u "admin:$CUR" -H 'Content-Type: application/json' \
  --data-binary @"$BODY" "$URL/api/admin/users/$ADMIN_ID/password")"
[ "$code" = 200 ] || die "the API refused the change (HTTP $code). Nothing has changed."
rm -f "$BODY"; trap - EXIT
ok "Grafana accepted the new password"

step "2/5  Verify"
[ "$(api "$NEW")" = 200 ] || die "the NEW password does not authenticate. The old one may still work -- check before retrying."
ok "new password authenticates (HTTP 200)"
oldcode="$(api "$CUR")"
if [ "$oldcode" = 200 ]; then
  die "the OLD password STILL authenticates (HTTP 200) -- rotation did not take. Do not record the new value yet."
fi
ok "old password rejected (HTTP $oldcode)"

step "3/5  Reseal $NS/$SECRET"
d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT
printf %s admin  > "$d/admin-user"
printf %s "$NEW" > "$d/admin-password"
k create secret generic "$SECRET" -n "$NS" \
  --from-file=admin-user="$d/admin-user" \
  --from-file=admin-password="$d/admin-password" \
  --dry-run=client -o yaml | kubeseal --format yaml > "$d/sealed.yaml"
{ echo "# Grafana admin credential, encrypted to this cluster's sealed-secrets key."
  echo "#"
  echo "# THIS ONLY SEEDS A FRESH GRAFANA. Grafana stores the admin password in its"
  echo "# own database on first boot and ignores GF_SECURITY_ADMIN_PASSWORD on every"
  echo "# later start, so changing this file does NOT change a running Grafana --"
  echo "# resealing alone once left the publicly leaked password live here."
  echo "# Rotate with scripts/rotate_grafana_admin.sh, which goes via the API."
  grep -v '^\s*creationTimestamp' "$d/sealed.yaml"; } > "$MANIFEST"
k apply -f "$MANIFEST" >/dev/null
for i in $(seq 1 30); do k -n "$NS" get secret "$SECRET" >/dev/null 2>&1 && break; sleep 1; done
got="$(k -n "$NS" get secret "$SECRET" -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)"
[ "$got" = "$NEW" ] || die "the sealed secret did not decrypt to the new value -- $MANIFEST is wrong, do not commit it"
ok "sealed, applied, and confirmed to decrypt to the new password"
rm -rf "$d"; trap - EXIT

step "4/5  Record it in $CREDS"
perm="$(stat -c %a "$CREDS")"
tmp="$(mktemp)"; chmod "$perm" "$tmp"
# Replace the single grafana admin line, leaving every other line byte-identical.
NEW_LINE="$(printf '%-34s %s' 'grafana admin (user: admin)' "$NEW")"
awk -v repl="$NEW_LINE" '
  /^grafana admin \(user: admin\)/ { print repl; found=1; next }
  { print }
  END { if (!found) exit 3 }
' "$CREDS" > "$tmp" || die "no 'grafana admin (user: admin)' line in $CREDS -- add it by hand: $NEW_LINE"
mv "$tmp" "$CREDS"; chmod "$perm" "$CREDS"
grep -q "^grafana admin (user: admin)" "$CREDS" || die "credential file lost its grafana line"
ok "updated in place (mode $perm preserved)"

step "5/5  Commit and push"
if git diff --quiet -- "$MANIFEST"; then
  warn "manifest byte-identical to HEAD (nothing to commit)"
else
  git add -- "$MANIFEST"
  git commit -q -F - <<'MSG'
security: rotate the Grafana admin password through the API

The earlier rotation did not take effect. Grafana writes the admin password
into its own database on first boot and ignores GF_SECURITY_ADMIN_PASSWORD on
every later start, so resealing grafana-admin-sealed and restarting the pod
changed nothing -- and the password that had been committed to the public repo
was still live. Verified against the API: the old value returned HTTP 200 and
the sealed value returned 401, which is the reverse of what was reported.

The password is now changed via PUT /api/admin/users/<id>/password, which is
the only path that touches the database, and the secret is resealed so a
rebuilt Grafana seeds with the same value instead of reverting.

Same trap as the Keycloak admin user, and the opposite of the oauth2-proxy
client secrets: those are read from env on every start, so resealing really did
rotate them. The manifest now says which kind it is.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
  ok "committed $(git rev-parse --short HEAD)"
  br="$(git rev-parse --abbrev-ref HEAD)"
  git push -q origin "$br" && ok "pushed to $br"
fi

printf '\n%s================ done ================%s\n' "$C_B" "$C_0"
echo "The new password is in $CREDS (not printed here)."
echo "Read it with:  sed -n 's/^grafana admin (user: admin)  *//p' $CREDS"
echo
echo "Sanity check, if you want it:"
echo "  curl -s -o /dev/null -w '%{http_code}\\n' -u admin:\"\$(sed -n 's/^grafana admin (user: admin)  *//p' $CREDS)\" $URL/api/org"
echo
warn "Grafana's OIDC login via Keycloak is unaffected -- this is the local admin only."
