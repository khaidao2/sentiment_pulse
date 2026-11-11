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
# NO CLIENT SECRET HERE. This script used to carry one as a shell default, in
# a PUBLIC repository, and omitting `secret` from the client representation is
# what makes that unnecessary: Keycloak leaves an existing client's secret
# alone on update. Keeping the default was also a live footgun, because a
# re-run would have reset the client secret to a value anyone could read.
#
# To rotate a client secret, use scripts/rotate_keycloak_client_secret.sh,
# which regenerates it in Keycloak, seals it, and never prints it.
KAFKA_UI_URL="${KAFKA_UI_URL:-https://kafka.sentpul.click}"

kc_init

kc_upsert_client "$(jq -n --arg u "$KAFKA_UI_URL" '{
  clientId: "kafka-ui",
  name: "Kafka UI (native OIDC)",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  standardFlowEnabled: true,
  redirectUris: [($u + "/login/oauth2/code/keycloak")],
  webOrigins: [$u]
}')"

info "Client secret:"
kc_print_secret kafka-ui
ok "Kafka UI Keycloak setup complete."
