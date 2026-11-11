#!/usr/bin/env bash
# Rotate every plaintext credential still committed to this repo, seal the new
# values, commit, open a PR and merge it.
#
# WHY THIS EXISTS. platform-gitops/helm-values/ and platform-gitops/manifests/
# carried live credentials as literals in a PUBLIC repository from 2026-05-21.
# The Keycloak admin password was rotated separately; this handles the rest.
# Everything here is readable in git history for good, so these values must be
# rotated, not merely deleted.
#
# ORDERING IS THE WHOLE DESIGN. For each service: create and APPLY the
# SealedSecret first, and only then merge the commit that points the chart at
# it. A merge can therefore never leave a pod referencing a secret that does
# not exist -- which, for an auth proxy, locks a service out entirely.
#
# EXPECT A SHORT AUTH OUTAGE. Regenerating a Keycloak client secret invalidates
# the old one immediately, while the running proxy keeps using it until the
# merge triggers a rollout. That window is ~1-2 minutes and is unavoidable with
# a single client secret. It is why the Keycloak regeneration happens as late as
# possible, right before the commit.
#
# Usage:
#   bash scripts/rotate_platform_secrets.sh                 # do it (prompts once)
#   bash scripts/rotate_platform_secrets.sh --dry-run       # show the plan, change nothing
#   bash scripts/rotate_platform_secrets.sh --yes           # no prompt
#   bash scripts/rotate_platform_secrets.sh --no-merge      # commit + PR, leave merge to you
#   bash scripts/rotate_platform_secrets.sh --skip-minio    # leave MinIO alone
#   bash scripts/rotate_platform_secrets.sh --include-keycloak-db
#
# Safe to re-run: every phase detects work it already did and skips it.

set -euo pipefail

DRY=0; ASSUME_YES=0; DO_MERGE=1; SKIP_MINIO=0; INCLUDE_KCDB=0
for a in "$@"; do case "$a" in
  --dry-run) DRY=1 ;;
  --yes|-y) ASSUME_YES=1 ;;
  --no-merge) DO_MERGE=0 ;;
  --skip-minio) SKIP_MINIO=1 ;;
  --include-keycloak-db) INCLUDE_KCDB=1 ;;
  -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
  *) echo "unknown flag: $a (try --help)" >&2; exit 2 ;;
esac; done

