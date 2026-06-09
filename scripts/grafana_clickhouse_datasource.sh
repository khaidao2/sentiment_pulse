#!/usr/bin/env bash
# grafana_clickhouse_datasource.sh — upsert the ClickHouse datasource in Grafana.
#
# Usage:
#   ./scripts/grafana_clickhouse_datasource.sh
#   GF_PORT_FORWARD=1 ./scripts/grafana_clickhouse_datasource.sh
#
# Env overrides:
#   GF_URL            Grafana base URL             (default: https://grafana.sentpul.click)
#   GF_ADMIN_USER     Grafana admin username        (default: admin)
#   GF_ADMIN_PASSWORD Grafana admin password        (default: adminpassword)
#   GF_PORT_FORWARD   Set to 1 to port-forward before connecting
#   GF_NAMESPACE      K8s namespace for Grafana     (default: airflow)
#   GF_PF_PORT        Local port for port-forward   (default: 3000)
#   CH_HOST           ClickHouse hostname           (default: clickhouse.database.svc.cluster.local)
#   CH_PORT           ClickHouse HTTP port          (default: 8123)
#   CH_USER           ClickHouse username           (default: clickhouse)
#   CH_PASSWORD       ClickHouse password           (default: clickhousepassword)
#   CH_DATABASE       Default database              (default: default)

set -euo pipefail

# ── helpers ───────────────────────────────────────────────────────────────────
info() { printf '\033[0;34m[INFO]\033[0m  %s\n' "$*"; }
ok()   { printf '\033[0;32m[OK]\033[0m    %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m  %s\n' "$*"; }
die()  { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

# ── config ────────────────────────────────────────────────────────────────────
GF_URL="${GF_URL:-https://grafana.sentpul.click}"
GF_ADMIN_USER="${GF_ADMIN_USER:-admin}"
GF_ADMIN_PASSWORD="${GF_ADMIN_PASSWORD:-adminpassword}"
GF_PORT_FORWARD="${GF_PORT_FORWARD:-0}"
GF_NAMESPACE="${GF_NAMESPACE:-airflow}"
GF_PF_PORT="${GF_PF_PORT:-3000}"

CH_HOST="${CH_HOST:-clickhouse.database.svc.cluster.local}"
CH_PORT="${CH_PORT:-8123}"
CH_USER="${CH_USER:-clickhouse}"
CH_PASSWORD="${CH_PASSWORD:-clickhousepassword}"
CH_DATABASE="${CH_DATABASE:-default}"

DS_NAME="ClickHouse"
DS_TYPE="grafana-clickhouse-datasource"

# ── cleanup ───────────────────────────────────────────────────────────────────
PF_PID=""
_cleanup() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true; }
trap _cleanup EXIT

# ── dep check ─────────────────────────────────────────────────────────────────
for cmd in curl jq; do
  command -v "$cmd" &>/dev/null || die "Missing required tool: $cmd"
done

# ── port-forward ──────────────────────────────────────────────────────────────
if [[ "$GF_PORT_FORWARD" == "1" ]]; then
  command -v kubectl &>/dev/null || die "GF_PORT_FORWARD=1 needs kubectl."
  GF_SVC="$(kubectl get svc -n "$GF_NAMESPACE" -o name 2>/dev/null | grep grafana | head -1)"
  [[ -n "$GF_SVC" ]] || die "No grafana service found in namespace '${GF_NAMESPACE}'."
  info "Port-forwarding ${GF_SVC} -n ${GF_NAMESPACE} -> localhost:${GF_PF_PORT}"
  kubectl port-forward -n "$GF_NAMESPACE" "$GF_SVC" "${GF_PF_PORT}:80" &>/dev/null &
  PF_PID=$!
  sleep 3
  kill -0 "$PF_PID" 2>/dev/null || die "Port-forward failed to start."
  GF_URL="http://localhost:${GF_PF_PORT}"
  ok "Using local Grafana at $GF_URL"
fi

GF_AUTH="${GF_ADMIN_USER}:${GF_ADMIN_PASSWORD}"

# ── verify Grafana is reachable ───────────────────────────────────────────────
info "Checking Grafana health at ${GF_URL} ..."
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 \
  -u "$GF_AUTH" "${GF_URL}/api/health")"
[[ "$HTTP_CODE" == "200" ]] \
  || die "Grafana not reachable at ${GF_URL} (HTTP ${HTTP_CODE}). Try GF_PORT_FORWARD=1."
ok "Grafana is up."

# ── verify plugin is installed ────────────────────────────────────────────────
info "Checking plugin '${DS_TYPE}' is installed ..."
PLUGIN_STATE="$(curl -sf -u "$GF_AUTH" "${GF_URL}/api/plugins/${DS_TYPE}/settings" \
  2>/dev/null | jq -r '.enabled // empty' || true)"
if [[ -z "$PLUGIN_STATE" ]]; then
  warn "Plugin '${DS_TYPE}' not found — datasource type will be unknown in Grafana."
  warn "Add to platform-gitops/helm-values/grafana-values.yaml:"
  warn "  plugins:"
  warn "    - grafana-clickhouse-datasource"
  warn "Proceeding anyway (datasource will activate once the plugin is installed)."
else
  ok "Plugin '${DS_TYPE}' is installed."
fi

# ── build datasource payload ──────────────────────────────────────────────────
DS_JSON="$(jq -n \
  --arg  name "$DS_NAME" \
  --arg  type "$DS_TYPE" \
  --arg  host "$CH_HOST" \
  --argjson port "$CH_PORT" \
  --arg  user "$CH_USER" \
  --arg  pass "$CH_PASSWORD" \
  --arg  db   "$CH_DATABASE" \
'{
  name:      $name,
  type:      $type,
  access:    "proxy",
  isDefault: false,
  jsonData: {
    server:          $host,
    port:            $port,
    protocol:        "http",
    username:        $user,
    defaultDatabase: $db,
    tlsSkipVerify:   false
  },
  secureJsonData: {
    password: $pass
  }
}')"

# ── upsert datasource ─────────────────────────────────────────────────────────
info "Checking if datasource '${DS_NAME}' already exists ..."
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' \
  -u "$GF_AUTH" "${GF_URL}/api/datasources/name/${DS_NAME}")"

if [[ "$HTTP_CODE" == "200" ]]; then
  DS_ID="$(curl -sf -u "$GF_AUTH" "${GF_URL}/api/datasources/name/${DS_NAME}" \
    | jq -r '.id')"
  info "Datasource '${DS_NAME}' exists (id=${DS_ID}), updating ..."
  MSG="$(curl -sf -X PUT "${GF_URL}/api/datasources/${DS_ID}" \
    -u "$GF_AUTH" \
    -H "Content-Type: application/json" \
    --data-binary "$DS_JSON" | jq -r '.message // "updated"')"
  ok "${MSG}"
elif [[ "$HTTP_CODE" == "404" ]]; then
  info "Datasource '${DS_NAME}' not found, creating ..."
  MSG="$(curl -sf -X POST "${GF_URL}/api/datasources" \
    -u "$GF_AUTH" \
    -H "Content-Type: application/json" \
    --data-binary "$DS_JSON" | jq -r '.message // "created"')"
  ok "${MSG}"
else
  die "Unexpected HTTP ${HTTP_CODE} from Grafana (check GF_URL / credentials)."
fi

ok "ClickHouse datasource '${DS_NAME}' provisioned at ${GF_URL}."
