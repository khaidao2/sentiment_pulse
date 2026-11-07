#!/usr/bin/env bash
# Rotate the Grafana admin password, via the pod's own CLI.
#
# WHY NOT JUST RESEAL. Grafana writes the admin password into its database on
# first boot and ignores GF_SECURITY_ADMIN_PASSWORD on every later start, so
# resealing grafana-admin-sealed and restarting the pod does NOT change a
# running Grafana. That is how the password committed to the public repo stayed
# live after a rotation that reported success. Credentials here split in two:
#
#   env-backed  read from the secret on every start -> resealing rotates them.
#               oauth2-proxy client/cookie secrets, MinIO root.
#   db-backed   the secret only SEEDS a fresh install; the running service keeps
#               its own copy. Grafana admin, and the Keycloak admin user.
#
# WHY THE CLI AND NOT THE HTTP API. The first version of this script discovered
# the current password by trying candidates against /api/org in turn. Grafana
# has brute-force protection (5 consecutive failures -> the admin account is
# blocked for ~5 minutes), so a probe loop IS a brute-force attempt and locked
# the account out instead of rotating it.
#
# `grafana cli admin reset-admin-password` runs inside the pod, writes the
# database directly, needs no current password, and cannot be locked out. It
# also takes --password-from-stdin, so the new value never appears in argv.
#
# Usage:
#   bash scripts/rotate_grafana_admin.sh            # prompts once
#   bash scripts/rotate_grafana_admin.sh --dry-run  # check everything, change nothing
#   bash scripts/rotate_grafana_admin.sh --yes      # no prompt

set -euo pipefail

DRY=0; ASSUME_YES=0
for a in "$@"; do case "$a" in
  --dry-run) DRY=1 ;;
  --yes|-y)  ASSUME_YES=1 ;;
  -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  *) echo "unknown flag: $a" >&2; exit 2 ;;
esac; done