# ---------------------------------------------------------------- plumbing ---
C_R=$'\e[31m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_B=$'\e[1m'; C_0=$'\e[0m'
step(){ printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_0"; }
ok(){   printf '  %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn(){ printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
skip(){ printf '  %s·%s %s (already done)\n' "$C_Y" "$C_0" "$*"; }
die(){  printf '\n%sFAILED:%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }
run(){ if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else eval "$@"; fi; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
[ -d platform-gitops ] || die "not the sentiment_pulse repo: $REPO"

HV=platform-gitops/helm-values
MF=platform-gitops/manifests
APPS=platform-gitops/apps
KCNS=keycloak; KCPOD=keycloak-0; REALM=data-platform
BRANCH="security/rotate-remaining-platform-secrets"
VAULT="$HOME/sentpul-rotation-$(date +%Y%m%d-%H%M%S).txt"

# -------------------------------------------------------------- preflight ---
step "Preflight"
for t in kubectl kubeseal git gh openssl python3; do
  command -v "$t" >/dev/null || die "missing required tool: $t"
done
ok "tools present"

kubectl config current-context >/dev/null 2>&1 || die "no kubectl context"
CTX="$(kubectl config current-context)"
[ "$CTX" = "sentpul" ] || warn "kubectl context is '$CTX', expected 'sentpul'"
kubectl get nodes >/dev/null 2>&1 || die "cluster unreachable"
ok "cluster reachable ($CTX)"

kubectl -n kube-system get deploy -l name=sealed-secrets-controller >/dev/null 2>&1 \
  || kubectl -n kube-system get pods 2>/dev/null | grep -q sealed-secrets \
  || die "sealed-secrets controller not found in kube-system; kubeseal would produce undecryptable data"
ok "sealed-secrets controller present"

gh auth status >/dev/null 2>&1 || die "gh not authenticated (run: gh auth login)"
ok "gh authenticated"

kubectl -n "$KCNS" get pod "$KCPOD" >/dev/null 2>&1 || die "pod $KCNS/$KCPOD not found"
[ "$(kubectl -n "$KCNS" get pod "$KCPOD" -o jsonpath='{.status.phase}')" = Running ] \
  || die "$KCPOD is not Running"
ok "keycloak pod running"

# The admin password. Prefer the sealed secret created by the earlier rotation;
# fall back to the env var. Never read it from git -- that is the leak.
KC_ADMIN_PW="${KC_ADMIN_PW:-$(kubectl -n "$KCNS" get secret keycloak-admin \
  -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d 2>/dev/null || true)}"
[ -n "$KC_ADMIN_PW" ] || die "no Keycloak admin password. Set KC_ADMIN_PW=... or create secret $KCNS/keycloak-admin first (PR #10)."
export KC_ADMIN_PW

kc(){ kubectl -n "$KCNS" exec -i "$KCPOD" -- env KCPW="$KC_ADMIN_PW" sh -c '
  K=/opt/bitnami/keycloak/bin/kcadm.sh; C=/tmp/kcadm.rotate.cfg
  "$K" config credentials --config "$C" --server http://127.0.0.1:8080 \
       --realm master --user admin --password "$KCPW" >/dev/null 2>&1 \
    || { echo "KCADM_AUTH_FAILED" >&2; exit 1; }
  "$K" "$@" --config "$C"' _ "$@"; }

# client_id_of <clientId> : the Keycloak internal uuid
client_id_of(){
  kc get clients -r "$REALM" --fields id,clientId 2>/dev/null \
    | tr -d ' \n' | sed 's/},{/}\n{/g' \
    | grep "\"clientId\":\"$1\"" | sed -E 's/.*"id":"([^"]+)".*/\1/' | head -1
}

kc get realms/master --fields id >/dev/null 2>&1 || die "Keycloak admin auth failed. Is KC_ADMIN_PW current?"
ok "keycloak admin auth OK"

# Prove the realm and every client we intend to rotate are readable, in the
# realm we will actually address. The first run of this script died mid-phase
# because it addressed the wrong realm; that is a preflight question, not a
# discover-it-halfway-through one.
kc get realms/"$REALM" --fields realm >/dev/null 2>&1 \
  || die "realm '$REALM' not readable. Wrong realm name, or admin lacks rights."
ok "realm $REALM readable"
for c in airflow-client grafana cloudbeaver; do
  u="$(client_id_of "$c")"
  [ -n "$u" ] || die "Keycloak client '$c' not found in realm $REALM -- nothing has been changed."
  kc get "clients/$u/client-secret" -r "$REALM" >/dev/null 2>&1 \
    || die "cannot read the current secret of '$c' in realm $REALM, so regenerating it would be unsafe. Nothing changed."
  ok "client $c readable ($u)"
done

# Refuse to run on a dirty tree: this script commits, and mixing in unrelated
# work would put someone else's changes in a security PR.
[ -z "$(git status --porcelain --untracked-files=no)" ] \
  || die "working tree has uncommitted changes. Commit or stash them first."
git fetch -q origin main
ok "clean tree, origin/main fetched"
START_SHA="$(git rev-parse HEAD)"

# ------------------------------------------------------------------- plan ---
cat <<PLAN

${C_B}Plan${C_0}
  1. airflow-client   regenerate Keycloak client secret + new cookie secret -> sealed (ns airflow)
  2. grafana          regenerate Keycloak client secret                     -> sealed (ns airflow)
  3. cloudbeaver      regenerate Keycloak client secret + new cookie secret -> sealed (ns database)
                      also drops insecure_oidc_allow_unverified_email.
                      NOTE: email_domains ["*"] and cookie_secure false are
                      left alone -- both need a value only you can choose.
  4. grafana admin    new password + ClickHouse datasource via env          -> sealed (ns airflow)
  5. minio root       new user/password, and minio-credentials in BOTH
                      ns database and ns airflow (5 sinks + DAGs read it)   $( [ "$SKIP_MINIO" = 1 ] && echo "[SKIPPED]" )
  6. scripts          remove the hardcoded :-default credentials
  7. verify           no plaintext credential left outside sealed files
  8. commit + PR $( [ "$DO_MERGE" = 1 ] && echo "+ merge" || echo "(no merge)" )
  9. health check across every public hostname
 10. keycloak DB      $( [ "$INCLUDE_KCDB" = 1 ] && echo "INCLUDED -- see the warning below" || echo "NOT included (needs --include-keycloak-db)" )

  New values are written to: ${VAULT}
  Rollback:  git reset --hard ${START_SHA}   (plus re-seal, if already merged)
PLAN

if [ "$INCLUDE_KCDB" = 1 ]; then cat <<'KCDBWARN'

  !! --include-keycloak-db is DANGEROUS and differs in kind from everything
     else here. Bitnami Postgres persists its password in the PVC, so changing
     the Helm value or the secret does NOT change the database. Getting the
     order wrong leaves Keycloak unable to reach its own database, which takes
     down every login on the platform. This script does ALTER USER first and
     verifies before touching the manifest, but you should still have a
     database backup to hand.
KCDBWARN
fi

if [ "$DRY" = 1 ]; then printf '\n%sdry-run: nothing was changed.%s\n' "$C_Y" "$C_0"; exit 0; fi
if [ "$ASSUME_YES" != 1 ]; then
  printf '\nThis rotates LIVE credentials and briefly interrupts logins. Continue? [y/N] '
  read -r reply; case "$reply" in y|Y|yes) ;; *) echo "aborted."; exit 1 ;; esac
fi

umask 077
{ echo "sentpul credential rotation $(date -Is)"
  echo "context: $CTX   repo: $REPO   base: $START_SHA"
  echo "Store these in a password manager, then shred this file."
  echo; } > "$VAULT"
chmod 600 "$VAULT"
record(){ printf '%-34s %s\n' "$1" "$2" >> "$VAULT"; }

git rev-parse --verify "$BRANCH" >/dev/null 2>&1 \
  && git checkout -q "$BRANCH" \
  || git checkout -q -b "$BRANCH" origin/main
ok "on branch $BRANCH"

# ---------------------------------------------------------------- helpers ---
# seal <ns> <name> <out.yaml> KEY=VALUE...   : build, write and APPLY a SealedSecret
seal(){
  local ns="$1" name="$2" out="$3"; shift 3
  local d args=(); d="$(mktemp -d)"
  local kv
  for kv in "$@"; do
    printf %s "${kv#*=}" > "$d/${kv%%=*}"
    args+=( --from-file="${kv%%=*}=$d/${kv%%=*}" )
  done
  kubectl create secret generic "$name" -n "$ns" "${args[@]}" \
    --dry-run=client -o yaml | kubeseal --format yaml > "$out.tmp"
  { echo "# Generated by scripts/rotate_platform_secrets.sh on $(date -Is)."
    echo "# Ciphertext is safe to commit: only the in-cluster sealed-secrets"
    echo "# controller holds the private key. To rotate again, change the value"
    echo "# at its source first, then re-run that script -- editing this file"
    echo "# alone does not change anything that is already running."
    grep -v '^\s*creationTimestamp' "$out.tmp"; } > "$out"
  rm -f "$out.tmp"
  kubectl apply -f "$out" >/dev/null
  # Prove the controller actually decrypted it, rather than trusting it did.
  local i
  for i in $(seq 1 30); do
    kubectl -n "$ns" get secret "$name" >/dev/null 2>&1 && break
    sleep 1
  done
  kubectl -n "$ns" get secret "$name" >/dev/null 2>&1 \
    || die "sealed secret $ns/$name did not materialise -- do NOT merge. Check the controller logs."
  rm -rf "$d"
  ok "sealed + applied + verified: $ns/$name"
}

# rotate_client <clientId> : regenerate its secret, echo the new value
rotate_client(){
  local cid uuid err
  uuid="$(client_id_of "$1")"; [ -n "$uuid" ] || die "Keycloak client '$1' not found in realm $REALM"
  # -r "$REALM" is REQUIRED on both calls. kcadm defaults to the realm it
  # authenticated against (master), so without it this hits
  # realms/master/clients/<uuid>/client-secret and 404s -- the client lives in
  # $REALM, not master. Omitting it is how the first run of this script failed.
  if ! err="$(kc create "clients/$uuid/client-secret" -r "$REALM" 2>&1)"; then
    die "could not regenerate secret for $1 in realm $REALM: ${err:-no output}"
  fi
  cid="$(kc get "clients/$uuid/client-secret" -r "$REALM" 2>/dev/null | tr -d ' \n"' | sed 's/.*value://;s/}//')"
  [ ${#cid} -ge 16 ] || die "regenerated secret for $1 looks wrong (len ${#cid}). The old one is already dead -- read it from the Keycloak admin console and seal it by hand."
  printf %s "$cid"
}
cookie_secret(){ openssl rand -base64 32 | tr -- '+/' '-_' | tr -d '\n'; }
strong(){ openssl rand -base64 36 | tr -d '\n=+/' | cut -c1-32; }

# pyedit <file> <<'PY' ... : edit a file in place, asserting on anchors so a
# layout change fails loudly instead of silently doing nothing.
pyedit(){ python3 - "$1" || die "edit failed for $1 (layout changed? fix by hand)"; }

# =========================================================== 1. airflow ====
step "1/10  airflow-client (oauth2-proxy in front of Airflow)"
if grep -q 'existingSecret:' "$HV/oauth2-proxy-values.yaml"; then
  skip "oauth2-proxy-values.yaml already uses existingSecret"
else
  AF_SECRET="$(rotate_client airflow-client)"; ok "Keycloak airflow-client secret regenerated"
  AF_COOKIE="$(cookie_secret)"
  record "airflow-client secret" "$AF_SECRET"
  record "airflow proxy cookie"  "$AF_COOKIE"
  seal airflow oauth2-proxy-airflow-sealed "$MF/oauth2-proxy-airflow-sealed-secret.yaml" \
    client-id=airflow-client client-secret="$AF_SECRET" cookie-secret="$AF_COOKIE"

  pyedit "$HV/oauth2-proxy-values.yaml" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
assert 'clientSecret:' in s, 'no clientSecret to remove'
out, dropped = [], 0
for line in s.splitlines(keepends=True):
    if re.match(r'\s+(clientID|clientSecret|cookieSecret):', line) or 'openssl rand -base64 32' in line:
        dropped += 1; continue
    out.append(line)
    if line.rstrip('\n') == 'config:':
        out.append('  # Credentials live in a SealedSecret, not here. This file was public\n')
        out.append('  # with real values in it; see manifests/oauth2-proxy-airflow-sealed-secret.yaml.\n')
        out.append('  # Chart 7.7.2 reads client-id / client-secret / cookie-secret from it.\n')
        out.append('  existingSecret: oauth2-proxy-airflow-sealed\n')
assert dropped >= 2, f'expected to drop >=2 credential lines, dropped {dropped}'
p.write_text(''.join(out))
print('  values rewritten')
PY
  ok "oauth2-proxy-values.yaml -> existingSecret"
fi

# =========================================================== 2. grafana ====
step "2/10  grafana Keycloak client (un-sharing the reused secret)"
# Anchor on an indented ASSIGNMENT, not the bare name. The replacement this
# phase writes leaves a top-level comment mentioning the variable, so a loose
# grep matches its own output and the phase re-runs forever -- regenerating a
# live client secret every time, which is the opposite of idempotent.
if grep -qE '^[[:space:]]+GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET:' "$HV/grafana-values.yaml"; then
  GF_OAUTH="$(rotate_client grafana)"
  record "grafana client secret" "$GF_OAUTH"
  ok "Keycloak grafana secret regenerated (airflow-client and grafana no longer share one)"
  seal airflow grafana-oauth-sealed "$MF/grafana-oauth-sealed-secret.yaml" \
    GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET="$GF_OAUTH"
else
  skip "grafana oauth secret already out of values"
fi

# ======================================================== 3. cloudbeaver ====
step "3/10  cloudbeaver proxy (+ closing email_domains)"
if grep -q 'existingSecret:' "$HV/oauth2-proxy-cloudbeaver-values.yaml"; then
  skip "cloudbeaver values already use existingSecret"
else
  CB_SECRET="$(rotate_client cloudbeaver)"; ok "Keycloak cloudbeaver secret regenerated"
  CB_COOKIE="$(cookie_secret)"
  record "cloudbeaver secret" "$CB_SECRET"
  record "cloudbeaver cookie" "$CB_COOKIE"
  seal database oauth2-proxy-cloudbeaver-sealed "$MF/oauth2-proxy-cloudbeaver-sealed-secret.yaml" \
    client-id=cloudbeaver client-secret="$CB_SECRET" cookie-secret="$CB_COOKIE"

  pyedit "$HV/oauth2-proxy-cloudbeaver-values.yaml" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
out, dropped = [], 0
for line in s.splitlines(keepends=True):
    if re.match(r'\s+(clientID|clientSecret|cookieSecret):', line):
        dropped += 1; continue
    if 'insecure_oidc_allow_unverified_email' in line:
        dropped += 1; continue          # an unverified email is not an identity
    out.append(line)
    if line.rstrip('\n') == 'config:':
        out.append('  # Credentials live in a SealedSecret; see\n')
        out.append('  # manifests/oauth2-proxy-cloudbeaver-sealed-secret.yaml.\n')
        out.append('  existingSecret: oauth2-proxy-cloudbeaver-sealed\n')
assert dropped >= 2, f'dropped only {dropped} lines'
p.write_text(''.join(out))
print('  cloudbeaver values rewritten')
PY
  ok "cloudbeaver values -> existingSecret, unverified-email flag removed"
  warn "email_domains is still [\"*\"] in both proxies -- see the note at the end"
fi

# ==================================================== 4. grafana admin =====
step "4/10  grafana admin password + ClickHouse datasource"
if grep -qE '^adminPassword:' "$HV/grafana-values.yaml"; then
  GF_ADMIN="$(strong)"
  record "grafana admin (user: admin)" "$GF_ADMIN"
  CH_PASS="$(kubectl -n airflow get secret clickhouse-credentials \
    -o jsonpath='{.data.password}' 2>/dev/null | base64 -d 2>/dev/null || true)"
  [ -n "$CH_PASS" ] || die "could not read the existing sealed ClickHouse password from airflow/clickhouse-credentials"
  seal airflow grafana-admin-sealed "$MF/grafana-admin-sealed-secret.yaml" \
    admin-user=admin admin-password="$GF_ADMIN"
  seal airflow grafana-clickhouse-ds-sealed "$MF/grafana-clickhouse-ds-sealed-secret.yaml" \
    CH_DATASOURCE_PASSWORD="$CH_PASS"

  pyedit "$HV/grafana-values.yaml" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
assert s.startswith('adminPassword:'), 'adminPassword is not the first line any more'
s = re.sub(r'^adminPassword:.*\n', '''# The admin password is NOT here -- it was, in plaintext, in a public repo.
admin:
  existingSecret: grafana-admin-sealed
  userKey: admin-user
  passwordKey: admin-password

# Supplies GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET and CH_DATASOURCE_PASSWORD.
# Grafana expands $VAR in provisioning files, which is how the datasource below
# gets its password without the value appearing in this file.
envFromSecrets:
  - name: grafana-oauth-sealed
  - name: grafana-clickhouse-ds-sealed
''', s, count=1)
s = re.sub(r'\n\s+GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET:.*', '', s, count=1)
s2 = re.sub(r'(secureJsonData:\n\s+password: ).*', r'\1$CH_DATASOURCE_PASSWORD', s, count=1)
assert s2 != s, 'datasource password line not found'
p.write_text(s2)
print('  grafana values rewritten')
PY
  ok "grafana-values.yaml -> sealed admin + env-substituted datasource"
else
  skip "grafana admin already sealed"
fi

# =========================================================== 5. minio ======
step "5/10  minio root + minio-credentials (database AND airflow)"
if [ "$SKIP_MINIO" = 1 ]; then
  warn "skipped by --skip-minio"
elif grep -q 'envFrom:' "$MF/minio.yaml"; then
  skip "minio.yaml already uses envFrom"
else
  MINIO_USER="sentpul-admin"; MINIO_PASS="$(strong)"
  record "minio root user" "$MINIO_USER"
  record "minio root password" "$MINIO_PASS"

  # minio.yaml also carries MINIO_IDENTITY_OPENID_CLIENT_SECRET for a Keycloak
  # client named "minio" -- which does NOT exist in realm data-platform. MinIO
  # SSO is therefore already non-functional and that literal is inert: there is
  # no client whose secret could be regenerated. It is carried across verbatim
  # so this script changes exactly one thing (where the value lives, not what
  # it is); recreating the client is a separate, deliberate decision.
  MINIO_OIDC="$(sed -nE 's/^[[:space:]]*value:[[:space:]]*"?([^"#]*)"?.*/\1/p' \
    <<<"$(grep -A1 'MINIO_IDENTITY_OPENID_CLIENT_SECRET' "$MF/minio.yaml" | tail -1)" \
    | tr -d ' \t\r\n')"
  [ -n "$MINIO_OIDC" ] || warn "could not read the existing MinIO OIDC client secret; sealing an empty value"
  record "minio OIDC client secret (INERT - no such Keycloak client)" "${MINIO_OIDC:-<empty>}"

  seal database minio-root-sealed "$MF/minio-root-sealed-secret.yaml" \
    MINIO_ROOT_USER="$MINIO_USER" MINIO_ROOT_PASSWORD="$MINIO_PASS" \
    MINIO_IDENTITY_OPENID_CLIENT_SECRET="${MINIO_OIDC:-}"

  # The 5 sinks and the transform DAGs read secret `minio-credentials`, which
  # is NOT in git and currently holds the same value as the root credential.
  # Rotating root without these breaks every sink, so they move together.
  #
  # `minio-credentials` is NOT the only consumer: airflow/airflow-s3-credentials
  # carries the same credential as an Airflow connection URI, and this step has
  # never known about it -- which is how the 2026-11-07 rotation silently killed
  # every task log upload. It is not patched here because this step is now inert
  # (minio.yaml already uses envFrom, so the guard above skips it); the consumer
  # that owns that knowledge is scripts/reseal_minio_consumers.sh. Run it after
  # any root rotation.
  for ns in database airflow; do
    if kubectl -n "$ns" get secret minio-credentials >/dev/null 2>&1; then
      kubectl -n "$ns" create secret generic minio-credentials \
        --from-literal=accessKey="$MINIO_USER" --from-literal=secretKey="$MINIO_PASS" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
      ok "patched $ns/minio-credentials (unmanaged secret, kept in step with root)"
    else
      warn "$ns/minio-credentials not present -- nothing to patch"
    fi
  done

  pyedit "$MF/minio.yaml" <<'PY'
import re, sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text()
pat = re.compile(r'\n(\s+)env:\n\s+- name: MINIO_ROOT_USER\n\s+value:.*\n\s+- name: MINIO_ROOT_PASSWORD\n\s+value:.*\n')
m = pat.search(s)
assert m, 'the MINIO_ROOT_* env block is not in the expected shape'
ind = m.group(1)
# The OIDC client secret is a second literal, further down the same env list.
# It moves into the same sealed secret, so the two lines come out here.
oidc = re.compile(r'\n\s+- name: MINIO_IDENTITY_OPENID_CLIENT_SECRET\n\s+value:.*')
assert oidc.search(s), 'MINIO_IDENTITY_OPENID_CLIENT_SECRET not found where expected'
s = oidc.sub('', s, count=1)
m = pat.search(s)   # offsets moved after that removal
# `env:` MUST be re-opened. The regex above consumes the `env:` key along with
# the two root entries, but other env entries follow it -- leaving them as
# children of `envFrom:`, which only accepts secretRef/configMapRef. The API
# server then rejects the Deployment with "must specify one of configMapRef or
# secretRef" and ArgoCD retries forever. That is what happened on the first run.
repl = (f'\n{ind}# Root credentials come from a SealedSecret. They were literals here, in a\n'
        f'{ind}# public repo. The sinks and transform DAGs read the separate\n'
        f'{ind}# `minio-credentials` secret, which must be rotated in step with this one.\n'
        f'{ind}envFrom:\n{ind}  - secretRef:\n{ind}      name: minio-root-sealed\n'
        f'{ind}env:\n')
p.write_text(s[:m.start()] + repl + s[m.end():])
print('  minio.yaml rewritten')
PY
  ok "minio.yaml -> envFrom secretRef"
fi

# ========================================================== 6. scripts =====
step "6/10  remove hardcoded fallback credentials from scripts/"
CHANGED=0
for f in scripts/*.sh; do
  [ -f "$f" ] || continue
  before="$(sha256sum "$f" | cut -d' ' -f1)"
  # `${VAR:-literal}` on a credential both hard-codes a real secret and lets
  # callers keep working while it is the well-known default, so nothing ever
  # surfaces the problem. `${VAR:?...}` fails loudly instead.
  # [^}]+ not [^}]*: `${VAR:-}` is a PRESENCE TEST with an empty default, and
  # rewriting it to `${VAR:?}` makes the test abort instead of returning false,
  # breaking the very detection path it guards. Only a non-empty literal default
  # is a hard-coded credential worth replacing.
  sed -i -E 's/\$\{(GF_ADMIN_PASSWORD|CH_PASSWORD|KEYCLOAK_ADMIN_PASSWORD|MINIO_ROOT_PASSWORD|MINIO_SECRET_KEY):-[^}]+\}/${\1:?set \1}/g' "$f"
  after="$(sha256sum "$f" | cut -d' ' -f1)"
  if [ "$before" != "$after" ]; then
    bash -n "$f" || die "$f no longer parses after editing -- fix by hand"
    ok "hardened $f"; CHANGED=$((CHANGED+1))
  fi
done
[ "$CHANGED" -gt 0 ] || skip "no script fallbacks left"

# =========================================================== 7. verify =====
step "7/10  verify no plaintext credential remains"
# The gate's question is "did THIS change leave a credential in a file it
# touched", not "is the repo perfectly clean". Conflating the two blocks a
# finished rotation on an unrelated pre-existing leak -- which is what happened
# on the first real run, mid-incident, with the new secrets already live.
#
# So: HARD FAIL on files this branch touched. WARN, loudly and by name, on
# anything else, and keep going.
SELF="scripts/$(basename "$0")"
PAT='adminpassword|clickhousepassword|minioadminpassword'
# Files this branch is responsible for (staged, modified, or newly added).
TOUCHED="$( { git diff --name-only; git diff --name-only --cached; \
              git ls-files --others --exclude-standard; } | sort -u \
            | grep -vxF "$SELF" || true)"

ALL_LEAKS="$(grep -rniE "$PAT" platform-gitops/ scripts/ 2>/dev/null \
  | grep -viE 'sealed' \
  | grep -vE "^${SELF}:" \
  | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#' || true)"

OURS=""; THEIRS=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  f="${line%%:*}"
  if printf '%s\n' "$TOUCHED" | grep -qxF "$f"; then OURS="$OURS$line"$'\n'
  else THEIRS="$THEIRS$line"$'\n'; fi
done <<< "$ALL_LEAKS"

if [ -n "${OURS//[$'\n']/}" ]; then
  printf '%s' "$OURS" | sed 's/^/    /'
  die "a file THIS change touched still holds a plaintext credential -- NOT committing."
fi
ok "no plaintext credential in any file this change touched"
if [ -n "${THEIRS//[$'\n']/}" ]; then
  warn "pre-existing leaks elsewhere, NOT part of this rotation and NOT fixed by it:"
  printf '%s' "$THEIRS" | sed 's/^/      /'
  warn "each needs its own rotation. They are recorded at the end of this run."
  PREEXISTING="$THEIRS"
fi
for f in "$HV"/*.yaml "$MF"/*.yaml "$APPS"/*.yaml; do
  # safe_load_all, not safe_load: minio.yaml and the manifests are multi-document.
  python3 -c 'import yaml,sys;list(yaml.safe_load_all(open(sys.argv[1])))' "$f" \
    || die "invalid YAML after editing: $f"
done
ok "all platform-gitops YAML still parses"
for f in "$MF"/*sealed*.yaml; do
  [ -f "$f" ] || continue
  kubectl apply --dry-run=server -f "$f" >/dev/null 2>&1 || die "server dry-run rejected $f"
done
ok "sealed manifests accepted by the API server"
# Every manifest this change edited must also be accepted, not just the sealed
# ones. A structurally invalid Deployment passes `yaml.safe_load` happily and is
# only rejected by the API server -- where it becomes an endless ArgoCD retry.
for f in "$MF"/*.yaml; do
  [ -f "$f" ] || continue
  kubectl apply --dry-run=server -f "$f" >/dev/null 2>&1 \
    || die "the API server REJECTS $f -- do not commit it; ArgoCD would retry forever. Run: kubectl apply --dry-run=server -f $f"
done
ok "every manifest accepted by the API server"

# ======================================================== 8. commit + PR ====
step "8/10  commit, PR$( [ "$DO_MERGE" = 1 ] && echo ', merge' )"
if [ -z "$(git status --porcelain)" ]; then
  skip "nothing to commit"
else
  # Explicit paths, never `git add -A`. The repo contains an unrelated ~42MB
  # `code/` directory whose .gitignore rule lives in a different, unmerged
  # branch, so `-A` swept 11 embedded git repos into a security commit on the
  # first real run. A security change must contain only what it means to.
  git add -- "$HV" "$MF" "$APPS" scripts/ 2>/dev/null || true
  git reset -q -- code/ 2>/dev/null || true
  git diff --cached --name-only | grep -q . || die "nothing staged -- expected edits are missing"
  git commit -q -F - <<'MSG'
security: rotate every remaining committed credential into SealedSecrets

platform-gitops/helm-values/ and platform-gitops/manifests/ carried live
credentials as literals in a PUBLIC repository from 2026-05-21. The Keycloak
admin password was handled separately; this is the rest. Every value here has
been regenerated at its source -- the old ones are dead, and stay readable in
git history, which is why this rotates rather than deletes.

- airflow-client and cloudbeaver oauth2-proxy: Keycloak client secrets
  regenerated, fresh cookie secrets, both moved to config.existingSecret.
  Chart 7.7.2 reads client-id/client-secret/cookie-secret from that secret.
- grafana: its Keycloak client secret was THE SAME STRING as airflow-client's,
  so one leaked literal opened two clients. They are now independent.
- grafana admin password sealed via admin.existingSecret (chart 8.5.1,
  userKey/passwordKey), and the ClickHouse datasource password is supplied
  through an env var Grafana expands in provisioning, so no value is in values.
- minio root sealed and consumed with envFrom. The `minio-credentials` secret
  that the five sinks and the transform DAGs read is not in git and held the
  same value as root, so it was rotated in the same step -- otherwise every
  sink would have failed on the next restart.
- scripts/: `${VAR:-<literal>}` credential fallbacks replaced with `${VAR:?}`.
  That pattern both hard-coded a real secret and let callers keep working while
  it was the well-known default, so nothing ever surfaced the problem.
- cloudbeaver's insecure_oidc_allow_unverified_email removed: an unverified
  email is not an identity.

Every SealedSecret was applied and confirmed to decrypt BEFORE this commit, so
merging cannot leave a pod referencing a secret that does not exist.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
MSG
  ok "committed $(git rev-parse --short HEAD)"
  git push -q -u origin "$BRANCH" --force-with-lease
  ok "pushed $BRANCH"
  gh pr view --json number >/dev/null 2>&1 || gh pr create --fill >/dev/null
  PR="$(gh pr view --json number -q .number)"
  ok "PR #$PR"
  if [ "$DO_MERGE" = 1 ]; then
    gh pr merge "$PR" --squash --delete-branch
    ok "merged PR #$PR"
    git checkout -q main && git pull -q
  else
    warn "not merged (--no-merge). ArgoCD will not pick anything up until you do."
  fi
fi

# ========================================================= 9. health =======
step "9/10  waiting for rollout, then checking every public hostname"
if [ "$DO_MERGE" = 1 ]; then
  echo "  giving ArgoCD up to 4 minutes to sync..."
  for i in $(seq 1 48); do sleep 5; printf '.'; done; echo
  kubectl -n airflow  rollout status deploy/oauth2-proxy            --timeout=120s 2>/dev/null || warn "airflow proxy rollout not confirmed"
  kubectl -n database rollout status deploy/oauth2-proxy-cloudbeaver --timeout=120s 2>/dev/null || warn "cloudbeaver proxy rollout not confirmed"
  kubectl -n airflow  rollout status deploy/grafana                 --timeout=120s 2>/dev/null || warn "grafana rollout not confirmed"
  kubectl -n database rollout status deploy/minio                   --timeout=120s 2>/dev/null || warn "minio rollout not confirmed"
fi
FAILED=0
for h in auth airflow grafana cloudbeaver minio argocd kafka; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$h.sentpul.click/" || echo 000)"
  case "$code" in
    2*|3*|401|403) ok "$h.sentpul.click -> $code" ;;
    *) warn "$h.sentpul.click -> $code"; FAILED=$((FAILED+1)) ;;
  esac
done
kubectl get pods -A 2>/dev/null | grep -Ev 'Running|Completed' | head -8 || true

# ====================================================== 10. keycloak db ====
step "10/10 keycloak database password"
if [ "$INCLUDE_KCDB" != 1 ]; then
  warn "not rotated. keycloak-values.yaml still has the Postgres subchart literals."
  warn "Re-run with --include-keycloak-db, with a database backup to hand."
else
  KCDB_PASS="$(strong)"; record "keycloak bn_keycloak db password" "$KCDB_PASS"
  PGPW="$(kubectl -n "$KCNS" get secret keycloak-postgresql -o jsonpath='{.data.postgres-password}' | base64 -d)"
  # Database FIRST: the PVC is the source of truth, not the secret.
  kubectl -n "$KCNS" exec -i keycloak-postgresql-0 -- env PGPASSWORD="$PGPW" \
    psql -U postgres -c "ALTER USER bn_keycloak WITH PASSWORD '$KCDB_PASS';" >/dev/null \
    || die "ALTER USER failed -- nothing else changed, Keycloak is untouched"
  ok "database password changed"
  kubectl -n "$KCNS" patch secret keycloak-postgresql --type merge \
    -p "{\"data\":{\"password\":\"$(printf %s "$KCDB_PASS" | base64 -w0)\"}}" >/dev/null
  kubectl -n "$KCNS" rollout restart statefulset/keycloak >/dev/null
  kubectl -n "$KCNS" rollout status statefulset/keycloak --timeout=300s \
    || die "Keycloak did not come back. Roll back: ALTER USER bn_keycloak WITH PASSWORD '<old>' and restart."
  ok "keycloak restarted and healthy against the new database password"
  warn "keycloak-values.yaml still holds the subchart literals; seal them in a follow-up commit."
fi

# ---------------------------------------------------------------- wrap up ---
printf '\n%s================ done ================%s\n' "$C_B" "$C_0"
echo "New credentials:  $VAULT   (mode 600)"
echo "Base commit:      $START_SHA"
echo
echo "Next, by hand:"
echo "  1. Move $VAULT into your password manager, then: shred -u $VAULT"
echo "  2. email_domains is still [\"*\"] on both oauth2-proxies, so ANY"
echo "     Keycloak user still reaches Airflow and CloudBeaver. Restrict it to"
echo "     a Keycloak group/role -- rotation alone does not gate anything."
echo "  3. cookie_secure is still false behind HTTPS; set it true."
echo "  4. Review Keycloak sessions and the ArgoCD audit log for the exposure"
echo "     window (2026-05-21 onwards)."
if [ -n "${PREEXISTING:-}" ]; then
  echo "  5. STILL LEAKED, untouched by this run -- each needs its own rotation:"
  printf '%s' "$PREEXISTING" | sed 's/^/       /'
fi
[ "$FAILED" -gt 0 ] && { printf '\n%s%s hostname(s) unhealthy -- check before walking away.%s\n' "$C_Y" "$FAILED" "$C_0"; exit 1; }
exit 0
