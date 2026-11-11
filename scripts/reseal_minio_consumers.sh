#!/usr/bin/env bash
# Reconcile EVERY MinIO consumer secret with the root credentials MinIO actually
# runs on, and leave each one described by git.
#
# WHY THIS EXISTS. MinIO here has exactly ONE identity: root. `mc admin user
# list` and `mc admin user svcacct list` are both empty, so every client
# authenticates with the root key and a root rotation invalidates all of them at
# once. There are three copies:
#
#   airflow/airflow-s3-credentials   the Airflow connection URI behind
#                                    AIRFLOW_CONN_AWS_DEFAULT and
#                                    AIRFLOW_CONN_DATALAKE_MINIO -- remote task
#                                    logging writes s3://airflow/logs through it
#   airflow/minio-credentials        the dbt task pods of transform_itviec
#                                    (dags/transform_itviec_dag.py)
#   database/minio-credentials       the five sinks
#
# rotate_platform_secrets.sh step 5 patches the two `minio-credentials` copies,
# which is why the sinks and dbt still work. It has never known about
# airflow-s3-credentials -- a hand-made plaintext Secret from 2026-05-26 that no
# manifest and no Application ever owned. The 2026-11-07 rotation therefore left
# it on a dead access key, and from 11-07 every task log upload failed with
# InvalidAccessKeyId: no DAG wrote a log anywhere, while ArgoCD reported Synced
# and the crawls kept landing Parquet. The platform lost its entire debugging
# surface to a credential git did not describe.
#
# So this script does two things that belong together:
#
#   1. DERIVES, never invents. It reads the live root secret and derives every
#      consumer from THAT, so there is one source of truth and drift is
#      impossible by construction (same discipline as
#      reseal_clickhouse_consumers.sh).
#   2. SEALS all three into committed manifests, so the next rotation cannot
#      leave a consumer behind in a Secret that only exists in the cluster.
#
# Each consumer keeps its NAME, so airflow-values.yaml, the worker pod template,
# transform_itviec_dag.py and the five sink manifests are all untouched.
#
# Run it after any MinIO root rotation, or any time task logs stop appearing.
# Idempotent: a re-run that changes nothing says so and restarts nothing.
#
# ORDERING. The sealed secrets are applied here so the fix takes effect at once,
# but the generated manifests and their two Applications must be COMMITTED AND
# MERGED. Until then the cluster is holding credentials git does not describe --
# which is the bug this script exists to end.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
MF=platform-gitops/manifests
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

for t in kubectl kubeseal python3 base64; do command -v $t >/dev/null || die "missing tool: $t"; done
k get nodes >/dev/null 2>&1 || die "kube context 'sentpul' unreachable"

d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT; umask 077
CHANGED_SINKS=0; CHANGED_AIRFLOW=0

# ---------------------------------------------------------- source of truth --
for key in MINIO_ROOT_USER MINIO_ROOT_PASSWORD; do
  k -n database get secret minio-root-sealed -o "jsonpath={.data.$key}" | base64 -d > "$d/$key"
  [ -s "$d/$key" ] || die "database/minio-root-sealed holds no $key"
done
ok "read the live root credentials (user sha $(sha256sum < "$d/MINIO_ROOT_USER" | cut -c1-10), password sha $(sha256sum < "$d/MINIO_ROOT_PASSWORD" | cut -c1-10))"

pod="$(k -n database get pods -l app=minio -o jsonpath='{.items[0].metadata.name}')"
[ -n "$pod" ] || die "no running MinIO pod in ns database"

# Authenticate a credential PAIR against MinIO, from inside the MinIO pod.
#
# Base64, one value per line: the sealed values carry NO trailing newline, so
# `cat user pass` hands the reader a single run-together line and an empty
# password (which is how the first run of this script failed), and a credential
# must survive a value containing a newline, a space or a shell metacharacter
# without the transport caring. MC_HOST_* rather than `mc alias set`, so the
# credential never reaches argv.
minio_auth(){ # <user-file> <pass-file>
  { base64 -w0 < "$1"; echo; base64 -w0 < "$2"; echo; } \
    | k -n database exec -i "$pod" -- sh -c '
        IFS= read -r u64; IFS= read -r p64
        u="$(printf %s "$u64" | base64 -d)"; p="$(printf %s "$p64" | base64 -d)"
        MC_HOST_loc="http://$u:$p@127.0.0.1:9000" mc ls "loc/'"$LOG_BUCKET"'" >/dev/null'
}

