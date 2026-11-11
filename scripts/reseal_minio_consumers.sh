#!/usr/bin/env bash
# Reconcile the MinIO CONSUMER secrets with the root credentials MinIO actually
# runs on.
#
# WHY THIS EXISTS. MinIO here has exactly ONE identity: root. `mc admin user
# list` and `mc admin user svcacct list` are both empty, so every client
# authenticates with the root key and a root rotation invalidates all of them at
# once.
#
# rotate_platform_secrets.sh step 5 knows that for the sinks: it patches
# `minio-credentials` in ns database AND ns airflow in step with the root it
# seals. It does NOT know about airflow/airflow-s3-credentials -- the Airflow
# connection URI behind AIRFLOW_CONN_AWS_DEFAULT and AIRFLOW_CONN_DATALAKE_MINIO,
# a hand-made plaintext Secret from 2026-05-26 that no manifest and no
# Application ever owned. The 2026-11-07 rotation therefore left it on a dead
# access key, and because remote task logging writes to s3://airflow/logs
# through that connection, EVERY task log upload has failed with
# InvalidAccessKeyId since: no DAG wrote a log anywhere after 11-07, while
# ArgoCD reported Synced and the DAGs themselves kept running. The platform's
# whole debugging surface, gone, because one consumer of a rotated credential
# was owned by nothing.
#
# So, exactly like reseal_clickhouse_consumers.sh, this script never invents a
# credential: it reads the LIVE root secret and derives the consumers from THAT.
# One source of truth, drift impossible by construction.
#
# It also seals the result, with sealedsecrets.bitnami.com/managed: "true" so
# the controller adopts the existing hand-made Secret in place rather than
# refusing to touch a Secret it does not own. The secret keeps its name, so
# nothing in airflow-values.yaml or the worker pod template has to change.
#
# Run it after any MinIO root rotation, or any time task logs stop appearing.
# Idempotent and safe to repeat.
#
# ORDERING. The sealed secret is applied here so logs resume at once, but the
# generated manifest and its Application must be COMMITTED AND MERGED, or the
# cluster is once again holding a credential that git does not describe -- which
# is the bug this script exists to clean up.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
MF=platform-gitops/manifests
OUT="$MF/airflow-s3-sealed-credentials.yaml"
# The in-cluster endpoint, not the public tunnel: these clients are pods.
ENDPOINT="http://minio.database.svc.cluster.local:9000"
# The bucket remote logging writes to (airflow.cfg: remote_base_log_folder).
LOG_BUCKET=airflow

