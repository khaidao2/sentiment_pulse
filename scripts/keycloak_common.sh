# keycloak_common.sh — shared helpers sourced by <service>_keycloak.sh scripts.
# Not executable on its own; `source` it.
#
# Provides: realm bootstrap + idempotent client/role upsert against the
# Keycloak Admin REST API. Each per-service script defines only its own client.
#
# Credential resolution (first match wins):
#   1. KEYCLOAK_ADMIN_USER / KEYCLOAK_ADMIN_PASSWORD env vars
#   2. K8s secret  keycloak  (namespace: keycloak)  via kubectl
#   3. Chart defaults (admin / adminpassword)
#
# Connection: default https://auth.sentpul.click; set KC_PORT_FORWARD=1 to
# port-forward svc/keycloak and talk to it locally (no DNS/TLS needed).

set -euo pipefail

KC_URL="${KC_URL:-https://auth.sentpul.click}"
KC_NAMESPACE="${KC_NAMESPACE:-keycloak}"
KC_SERVICE="${KC_SERVICE:-keycloak}"
REALM_NAME="${REALM_NAME:-data-platform}"
KC_PORT_FORWARD="${KC_PORT_FORWARD:-0}"
PF_LOCAL_PORT="${PF_LOCAL_PORT:-8088}"

info() { printf '\033[0;34m[INFO]\033[0m  %s\n' "$*"; }
ok()   { printf '\033[0;32m[OK]\033[0m    %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m  %s\n' "$*"; }
die()  { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

PF_PID=""
_kc_cleanup() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true; }
trap _kc_cleanup EXIT

kc_check_deps() {
  local missing=()
  for cmd in curl jq; do command -v "$cmd" &>/dev/null || missing+=("$cmd"); done
  [[ ${#missing[@]} -eq 0 ]] || die "Missing required tools: ${missing[*]}"
}

kc_start_port_forward() {
  [[ "$KC_PORT_FORWARD" == "1" ]] || return 0
  command -v kubectl &>/dev/null || die "KC_PORT_FORWARD=1 needs kubectl."
  info "Port-forwarding svc/${KC_SERVICE} -n ${KC_NAMESPACE} -> localhost:${PF_LOCAL_PORT}"
  kubectl port-forward -n "$KC_NAMESPACE" "svc/${KC_SERVICE}" "${PF_LOCAL_PORT}:80" &>/dev/null &
  PF_PID=$!
  sleep 3
  kill -0 "$PF_PID" 2>/dev/null || die "Port-forward failed to start."
  KC_URL="http://localhost:${PF_LOCAL_PORT}"
  ok "Using local Keycloak at $KC_URL"
}

kc_load_credentials() {
  if [[ -n "${KEYCLOAK_ADMIN_USER:-}" && -n "${KEYCLOAK_ADMIN_PASSWORD:-}" ]]; then
    info "Using admin credentials from environment variables."; return
  fi
  if command -v kubectl &>/dev/null && kubectl get secret keycloak -n "$KC_NAMESPACE" &>/dev/null; then
    KEYCLOAK_ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"
    KEYCLOAK_ADMIN_PASSWORD="$(kubectl get secret keycloak -n "$KC_NAMESPACE" \
      -o jsonpath='{.data.admin-password}' | base64 -d 2>/dev/null || true)"
    [[ -n "$KEYCLOAK_ADMIN_PASSWORD" ]] && { ok "Admin credentials loaded from secret 'keycloak'."; return; }
  fi
  warn "Falling back to chart default credentials (admin/adminpassword)."
  KEYCLOAK_ADMIN_USER="${KEYCLOAK_ADMIN_USER:-admin}"
  KEYCLOAK_ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-adminpassword}"
}

kc_get_token() {
  info "Requesting admin access token from $KC_URL ..."
  TOKEN="$(curl -sf --connect-timeout 10 --max-time 30 \
    -d "client_id=admin-cli" -d "grant_type=password" \
    -d "username=${KEYCLOAK_ADMIN_USER}" \
    --data-urlencode "password=${KEYCLOAK_ADMIN_PASSWORD}" \
    "${KC_URL}/realms/master/protocol/openid-connect/token" | jq -r '.access_token')" \
    || die "Failed to reach Keycloak at $KC_URL (check URL / try KC_PORT_FORWARD=1)."
  [[ -n "$TOKEN" && "$TOKEN" != "null" ]] || die "Login failed — wrong admin credentials?"
  ok "Got admin token."
}

kc_ensure_realm() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
    "${KC_URL}/admin/realms/${REALM_NAME}")"
  if [[ "$code" == "200" ]]; then
    ok "Realm '${REALM_NAME}' exists."
  else
    info "Creating realm '${REALM_NAME}' ..."
    curl -sf -X POST "${KC_URL}/admin/realms" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "{\"realm\":\"${REALM_NAME}\",\"enabled\":true}" || die "Realm create failed."
    ok "Realm '${REALM_NAME}' created."
  fi
}

# kc_upsert_client <client-json>   (JSON must contain .clientId)
kc_upsert_client() {
  local spec="$1" cid existing
  cid="$(jq -r '.clientId' <<<"$spec")"
  existing="$(curl -sf -H "Authorization: Bearer $TOKEN" \
    "${KC_URL}/admin/realms/${REALM_NAME}/clients?clientId=${cid}" | jq -r '.[0].id // empty')"
  if [[ -n "$existing" ]]; then
    info "Updating client '${cid}' ..."
    jq --arg id "$existing" '. + {id:$id}' <<<"$spec" \
      | curl -sf -X PUT "${KC_URL}/admin/realms/${REALM_NAME}/clients/${existing}" \
          -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
          --data-binary @- || die "Update of client '${cid}' failed."
    ok "Client '${cid}' updated."
  else
    info "Creating client '${cid}' ..."
    curl -sf -X POST "${KC_URL}/admin/realms/${REALM_NAME}/clients" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      --data-binary "$spec" || die "Create of client '${cid}' failed."
    ok "Client '${cid}' created."
  fi
}

# kc_upsert_realm_role <name> [description]
kc_upsert_realm_role() {
  local name="$1" desc="${2:-}"
  if curl -sf -o /dev/null -H "Authorization: Bearer $TOKEN" \
      "${KC_URL}/admin/realms/${REALM_NAME}/roles/${name}"; then
    ok "Realm role '${name}' exists."
  else
    curl -sf -X POST "${KC_URL}/admin/realms/${REALM_NAME}/roles" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "{\"name\":\"${name}\",\"description\":\"${desc}\"}" || die "Role create failed."
    ok "Realm role '${name}' created."
  fi
}

# kc_print_secret <clientId>
kc_print_secret() {
  local cid="$1" id secret
  id="$(curl -sf -H "Authorization: Bearer $TOKEN" \
    "${KC_URL}/admin/realms/${REALM_NAME}/clients?clientId=${cid}" | jq -r '.[0].id // empty')"
  [[ -n "$id" ]] || return 0
  secret="$(curl -sf -H "Authorization: Bearer $TOKEN" \
    "${KC_URL}/admin/realms/${REALM_NAME}/clients/${id}/client-secret" | jq -r '.value // empty')"
  [[ -n "$secret" ]] && info "  ${cid} secret: ${secret}"
}

# kc_init — run the common bootstrap (deps, connection, token, realm).
kc_init() {
  kc_check_deps
  kc_start_port_forward
  kc_load_credentials
  kc_get_token
  kc_ensure_realm
}
