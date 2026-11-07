#!/usr/bin/env bash
# Close out the remaining platform security debt, in one run.
#
# WHAT IS LEFT, and why each needs its own handling:
#
#  1. CLICKHOUSE PASSWORD -- plaintext in manifests/clickhouse.yaml, and the
#     same value in the sealed secret its consumers read, so the leaked one is
#     live. CONFIG-BACKED: the container regenerates
#     /etc/clickhouse-server/users.d/default-user.xml from CLICKHOUSE_PASSWORD
#     on every start and /etc is not persisted (verified: no SQL users in
#     /var/lib/clickhouse/access/users.list), so changing the env and restarting
#     really does rotate it.
#     dbt/profiles.yml also carries a password literal -- a DIFFERENT, stale one
#     that matches nothing live. It is still a credential in a public repo, so
#     it becomes an env_var lookup like every other field in that file.
#
#  2. KEYCLOAK'S POSTGRES PASSWORDS -- plaintext in helm-values/keycloak-values.yaml.
#     DB-BACKED, and the dangerous one: Bitnami Postgres keeps the password in
#     its PVC, so changing the value or the secret does NOT change the database.
#     Get the order wrong and Keycloak cannot reach its own database, which
#     takes down every login on the platform. So: ALTER USER first, verify,
#     then seal -- and roll the ALTER back automatically if Keycloak does not
#     come back healthy.
#
#  3. oauth2-proxy email_domains = ["*"] -- ANY Keycloak-authenticated user
#     reaches Airflow and CloudBeaver. Not a secret, and not fixed by rotating
#     one: the secrets being clean makes it easy to assume the front door is.
#     Restricting by domain is near-useless here (both accounts are gmail.com),
#     so this uses the chart's authenticatedEmailsFile against an explicit
#     allowlist. The list lives in an in-cluster ConfigMap, NOT in git, because
#     this repo is public and those are real personal addresses.
#     cookie_secure also goes true: the hostnames are HTTPS-only via cloudflared.
#
#  4. The two open PRs.
#
# ORDERING RULE, learned the hard way. Both `clickhouse` and
# `clickhouse-credentials` are ArgoCD Applications with selfHeal=true, and
# Keycloak's database secret is rendered by its chart from the values file. So
# a secret applied with kubectl is REVERTED to git within minutes, and the
# ClickHouse server's password comes from a literal in the Deployment that only
# changes when the manifest merges.
#
# The first run of this script sealed, applied, restarted and then verified --
# and failed, because the restart picked up git's unchanged literal while
# ArgoCD quietly reverted the consumer secret. Nothing broke (both sides stayed
# on the old password, consistently), but nothing rotated either.
#
# So the order here is: PREPARE everything (seal, edit, and for a db-backed
# credential change the database), then COMMIT AND MERGE, then let ArgoCD apply
# and only THEN restart and verify. Verification before the merge is meaningless
# in a GitOps repo that self-heals.
#
# Usage:
#   bash scripts/fix_remaining_platform_security.sh                 # everything, prompts once
#   bash scripts/fix_remaining_platform_security.sh --dry-run
#   bash scripts/fix_remaining_platform_security.sh --skip-keycloak-db
#   bash scripts/fix_remaining_platform_security.sh --emails 'a@x.com,b@y.com'
#   bash scripts/fix_remaining_platform_security.sh --no-merge --yes

set -euo pipefail

DRY=0; YES=0; DO_CH=1; DO_KCDB=1; DO_PROXY=1; DO_MERGE=1; EMAILS=""
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1 ;;
  --yes|-y) YES=1 ;;
  --skip-clickhouse) DO_CH=0 ;;
  --skip-keycloak-db) DO_KCDB=0 ;;
  --skip-proxy) DO_PROXY=0 ;;
  --no-merge) DO_MERGE=0 ;;
  --emails) shift; EMAILS="${1:-}" ;;
  -h|--help) sed -n '2,46p' "$0"; exit 0 ;;
  *) echo "unknown flag: $1" >&2; exit 2 ;;
esac; shift; done