k(){ kubectl --context sentpul "$@"; }
# kubeseal fetches the sealing CERTIFICATE from the controller, so it needs the
# context too. Bare `kubeseal` uses whatever context happens to be current --
# seal against the wrong cluster and the ciphertext is one this controller can
# never decrypt, which shows up later as a SealedSecret that silently does
# nothing rather than as an error here.
seal_stdin(){ kubeseal --context sentpul --format yaml; }
C_G=$'\e[32m'; C_Y=$'\e[33m'; C_0=$'\e[0m'
ok(){ printf '  %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn(){ printf '  %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die(){ printf '\n%sFAILED:%s %s\n' $'\e[31m' "$C_0" "$*" >&2; exit 1; }

for t in kubectl kubeseal python3; do command -v $t >/dev/null || die "missing tool: $t"; done
k get nodes >/dev/null 2>&1 || die "kube context 'sentpul' unreachable"

d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT; umask 077

# ---------------------------------------------------------- source of truth --
for key in MINIO_ROOT_USER MINIO_ROOT_PASSWORD; do
  k -n database get secret minio-root-sealed -o "jsonpath={.data.$key}" | base64 -d > "$d/$key"
  [ -s "$d/$key" ] || die "database/minio-root-sealed holds no $key"
done
ok "read the live root credentials (user sha $(sha256sum < "$d/MINIO_ROOT_USER" | cut -c1-10), password sha $(sha256sum < "$d/MINIO_ROOT_PASSWORD" | cut -c1-10))"

# Prove the source of truth is what MinIO actually accepts BEFORE deriving
# anything from it. A sealed secret and a running server can disagree, and that
# disagreement is the whole bug class this script addresses.
pod="$(k -n database get pods -l app=minio -o jsonpath='{.items[0].metadata.name}')"
[ -n "$pod" ] || die "no running MinIO pod in ns database"
cat "$d/MINIO_ROOT_USER" "$d/MINIO_ROOT_PASSWORD" \
  | k -n database exec -i "$pod" -- sh -c '
      IFS= read -r u; IFS= read -r p
      # MC_HOST_* rather than `mc alias set`, so the credential never reaches argv.
      MC_HOST_loc="http://$u:$p@127.0.0.1:9000" mc ls "loc/'"$LOG_BUCKET"'" >/dev/null' \
  || die "the live root credentials do NOT authenticate against MinIO -- rotate the root first, there is nothing sound to derive from"
ok "live root credentials authenticate against MinIO and can read bucket '$LOG_BUCKET'"

# ------------------------------------------------------ derive the consumer --
# Percent-encoded, because a password containing '@', '/' or ':' would otherwise
# silently produce a URI that parses into the wrong credential. strong() in
# rotate_platform_secrets.sh happens to emit alphanumerics today; this does not
# depend on that staying true.
python3 - "$d" "$ENDPOINT" <<'PY'
import pathlib, sys
from urllib.parse import quote, urlencode
d, endpoint = pathlib.Path(sys.argv[1]), sys.argv[2]
user = d.joinpath("MINIO_ROOT_USER").read_text()
password = d.joinpath("MINIO_ROOT_PASSWORD").read_text()
uri = "aws://{}:{}@?{}".format(
    quote(user, safe=""), quote(password, safe=""),
    urlencode({"endpoint_url": endpoint}),
)
# No trailing newline: the bytes of this file ARE the secret value.
d.joinpath("connectionUri").write_text(uri)
PY
[ -s "$d/connectionUri" ] || die "failed to build the connection URI"
ok "built the Airflow connection URI ($(wc -c < "$d/connectionUri") bytes, endpoint $ENDPOINT)"

# ------------------------------------------------------------------- seal it --
k create secret generic airflow-s3-credentials -n airflow \
    --from-file="connectionUri=$d/connectionUri" --dry-run=client -o yaml \
  | k annotate --local -f - sealedsecrets.bitnami.com/managed=true -o yaml \
  | seal_stdin > "$d/s.yaml"
grep -q 'kind: SealedSecret' "$d/s.yaml" || die "kubeseal produced no SealedSecret"

{ echo "# Generated by scripts/reseal_minio_consumers.sh on $(date -Is)."
  echo "# Sealed from the live value of database/minio-root-sealed, so MinIO and"
  echo "# its consumers cannot drift apart. Never edit by hand: the ciphertext is"
  echo "# the only copy and re-running the script is the way to change it."
  echo "#"
  echo "# managed: \"true\" is load-bearing -- it lets the controller adopt the"
  echo "# hand-made Secret of the same name that predates this manifest."
  grep -v '^\s*creationTimestamp' "$d/s.yaml"; } > "$OUT"
k apply -f "$OUT" >/dev/null
ok "sealed and applied: airflow/airflow-s3-credentials -> $OUT"

# Adoption is the thing to verify, not existence: a Secret of this name already
# existed, so "the Secret is there" proves nothing at all.
for _ in $(seq 1 30); do
  [ "$(k -n airflow get secret airflow-s3-credentials \
        -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null)" = SealedSecret ] && break
  sleep 1
done
[ "$(k -n airflow get secret airflow-s3-credentials -o jsonpath='{.metadata.ownerReferences[0].kind}')" = SealedSecret ] \
  || die "the controller did not adopt airflow/airflow-s3-credentials -- do NOT commit $OUT"
want="$(sha256sum < "$d/connectionUri" | cut -d' ' -f1)"
got="$(k -n airflow get secret airflow-s3-credentials -o jsonpath='{.data.connectionUri}' | base64 -d | sha256sum | cut -d' ' -f1)"
[ "$want" = "$got" ] || die "the live secret does not match what was sealed -- do NOT commit $OUT"
ok "adopted by the SealedSecret, and the live value is the one that was sealed"

# ------------------------------------------------- make it take effect, prove --
# The long-running components resolve this secretKeyRef into env at boot, so
# they hold the dead key until they restart. KPO/executor task pods read theirs
# per task run and need nothing.
for dep in airflow-scheduler airflow-api-server airflow-dag-processor; do
  k -n airflow rollout restart "deploy/$dep" >/dev/null
done
k -n airflow rollout restart statefulset/airflow-triggerer >/dev/null 2>&1 || true
for dep in airflow-scheduler airflow-api-server airflow-dag-processor; do
  k -n airflow rollout status "deploy/$dep" --timeout=300s >/dev/null \
    && ok "restarted $dep" || warn "$dep restart did not complete; check it by hand"
done

# The same probe that exposed the breakage, run against the fix.
k -n airflow exec deploy/airflow-scheduler -c scheduler -- python -c "
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
import sys
h = S3Hook(aws_conn_id='aws_default')
sys.exit(0 if h.check_for_bucket('$LOG_BUCKET') else 1)
" >/dev/null 2>&1 \
  && ok "airflow's aws_default connection can reach s3://$LOG_BUCKET again" \
  || die "aws_default still cannot reach s3://$LOG_BUCKET -- investigate before committing $OUT"

printf '\n%sNow commit %s and its Application, or the cluster is again holding a credential git does not describe.%s\n' "$C_Y" "$OUT" "$C_0"
