#!/usr/bin/env bash
# kafka_ui_keycloak.sh — create/update kafka-ui's Keycloak OIDC client.
#
# Creates the `kafka-ui` client in the data-platform realm (idempotent).
# kafka-ui is wired for native OAuth2/OIDC SSO in manifests/kafka-ui.yaml
# (SPRING_SECURITY_OAUTH2_CLIENT_*, same pattern as MinIO — no oauth2-proxy),
# but that only works once this client actually exists in Keycloak.
#
# Usage:
#   ./scripts/kafka_ui_keycloak.sh
#   KC_PORT_FORWARD=1 ./scripts/kafka_ui_keycloak.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

# Must match SPRING_SECURITY_OAUTH2_CLIENT_REGISTRATION_KEYCLOAK_CLIENTSECRET
# in manifests/kafka-ui.yaml.
KAFKA_UI_SECRET="${KAFKA_UI_CLIENT_SECRET:-736d99c12362911eeb9837cc08dcc7d83fa2af3c20485851}"
KAFKA_UI_URL="${KAFKA_UI_URL:-https://kafka.sentpul.click}"

kc_init

kc_upsert_client "$(jq -n --arg s "$KAFKA_UI_SECRET" --arg u "$KAFKA_UI_URL" '{
  clientId: "kafka-ui",
  name: "Kafka UI (native OIDC)",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  secret: $s,
  standardFlowEnabled: true,
  redirectUris: [($u + "/login/oauth2/code/keycloak")],
  webOrigins: [$u]
}')"

info "Client secret:"
kc_print_secret kafka-ui
ok "Kafka UI Keycloak setup complete."