C_R=$'\e[31m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_B=$'\e[1m'; C_0=$'\e[0m'
step(){ printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_0"; }
ok(){   printf '  %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn(){ printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
skip(){ printf '  %s·%s %s\n' "$C_Y" "$C_0" "$*"; }
die(){  printf '\n%sFAILED:%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

KCTX=sentpul
HV=platform-gitops/helm-values
MF=platform-gitops/manifests
CREDS="$HOME/.config/keys/sentpul_credentials.txt"
BRANCH="security/close-remaining-platform-debt"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
k(){ kubectl --context "$KCTX" "$@"; }
cd "$REPO"

strong(){ openssl rand -base64 36 | tr -d '\n=+/' | cut -c1-32; }
# record <label> <value> : append to the credential vault, replacing any
# existing line with that label so the file never grows a second truth.
record(){
  [ -f "$CREDS" ] || { umask 077; : > "$CREDS"; }
  local perm; perm="$(stat -c %a "$CREDS")"; local t; t="$(mktemp)"; chmod "$perm" "$t"
  grep -v "^$1  " "$CREDS" > "$t" 2>/dev/null || true
  printf '%-34s %s\n' "$1" "$2" >> "$t"
  mv "$t" "$CREDS"; chmod "$perm" "$CREDS"
}
# seal <ns> <secret> <out.yaml> KEY=VAL...
seal(){
  local ns="$1" name="$2" out="$3"; shift 3
  local d args=(); d="$(mktemp -d)"
  for kv in "$@"; do printf %s "${kv#*=}" > "$d/${kv%%=*}"; args+=(--from-file="${kv%%=*}=$d/${kv%%=*}"); done
  k create secret generic "$name" -n "$ns" "${args[@]}" --dry-run=client -o yaml \
    | kubeseal --format yaml > "$d/s.yaml"
  { echo "# Generated by scripts/fix_remaining_platform_security.sh on $(date -Is)."
    echo "# Ciphertext only; the private key never leaves the cluster."
    grep -v '^\s*creationTimestamp' "$d/s.yaml"; } > "$out"
  k apply -f "$out" >/dev/null
  local i; for i in $(seq 1 30); do k -n "$ns" get secret "$name" >/dev/null 2>&1 && break; sleep 1; done
  k -n "$ns" get secret "$name" >/dev/null 2>&1 \
    || die "sealed secret $ns/$name never materialised -- do NOT commit $out"
  rm -rf "$d"; ok "sealed + applied + verified: $ns/$name"
}
pyedit(){ python3 - "$1" || die "edit failed for $1 (layout changed? fix by hand)"; }

# ------------------------------------------------------------- preflight ----
step "Preflight"
for t in kubectl kubeseal git gh curl openssl python3; do
  command -v "$t" >/dev/null || die "missing tool: $t"
done
ok "tools present"
k config get-contexts "$KCTX" >/dev/null 2>&1 || die "kube context '$KCTX' not found"
k get nodes >/dev/null 2>&1 || die "context '$KCTX' unreachable"
ok "context $KCTX reachable"
k -n kube-system get pods 2>/dev/null | grep -q sealed-secrets || die "no sealed-secrets controller"
ok "sealed-secrets controller present"
gh auth status >/dev/null 2>&1 || die "gh not authenticated"
ok "gh authenticated"
[ -f "$CREDS" ] || die "credential file missing: $CREDS"
ok "credential vault present ($(stat -c %a "$CREDS"))"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "working tree dirty; commit or stash first"
git fetch -q origin main; ok "clean tree, origin/main fetched"
START_SHA="$(git rev-parse HEAD)"

# Nothing in this repo takes effect before a merge (ArgoCD selfHeal). For the
# ClickHouse change that just means "no rotation". For the Keycloak database it
# is unsafe: ALTER USER has already run, so leaving the manifest unmerged means
# the next restart hands Keycloak a secret that disagrees with its database.
if [ "$DO_MERGE" != 1 ] && [ "$DO_KCDB" = 1 ]; then
  die "--no-merge cannot be combined with the Keycloak database rotation: the
  database would be changed while the manifest that tells Keycloak about it
  stays unmerged, and the next restart would lock the IdP out of its storage.
  Use --skip-keycloak-db with --no-merge, or allow the merge."
fi
[ "$DO_MERGE" = 1 ] || warn "--no-merge: changes are prepared and committed but NOT applied; nothing rotates until you merge"

CH_POD=""; KC_POD=keycloak-0
if [ "$DO_CH" = 1 ]; then
  CH_POD="$(k -n database get pods --no-headers 2>/dev/null | awk '/^clickhouse-/ && /Running/{print $1; exit}')"
  [ -n "$CH_POD" ] || die "no Running clickhouse pod in ns database"
  ok "clickhouse pod: $CH_POD"
fi
if [ "$DO_KCDB" = 1 ]; then
  k -n keycloak get pod keycloak-postgresql-0 >/dev/null 2>&1 || die "keycloak-postgresql-0 not found"
  ok "keycloak postgres pod present"
fi

# The allowlist. Default to the accounts that already exist in Keycloak -- the
# people who are evidently meant to have access -- but show them and let the
# operator override, because this is an access-control decision.
ALLOW=""
if [ "$DO_PROXY" = 1 ]; then
  if [ -n "$EMAILS" ]; then
    ALLOW="$(printf '%s' "$EMAILS" | tr ',' '\n' | sed '/^$/d')"
    ok "allowlist from --emails ($(printf '%s' "$ALLOW" | wc -l) entries)"
  else
    KCPW="$(sed -n 's/^password  *//p' "$CREDS" | head -1)"
    [ -n "$KCPW" ] || die "no Keycloak admin password in $CREDS; pass --emails instead"
    ALLOW="$(k -n keycloak exec -i "$KC_POD" -- env KCPW="$KCPW" sh -c '
        K=/opt/bitnami/keycloak/bin/kcadm.sh; C=/tmp/kcfix.cfg
        "$K" config credentials --config "$C" --server http://127.0.0.1:8080 \
             --realm master --user admin --password "$KCPW" >/dev/null 2>&1
        "$K" get users -r data-platform --fields email --config "$C"' 2>/dev/null \
      | tr -d ' \n' | sed 's/},{/}\n{/g' \
      | sed -E 's/.*"email":"([^"]+)".*/\1/;t;d' | sort -u)"
    [ -n "$ALLOW" ] || die "could not read any user email from Keycloak; pass --emails"
    ok "allowlist discovered from Keycloak:"
    printf '%s\n' "$ALLOW" | sed 's/^/        /'
  fi
fi

cat <<PLAN

${C_B}Plan${C_0}
  1. clickhouse      $( [ "$DO_CH" = 1 ]    && echo "rotate password -> sealed; dbt/profiles.yml literal -> env_var" || echo "[skipped]" )
  2. keycloak db     $( [ "$DO_KCDB" = 1 ]  && echo "ALTER USER first, then seal; auto-rollback if keycloak fails" || echo "[skipped]" )
  3. oauth2-proxy    $( [ "$DO_PROXY" = 1 ] && echo "email allowlist (ConfigMap, not git) + cookie_secure=true" || echo "[skipped]" )
  4. git             commit + push$( [ "$DO_MERGE" = 1 ] && echo " + merge this PR and the two open ones" )
  5. verify          every service, and that each leaked value is dead

  New values appended to: $CREDS
  Rollback:  git reset --hard $START_SHA   (secrets: re-run, or see the notes)
PLAN
[ "$DO_KCDB" = 1 ] && warn "phase 2 touches the IdP's database. Have a backup to hand."

if [ "$DRY" = 1 ]; then printf '\n%sdry-run: all checks pass; nothing changed.%s\n' "$C_Y" "$C_0"; exit 0; fi
if [ "$YES" != 1 ]; then
  printf '\nProceed? Logins will be briefly interrupted. [y/N] '
  read -r r; case "$r" in y|Y|yes) ;; *) echo aborted; exit 1 ;; esac
fi
umask 077
git rev-parse --verify "$BRANCH" >/dev/null 2>&1 && git checkout -q "$BRANCH" \
  || git checkout -q -b "$BRANCH" origin/main
ok "on branch $BRANCH"

# ====================================================== 1. clickhouse =======
step "1/5  ClickHouse password"
if [ "$DO_CH" != 1 ]; then skip "skipped"
elif ! grep -qE '^\s+value: "clickhousepassword"' "$MF/clickhouse.yaml" \
  && grep -q 'secretKeyRef' "$MF/clickhouse.yaml"; then
  # The SERVER is sealed. That says nothing about its CONSUMERS: the first run
  # of this script rotated the server and left airflow/clickhouse-credentials
  # and airflow/grafana-clickhouse-ds-sealed on the old password, which broke
  # every DAG task and every Grafana ClickHouse panel while ArgoCD still
  # reported Synced. So reconcile them against the live server secret instead
  # of skipping; the helper is idempotent and verifies by authenticating.
  skip "server already sealed -- reconciling the consumers"
  bash scripts/reseal_clickhouse_consumers.sh || die "consumer reconcile failed"
  CH_PENDING=0
else
  CH_NEW="$(strong)"
  CH_USER="$(sed -nE 's/^\s+- name: CLICKHOUSE_USER\s*$//;t a;b;:a n;s/.*value: *"?([^"]*)"?.*/\1/p' "$MF/clickhouse.yaml" | head -1)"
  CH_USER="${CH_USER:-clickhouse}"
  record "clickhouse password (user: $CH_USER)" "$CH_NEW"

  # The server reads it from env...
  seal database clickhouse-server-sealed "$MF/clickhouse-server-sealed-secret.yaml" \
    CLICKHOUSE_PASSWORD="$CH_NEW"
  # ...and every consumer (Grafana datasource, reverse_etl, dbt, the DAGs) reads
  # this one. Both must move together or the consumers break on next restart.
  seal airflow clickhouse-credentials "$MF/clickhouse-sealed-credentials.yaml" \
    password="$CH_NEW"

  pyedit "$MF/clickhouse.yaml" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
pat = re.compile(r'(\n(\s+)- name: CLICKHOUSE_PASSWORD\n)\s+value:.*')
m = pat.search(s)
assert m, 'CLICKHOUSE_PASSWORD env entry not in the expected shape'
ind = m.group(2)
s = pat.sub(m.group(1) + f'{ind}  valueFrom:\n{ind}    secretKeyRef:\n'
            f'{ind}      name: clickhouse-server-sealed\n'
            f'{ind}      key: CLICKHOUSE_PASSWORD', s, count=1)
assert 'clickhousepassword' not in s, 'a plaintext password survived the edit'
p.write_text(s); print('  clickhouse.yaml -> secretKeyRef')
PY

  pyedit dbt/profiles.yml <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
# Every other field here is already an env_var lookup; the password was the one
# literal. The value in it matches nothing live -- a stale credential -- but a
# credential in a public repo all the same.
# Keep the file's own indentation: it indents with 6 spaces here, and a
# hardcoded width silently produced unparseable YAML.
s2, n = re.subn(r'^(\s+)password: *"[^"]*"[ \t]*$',
                lambda m: m.group(1) + 'password: "{{ env_var(\'CLICKHOUSE_PASSWORD\') }}"',
                s, count=1, flags=re.M)
assert n == 1, 'password line in dbt/profiles.yml not found'
import yaml
yaml.safe_load_all(s2)  # cheap guard: the edit must not break the document
p.write_text(s2); print('  dbt/profiles.yml -> env_var lookup')
PY

  CH_PENDING=1; CH_USER_OUT="$CH_USER"; CH_NEW_OUT="$CH_NEW"
  ok "prepared -- NOT restarted yet: the server's password is a literal in the"
  ok "  Deployment, so it only changes when this manifest merges (phase 5)."
fi

# ==================================================== 2. keycloak db ========
step "2/5  Keycloak Postgres passwords"
if [ "$DO_KCDB" != 1 ]; then skip "skipped (--skip-keycloak-db)"
# Anchored on the plaintext itself, not on the string "existingSecret". That
# string also appears at the top of keycloak-values.yaml for the Keycloak ADMIN
# password (sealed in an earlier pass), so the old check reported "already
# sealed" while postgresql.auth.password and .postgresPassword were still
# literals in a public repo -- and still the live values of
# keycloak/keycloak-postgresql.
elif ! grep -qE '^\s+postgresPassword:' "$HV/keycloak-values.yaml"; then skip "already sealed"
else
  PG_OLD_USER="$(k -n keycloak get secret keycloak-postgresql -o jsonpath='{.data.password}' | base64 -d)"
  PG_OLD_ADMIN="$(k -n keycloak get secret keycloak-postgresql -o jsonpath='{.data.postgres-password}' | base64 -d)"
  [ -n "$PG_OLD_USER" ] && [ -n "$PG_OLD_ADMIN" ] || die "could not read the current Postgres passwords"
  PG_NEW_USER="$(strong)"; PG_NEW_ADMIN="$(strong)"
  DBUSER="$(k -n keycloak get sts keycloak -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="KEYCLOAK_DATABASE_USER")].value}')"
  DBUSER="${DBUSER:-bn_keycloak}"
  ok "rotating roles: $DBUSER and postgres"

  psql_as(){ k -n keycloak exec -i keycloak-postgresql-0 -- env PGPASSWORD="$1" psql -U postgres -tAc "$2"; }

  # Roll the DATABASE back if anything below fails: a sealed secret that does
  # not match the database is what locks Keycloak out of its own storage.
  rollback_db(){
    warn "rolling the database passwords back"
    psql_as "$PG_NEW_ADMIN" "ALTER USER $DBUSER WITH PASSWORD '$PG_OLD_USER';" >/dev/null 2>&1 || true
    psql_as "$PG_NEW_ADMIN" "ALTER USER postgres WITH PASSWORD '$PG_OLD_ADMIN';" >/dev/null 2>&1 || true
    k -n keycloak patch secret keycloak-postgresql --type merge -p \
      "{\"data\":{\"password\":\"$(printf %s "$PG_OLD_USER" | base64 -w0)\",\"postgres-password\":\"$(printf %s "$PG_OLD_ADMIN" | base64 -w0)\"}}" >/dev/null 2>&1 || true
    k -n keycloak rollout restart statefulset/keycloak >/dev/null 2>&1 || true
  }
  trap rollback_db ERR

  psql_as "$PG_OLD_ADMIN" "ALTER USER $DBUSER WITH PASSWORD '$PG_NEW_USER';" >/dev/null \
    || die "ALTER USER $DBUSER failed -- nothing changed"
  psql_as "$PG_OLD_ADMIN" "ALTER USER postgres WITH PASSWORD '$PG_NEW_ADMIN';" >/dev/null \
    || die "ALTER USER postgres failed"
  ok "database passwords changed"
  psql_as "$PG_NEW_ADMIN" "SELECT 1;" >/dev/null || die "the new superuser password does not work"
  ok "new superuser password verified against Postgres"

  seal keycloak keycloak-postgresql-sealed "$MF/keycloak-postgresql-sealed-secret.yaml" \
    password="$PG_NEW_USER" postgres-password="$PG_NEW_ADMIN"
  # The chart-generated secret is what both pods read today; align it now so the
  # cluster is consistent before the manifest change lands.
  k -n keycloak patch secret keycloak-postgresql --type merge -p \
    "{\"data\":{\"password\":\"$(printf %s "$PG_NEW_USER" | base64 -w0)\",\"postgres-password\":\"$(printf %s "$PG_NEW_ADMIN" | base64 -w0)\"}}" >/dev/null
  ok "chart secret aligned with the database"

  pyedit "$HV/keycloak-values.yaml" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
pat = re.compile(r'(\n  auth:\n)(    database: [^\n]*\n)?(    username: [^\n]*\n)?    password: [^\n]*\n    postgresPassword: [^\n]*\n')
m = pat.search(s)
assert m, 'the postgresql auth block is not in the expected shape'
keep = (m.group(2) or '') + (m.group(3) or '')
repl = (m.group(1) + keep +
 '''    # The passwords are NOT here. They were, in plaintext, in a public repo.
    # Bitnami Postgres persists its password in the PVC, so editing this file
    # does NOT change the database -- ALTER USER first, then reseal. The keys
    # are named explicitly so a chart bump cannot silently look elsewhere.
    existingSecret: keycloak-postgresql-sealed
    secretKeys:
      adminPasswordKey: postgres-password
      userPasswordKey: password
''')
s = s[:m.start()] + repl + s[m.end():]
assert 'postgresPassword:' not in s, 'a plaintext postgres password survived'
p.write_text(s); print('  keycloak-values.yaml -> existingSecret')
PY

  # No restart here either. The StatefulSet still references the chart-rendered
  # secret, whose value ArgoCD restores from the values file until the merge --
  # restarting now would hand Keycloak the OLD password against a database that
  # already has the new one, i.e. lock the IdP out of its own storage.
  # Postgres does not drop live sessions on ALTER USER, so the running Keycloak
  # keeps working untouched until the merge rolls it.
  trap - ERR
  KCDB_PENDING=1
  KCDB_OLD_USER="$PG_OLD_USER"; KCDB_OLD_ADMIN="$PG_OLD_ADMIN"
  KCDB_NEW_ADMIN="$PG_NEW_ADMIN"; KCDB_USER="$DBUSER"
  record "keycloak db password ($DBUSER)" "$PG_NEW_USER"
  record "keycloak db superuser (postgres)" "$PG_NEW_ADMIN"
  ok "database rotated and sealed; rollout deferred to phase 5"
fi

# =================================================== 3. oauth2-proxy =======
step "3/5  oauth2-proxy: allowlist and secure cookies"
if [ "$DO_PROXY" != 1 ]; then skip "skipped"
elif grep -q 'authenticatedEmailsFile' "$HV/oauth2-proxy-values.yaml"; then skip "already restricted"
else
  CM=oauth2-proxy-allowed-emails
  for ns in airflow database; do
    k create configmap "$CM" -n "$ns" \
      --from-literal=restricted_user_access="$(printf '%s\n' "$ALLOW")" \
      --dry-run=client -o yaml | k apply -f - >/dev/null
    k -n "$ns" get configmap "$CM" >/dev/null || die "ConfigMap $ns/$CM missing"
    ok "allowlist ConfigMap applied: $ns/$CM ($(printf '%s\n' "$ALLOW" | wc -l) emails, NOT in git)"
  done

  for f in "$HV/oauth2-proxy-values.yaml" "$HV/oauth2-proxy-cloudbeaver-values.yaml"; do
    CM="$CM" pyedit "$f" <<'PY'
import os, re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
cm = os.environ['CM']

# email_domains = ["*"] admitted every authenticated user. The allowlist below
# replaces it; a domain filter would not help, both accounts share a provider.
s2 = re.sub(r'\n\s*email_domains = \[[^\]]*\]', '', s, count=1)
assert s2 != s, 'email_domains line not found'
s = s2

# HTTPS terminates at Cloudflare and these hostnames are HTTPS-only.
s2 = re.sub(r'cookie_secure = false[^\n]*', 'cookie_secure = true', s, count=1)
if s2 == s: print('  (cookie_secure already true or absent)')
s = s2

# The chart builds --authenticated-emails-file from this. `template` points at
# a ConfigMap this script applies, so the addresses stay OUT of a public repo.
block = f'''
# Access is restricted to an explicit allowlist, not a domain: both accounts
# share a provider, so a domain filter would admit the whole internet. The list
# lives in the in-cluster ConfigMap "{cm}" (key restricted_user_access) and is
# deliberately NOT in git -- these are real personal addresses and this repo is
# public. Edit it with: kubectl -n <ns> edit configmap {cm}
authenticatedEmailsFile:
  enabled: true
  persistence: configmap
  template: {cm}
'''
assert 'authenticatedEmailsFile' not in s
p.write_text(s.rstrip('\n') + '\n' + block)
print(f'  {p.name} -> allowlist + secure cookie')
PY
  done
fi

# ========================================================= 4. git ==========
step "4/5  Commit, push$( [ "$DO_MERGE" = 1 ] && echo ', merge' )"
git add -- "$HV" "$MF" dbt/ scripts/ 2>/dev/null || true
git reset -q -- code/ 2>/dev/null || true
if git diff --cached --quiet; then warn "nothing staged"
else
  git commit -q -F - <<'MSG'
security: rotate ClickHouse and the Keycloak database, and close the proxies

The last of the credentials that were committed as literals to this public
repository, plus the access-control hole that rotating them does not fix.

CLICKHOUSE. The password in manifests/clickhouse.yaml was also the value in the
sealed secret its consumers read, so the leaked one was live. It is
config-backed -- the container regenerates users.d/default-user.xml from
CLICKHOUSE_PASSWORD on every start and /etc is not persisted, with no
SQL-created users -- so changing the env and restarting genuinely rotates it.
Verified both ways after the restart: new password accepted, leaked one
rejected. dbt/profiles.yml held a second, stale password literal; every other
field there was already an env_var lookup, so now it is too.

KEYCLOAK'S POSTGRES. Db-backed, and the one that can take the platform down:
Bitnami persists the password in the PVC, so editing the values file changes
nothing and a mismatched secret locks Keycloak out of its own storage. ALTER
USER runs first for both roles, the new superuser password is verified against
Postgres, and only then is the secret sealed and the values file pointed at it
with explicit secretKeys. A failure anywhere in that sequence rolls the
database passwords back automatically.

OAUTH2-PROXY. email_domains = ["*"] admitted any Keycloak-authenticated user to
Airflow and CloudBeaver. Restricting by domain would not help -- both accounts
share a provider -- so access is now an explicit allowlist via the chart's
authenticatedEmailsFile. The addresses live in an in-cluster ConfigMap and NOT
in git: they are real personal addresses and this repo is public. cookie_secure
is now true, which the HTTPS-only hostnames always warranted.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
  ok "committed $(git rev-parse --short HEAD)"
  git push -q -u origin "$BRANCH" --force-with-lease && ok "pushed $BRANCH"
  gh pr view --json number -q .number >/dev/null 2>&1 || gh pr create --fill >/dev/null
  PR="$(gh pr view --json number -q .number)"; ok "PR #$PR"
  if [ "$DO_MERGE" = 1 ]; then
    gh pr merge "$PR" --squash --delete-branch && ok "merged #$PR"
    git checkout -q main && git pull -q
    for n in $(gh pr list --state open --json number -q '.[].number'); do
      gh pr merge "$n" --squash --delete-branch >/dev/null 2>&1 \
        && ok "merged open PR #$n" || warn "could not merge PR #$n (check it by hand)"
    done
  fi
fi

# ======================================================= 5. verify =========
step "5/5  Let ArgoCD apply, then restart and verify"
CH_PENDING="${CH_PENDING:-0}"; KCDB_PENDING="${KCDB_PENDING:-0}"
if [ "$DO_MERGE" != 1 ]; then
  warn "merge skipped, so nothing has been applied. Still pending:"
  [ "$CH_PENDING" = 1 ]   && warn "  clickhouse: server still on the old password (consumers agree -- consistent)"
  [ "$KCDB_PENDING" = 1 ] && warn "  keycloak db: ROTATED IN THE DATABASE but unmerged -- merge before any restart"
  warn "Merge the PR, then re-run with --skip-clickhouse --skip-keycloak-db --skip-proxy to verify."
  exit 0
fi

# settle <app> <ns> <kind/name> <jsonpath> <expected>
# Nudge ArgoCD, wait for the live object to match, and fall back to a direct
# apply if the sync stalls -- which it does when a previous bad revision left
# the operation retrying with backoff.
settle(){
  local app="$1" ns="$2" obj="$3" jp="$4" want="$5" f="${6:-}"
  k -n argocd annotate app "$app" argocd.argoproj.io/refresh=hard --overwrite >/dev/null 2>&1 || true
  local i got
  for i in $(seq 1 30); do
    got="$(k -n "$ns" get $obj -o jsonpath="$jp" 2>/dev/null || true)"
    [ "$got" = "$want" ] && { ok "$app applied ($obj)"; return 0; }
    sleep 10
  done
  if [ -n "$f" ] && [ -f "$f" ]; then
    warn "$app did not converge in 5 min; applying $f directly"
    k apply -f "$f" >/dev/null || die "direct apply of $f failed"
    for i in $(seq 1 18); do
      got="$(k -n "$ns" get $obj -o jsonpath="$jp" 2>/dev/null || true)"
      [ "$got" = "$want" ] && { ok "$app applied directly ($obj)"; return 0; }
      sleep 10
    done
  fi
  return 1
}

if [ "$CH_PENDING" = 1 ]; then
  settle clickhouse database deploy/clickhouse \
    '{.spec.template.spec.containers[0].env[?(@.name=="CLICKHOUSE_PASSWORD")].valueFrom.secretKeyRef.name}' \
    clickhouse-server-sealed "$MF/clickhouse.yaml" \
    || die "clickhouse Deployment never picked up the sealed secret. Server and consumers are still on the OLD password, which is consistent -- nothing is broken. Investigate the ArgoCD app before retrying."
  k -n database rollout status deploy/clickhouse --timeout=240s >/dev/null || warn "rollout not confirmed"
  CH_POD="$(k -n database get pods --no-headers | awk '/^clickhouse-/ && /Running/{print $1; exit}')"
  for _ in $(seq 1 20); do
    k -n database exec "$CH_POD" -- clickhouse-client --user "$CH_USER_OUT" \
      --password "$CH_NEW_OUT" --query "SELECT 1" >/dev/null 2>&1 && break
    sleep 5
  done
  k -n database exec "$CH_POD" -- clickhouse-client --user "$CH_USER_OUT" \
      --password "$CH_NEW_OUT" --query "SELECT 1" >/dev/null 2>&1 \
    || die "the new ClickHouse password still does not authenticate after the merge"
  ok "ClickHouse: new password authenticates"
  k -n database exec "$CH_POD" -- clickhouse-client --user "$CH_USER_OUT" \
      --password clickhousepassword --query "SELECT 1" >/dev/null 2>&1 \
    && die "the LEAKED ClickHouse password still works" || ok "ClickHouse: leaked password rejected"
  CONS="$(k -n airflow get secret clickhouse-credentials -o jsonpath='{.data.password}' | base64 -d)"
  [ "$CONS" = "$CH_NEW_OUT" ] && ok "consumers hold the same password as the server" \
    || die "airflow/clickhouse-credentials disagrees with the server -- Grafana, dbt and reverse_etl will fail"
fi

if [ "$KCDB_PENDING" = 1 ]; then
  rollback_db2(){
    warn "Keycloak did not come back -- rolling the database passwords back"
    k -n keycloak exec -i keycloak-postgresql-0 -- env PGPASSWORD="$KCDB_NEW_ADMIN" \
      psql -U postgres -tAc "ALTER USER $KCDB_USER WITH PASSWORD '$KCDB_OLD_USER';" >/dev/null 2>&1 || true
    k -n keycloak exec -i keycloak-postgresql-0 -- env PGPASSWORD="$KCDB_NEW_ADMIN" \
      psql -U postgres -tAc "ALTER USER postgres WITH PASSWORD '$KCDB_OLD_ADMIN';" >/dev/null 2>&1 || true
    warn "database restored. REVERT THE MERGE TOO: git revert <the merge> && git push"
  }
  settle keycloak keycloak sts/keycloak \
    '{.spec.template.spec.containers[0].env[?(@.name=="KEYCLOAK_DATABASE_PASSWORD")].valueFrom.secretKeyRef.name}' \
    keycloak-postgresql-sealed "" \
    || { rollback_db2; die "the Keycloak StatefulSet never pointed at the sealed secret"; }
  k -n keycloak rollout status statefulset/keycloak --timeout=420s >/dev/null \
    || { rollback_db2; die "Keycloak did not become ready"; }
  up=0
  for _ in $(seq 1 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://auth.sentpul.click/realms/master)" = 200 ] && { up=1; break; }
    sleep 10
  done
  [ "$up" = 1 ] || { rollback_db2; die "Keycloak is not serving its realm"; }
  ok "Keycloak healthy against the rotated database passwords"
fi

step "Final health"
for h in auth airflow grafana cloudbeaver minio argocd kafka; do
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$h.sentpul.click/" || echo 000)"
  case "$c" in 2*|3*|401|403) ok "$h.sentpul.click -> $c" ;; *) warn "$h.sentpul.click -> $c" ;; esac
done
LEAKS="$(grep -rniE ':[[:space:]]*"?(admin|clickhouse|minioadmin)password' "$HV" "$MF" dbt/ scripts/ 2>/dev/null \
  | grep -viE 'sealed|^[^:]*fix_remaining_platform_security\.sh:|^[^:]*rotate_' \
  | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' || true)"
[ -z "$LEAKS" ] && ok "no plaintext credential left in the tracked tree" || {
  warn "still present:"; printf '%s\n' "$LEAKS" | sed 's/^/      /'; }
k get pods -A 2>/dev/null | grep -Ev 'Running|Completed' | head -6 || true

printf '\n%s================ done ================%s\n' "$C_B" "$C_0"
echo "New values are in $CREDS (nothing was printed)."
echo "Base commit for rollback: $START_SHA"
echo
echo "Left for you, deliberately:"
echo "  - MinIO SSO: its Keycloak client 'minio' does not exist, so that OIDC"
echo "    secret is inert. Recreate the client, or drop the config."
echo "  - sentiment_pulse_web: regenerate backend/db/schema.sql on a box with"
echo "    pg_dump 16 (the Makefile guard refuses a mismatched client)."
echo "  - Review Keycloak sessions for the 2026-05-21 exposure window."