# Prove the source of truth is what MinIO actually accepts BEFORE deriving
# anything from it. A sealed secret and a running server can disagree, and that
# disagreement is the whole bug class this script addresses.
minio_auth "$d/MINIO_ROOT_USER" "$d/MINIO_ROOT_PASSWORD" \
  || die "the live root credentials do NOT authenticate against MinIO -- rotate root first, there is nothing sound to derive from"
ok "live root credentials authenticate against MinIO and can read bucket '$LOG_BUCKET'"

# ------------------------------------------------------ derive the consumers --
# Percent-encoded, because a password containing '@', '/' or ':' would otherwise
# silently produce a URI that parses into the wrong credential. strong() in
# rotate_platform_secrets.sh happens to emit alphanumerics today; this does not
# depend on that staying true. Verified against Airflow's own parser with a
# password containing @ / : ? & #.
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

# ------------------------------------------------------------- seal and adopt --
seal_adopt(){ # <ns> <name> <out> <key>=<srcfile>...
  local ns="$1" name="$2" out="$3"; shift 3
  local kv key src live want changed=0 args=()

  for kv in "$@"; do
    key="${kv%%=*}"; src="${kv#*=}"
    args+=( --from-file="$key=$src" )
    # Ownership changes are free. A VALUE change means the consumer's running
    # pods hold a stale key until they restart, which is a different problem and
    # has to be said out loud.
    live="$(k -n "$ns" get secret "$name" -o "jsonpath={.data.$key}" 2>/dev/null \
            | base64 -d 2>/dev/null | sha256sum | cut -d' ' -f1 || true)"
    want="$(sha256sum < "$src" | cut -d' ' -f1)"
    [ "$live" = "$want" ] || changed=1
  done

  # ADOPTION. The controller refuses to overwrite a Secret it does not own:
  #   failed update: Resource "..." already exists and is not managed by SealedSecret
  # The annotation has to be on the EXISTING Secret for the takeover to happen --
  # carrying it only in the SealedSecret's template is not enough, because the
  # template is what the controller writes AFTER it has decided it may write at
  # all. It goes in both places on purpose: here to permit the takeover once, and
  # in the template so adoption survives the Secret being recreated from git.
  if k -n "$ns" get secret "$name" >/dev/null 2>&1; then
    k -n "$ns" annotate secret "$name" \
      sealedsecrets.bitnami.com/managed=true --overwrite >/dev/null
  fi

  k create secret generic "$name" -n "$ns" "${args[@]}" --dry-run=client -o yaml \
    | k annotate --local -f - sealedsecrets.bitnami.com/managed=true -o yaml \
    | seal_stdin > "$d/s.yaml"
  grep -q 'kind: SealedSecret' "$d/s.yaml" || die "kubeseal produced no SealedSecret for $ns/$name"

  { echo "# Generated by scripts/reseal_minio_consumers.sh on $(date -Is)."
    echo "# Sealed from the live value of database/minio-root-sealed, so MinIO and"
    echo "# its consumers cannot drift apart. Never edit by hand: the ciphertext is"
    echo "# the only copy and re-running the script is the way to change it."
    echo "#"
    echo "# managed: \"true\" is load-bearing -- it lets the controller adopt the"
    echo "# hand-made Secret of the same name that predates this manifest."
    grep -v '^\s*creationTimestamp' "$d/s.yaml"; } > "$out"
  k apply -f "$out" >/dev/null

  # Adoption is the thing to verify, not existence: a Secret of this name
  # already existed, so "the Secret is there" proves nothing at all.
  local i
  for i in $(seq 1 30); do
    [ "$(k -n "$ns" get secret "$name" \
          -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null)" = SealedSecret ] && break
    sleep 1
  done
  [ "$(k -n "$ns" get secret "$name" -o jsonpath='{.metadata.ownerReferences[0].kind}')" = SealedSecret ] \
    || die "the controller did not adopt $ns/$name -- do NOT commit $out"

  for kv in "$@"; do
    key="${kv%%=*}"; src="${kv#*=}"
    want="$(sha256sum < "$src" | cut -d' ' -f1)"
    live="$(k -n "$ns" get secret "$name" -o "jsonpath={.data.$key}" | base64 -d | sha256sum | cut -d' ' -f1)"
    [ "$want" = "$live" ] || die "$ns/$name key $key does not match what was sealed -- do NOT commit $out"
  done

  if [ "$changed" = 1 ]; then
    case "$ns/$name" in
      database/minio-credentials) CHANGED_SINKS=1 ;;
      *) CHANGED_AIRFLOW=1 ;;
    esac
    ok "$ns/$name: sealed, adopted, VALUE CHANGED -> $out"
  else
    ok "$ns/$name: sealed, adopted, value unchanged (ownership moved to git) -> $out"
  fi
}