C_R=$'\e[31m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_B=$'\e[1m'; C_0=$'\e[0m'
step(){ printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_0"; }
ok(){   printf '  %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn(){ printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die(){  printf '\n%sFAILED:%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

KCTX=sentpul                  # pinned: never act on whatever current-context is
NS=airflow
SECRET=grafana-admin-sealed
URL=https://grafana.sentpul.click
HOMEPATH=/usr/share/grafana
CONF=/etc/grafana/grafana.ini
CREDS="$HOME/.config/keys/sentpul_credentials.txt"
CRED_LABEL='grafana admin (user: admin)'
MANIFEST="platform-gitops/manifests/grafana-admin-sealed-secret.yaml"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

k(){ kubectl --context "$KCTX" "$@"; }
cd "$REPO"
[ -d platform-gitops ] || die "not the sentiment_pulse repo: $REPO"

step "Preflight"
for t in kubectl kubeseal git curl openssl; do
  command -v "$t" >/dev/null || die "missing required tool: $t"
done
ok "tools present"

k config get-contexts "$KCTX" >/dev/null 2>&1 || die "kube context '$KCTX' does not exist"
k get nodes >/dev/null 2>&1 || die "context '$KCTX' unreachable"
ok "context $KCTX reachable"

k -n kube-system get pods 2>/dev/null | grep -q sealed-secrets \
  || die "sealed-secrets controller not found; kubeseal output would be undecryptable"
ok "sealed-secrets controller present"

POD="$(k -n "$NS" get pods -l app.kubernetes.io/name=grafana \
        --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
[ -n "$POD" ] || die "no Running grafana pod in namespace $NS"
ok "grafana pod: $POD"

k -n "$NS" exec "$POD" -c grafana -- sh -c "test -x $HOMEPATH/bin/grafana" 2>/dev/null \
  || die "grafana binary not at $HOMEPATH/bin/grafana in this image"
ok "grafana cli available in the pod"

[ -f "$CREDS" ] || die "credential file missing: $CREDS"
grep -q "^$CRED_LABEL" "$CREDS" || die "no '$CRED_LABEL' line in $CREDS"
PERM="$(stat -c %a "$CREDS")"
ok "credential file present (mode $PERM) with the line to update"

# Deliberately NOT probing the current password: see the header. We do not need
# it, and asking for it wrongly is what locked the account out last time.
if k -n "$NS" logs "$POD" -c grafana --tail=200 2>/dev/null | grep -q "temporarily blocked"; then
  warn "the admin account is currently in a brute-force lockout from earlier attempts."
  warn "That does not block this script -- the CLI path bypasses it -- but the"
  warn "post-change API check may need to wait for the block to expire (~5 min)."
fi

cat <<PLAN

${C_B}Plan${C_0}
  1. generate a new 32-char password
  2. reset it with: grafana cli admin reset-admin-password --password-from-stdin
     inside $POD -- writes the database, no current password needed, and the
     value never appears in a command line
  3. verify the new password over the API, waiting out any lockout
  4. reseal $NS/$SECRET so a REBUILT grafana seeds the same value
  5. update '$CRED_LABEL' in $CREDS, in place
  6. commit + push $MANIFEST
  No restart: the change is in Grafana's database, not its environment.
PLAN

if [ "$DRY" = 1 ]; then printf '\n%sdry-run: all checks pass; nothing changed.%s\n' "$C_Y" "$C_0"; exit 0; fi
if [ "$ASSUME_YES" != 1 ]; then
  printf '\nRotate the Grafana admin password now? [y/N] '
  read -r r; case "$r" in y|Y|yes) ;; *) echo "aborted."; exit 1 ;; esac
fi

step "1/5  Reset the password via the pod's CLI"
umask 077
NEW="$(openssl rand -base64 36 | tr -d '\n=+/' | cut -c1-32)"
[ ${#NEW} -eq 32 ] || die "password generation produced ${#NEW} chars"

out="$(printf %s "$NEW" | k -n "$NS" exec -i "$POD" -c grafana -- \
        "$HOMEPATH/bin/grafana" cli --homepath "$HOMEPATH" --config "$CONF" \
        admin reset-admin-password --password-from-stdin 2>&1)" \
  || die "reset-admin-password failed: ${out:-no output}"
printf '%s\n' "$out" | grep -qi "password updated\|admin password changed" \
  || warn "unexpected CLI output (continuing to verify): $(printf '%s' "$out" | tail -1)"
ok "CLI reports the admin password updated"

step "2/5  Verify over the API (waiting out any lockout)"
api(){ curl -s --max-time 20 -u "admin:$1" "$URL/api/org"; }
good=0
for i in $(seq 1 24); do          # up to ~6 minutes
  body="$(api "$NEW")"
  if printf '%s' "$body" | grep -q '"id"'; then good=1; break; fi
  if printf '%s' "$body" | grep -q "temporarily blocked"; then
    [ "$i" = 1 ] && printf '  waiting out the lockout'
    printf '.'; sleep 15; continue
  fi
  # a plain "Invalid username or password" here means the reset did not stick
  break
done
[ "$i" -gt 1 ] && echo
[ "$good" = 1 ] || die "the new password does not authenticate. Last response: $(printf '%s' "$body" | head -c 160)"
ok "new password authenticates"

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
  echo "# THIS ONLY SEEDS A FRESH GRAFANA. Grafana keeps the admin password in its"
  echo "# own database and ignores GF_SECURITY_ADMIN_PASSWORD after first boot, so"
  echo "# editing this file does NOT change a running Grafana -- resealing alone"
  echo "# once left a publicly leaked password live. Use"
  echo "# scripts/rotate_grafana_admin.sh, which resets it inside the pod."
  grep -v '^\s*creationTimestamp' "$d/sealed.yaml"; } > "$MANIFEST"
k apply -f "$MANIFEST" >/dev/null
for _ in $(seq 1 30); do k -n "$NS" get secret "$SECRET" >/dev/null 2>&1 && break; sleep 1; done
got="$(k -n "$NS" get secret "$SECRET" -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d)"
[ "$got" = "$NEW" ] || die "the sealed secret did not decrypt to the new value -- do not commit $MANIFEST"
ok "sealed, applied, confirmed to decrypt to the new password"
rm -rf "$d"; trap - EXIT

step "4/5  Record it in $CREDS"
tmp="$(mktemp)"; chmod "$PERM" "$tmp"
NEW_LINE="$(printf '%-34s %s' "$CRED_LABEL" "$NEW")"
awk -v label="$CRED_LABEL" -v repl="$NEW_LINE" '
  index($0, label) == 1 { print repl; found=1; next }
  { print }
  END { if (!found) exit 3 }
' "$CREDS" > "$tmp" || { rm -f "$tmp"; die "could not update $CREDS; set it by hand"; }
mv "$tmp" "$CREDS"; chmod "$PERM" "$CREDS"
grep -q "^$CRED_LABEL" "$CREDS" || die "credential file lost its grafana line"
ok "updated in place (mode $PERM preserved)"

step "5/5  Commit and push"
if git diff --quiet -- "$MANIFEST"; then
  warn "manifest unchanged from HEAD (nothing to commit)"
else
  git add -- "$MANIFEST"
  git commit -q -F - <<'MSG'
security: actually rotate the Grafana admin password

The earlier rotation did not take effect. Grafana keeps the admin password in
its own database and ignores GF_SECURITY_ADMIN_PASSWORD after first boot, so
resealing the secret and restarting the pod changed nothing -- the password
that had been committed to the public repo was still live, which the API
confirmed: old value 200, sealed value 401.

It is now reset with `grafana cli admin reset-admin-password` inside the pod,
which writes the database, and the secret is resealed so a rebuilt Grafana
seeds the same value instead of reverting.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
  ok "committed $(git rev-parse --short HEAD)"
  br="$(git rev-parse --abbrev-ref HEAD)"
  git push -q origin "$br" && ok "pushed to $br"
fi

printf '\n%s================ done ================%s\n' "$C_B" "$C_0"
echo "The new password is in $CREDS and was not printed."
echo "Read it:   sed -n 's/^$CRED_LABEL  *//p' $CREDS"
warn "Keycloak SSO login to Grafana is unaffected; this is the local admin only."