seal_adopt airflow  airflow-s3-credentials "$MF/minio-consumer-airflow-s3-sealed-secret.yaml" \
  "connectionUri=$d/connectionUri"
seal_adopt airflow  minio-credentials      "$MF/minio-consumer-airflow-creds-sealed-secret.yaml" \
  "accessKey=$d/MINIO_ROOT_USER" "secretKey=$d/MINIO_ROOT_PASSWORD"
seal_adopt database minio-credentials      "$MF/minio-consumer-database-creds-sealed-secret.yaml" \
  "accessKey=$d/MINIO_ROOT_USER" "secretKey=$d/MINIO_ROOT_PASSWORD"

# Prove the SEALED values authenticate, not just the source they came from: a
# controller that decrypts to the wrong bytes would otherwise be invisible until
# the next sink restart.
for ns in airflow database; do
  k -n "$ns" get secret minio-credentials -o jsonpath='{.data.accessKey}' | base64 -d > "$d/ak"
  k -n "$ns" get secret minio-credentials -o jsonpath='{.data.secretKey}' | base64 -d > "$d/sk"
  minio_auth "$d/ak" "$d/sk" || die "$ns/minio-credentials does not authenticate against MinIO"
  ok "$ns/minio-credentials authenticates against MinIO"
done

# ------------------------------------------------- make it take effect, prove --
# Long-running components resolve their secretKeyRefs into env at boot, so they
# hold the old value until they restart. KPO/executor task pods read theirs per
# task run and need nothing. Nothing is restarted when nothing changed.
if [ "$CHANGED_AIRFLOW" = 1 ]; then
  for dep in airflow-scheduler airflow-api-server airflow-dag-processor; do
    k -n airflow rollout restart "deploy/$dep" >/dev/null
  done
  k -n airflow rollout restart statefulset/airflow-triggerer >/dev/null 2>&1 || true
  for dep in airflow-scheduler airflow-api-server airflow-dag-processor; do
    k -n airflow rollout status "deploy/$dep" --timeout=300s >/dev/null \
      && ok "restarted $dep" || warn "$dep restart did not complete; check it by hand"
  done
else
  ok "airflow components already hold the current value; no restart needed"
fi

if [ "$CHANGED_SINKS" = 1 ]; then
  for dep in $(k -n database get deploy -o name | grep -- '-sink$'); do
    k -n database rollout restart "$dep" >/dev/null \
      && k -n database rollout status "$dep" --timeout=300s >/dev/null \
      && ok "restarted $dep" || warn "$dep restart did not complete; check it by hand"
  done
else
  ok "sinks already hold the current value; no restart needed"
fi

# The end-to-end proof, and the exact operation remote logging performs: a write,
# a read back, and a delete. check_for_bucket alone would pass on a credential
# that cannot PUT.
k -n airflow exec deploy/airflow-scheduler -c scheduler -- python -c "
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
import sys
h = S3Hook(aws_conn_id='aws_default')
key = 'logs/.reseal-write-probe'
try:
    h.load_string('probe', key=key, bucket_name='$LOG_BUCKET', replace=True)
    assert h.read_key(key, bucket_name='$LOG_BUCKET') == 'probe'
finally:
    h.delete_objects(bucket='$LOG_BUCKET', keys=[key])
sys.exit(0)
" >/dev/null 2>&1 \
  && ok "aws_default can write, read back and delete in s3://$LOG_BUCKET -- remote task logging works" \
  || die "aws_default still cannot round-trip s3://$LOG_BUCKET -- investigate before committing"

printf '\n%sNow commit the three manifests and their two Applications, or the cluster is again holding credentials git does not describe.%s\n' "$C_Y" "$C_0"
