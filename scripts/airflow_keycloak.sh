#!/usr/bin/env bash
# airflow_keycloak.sh — create/update Airflow's Keycloak OIDC clients.
#
# Creates two clients in the data-platform realm (idempotent):
#   * airflow         — Airflow's native FAB OAuth integration
#   * airflow-client  — used by oauth2-proxy in front of Airflow
# plus the airflow_admin realm role.
#
# Usage:
#   ./scripts/airflow_keycloak.sh                 # via public ingress
#   KC_PORT_FORWARD=1 ./scripts/airflow_keycloak.sh   # via port-forward

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

# Must match oauth2-proxy-values.yaml and the Airflow OAuth config.
AIRFLOW_SECRET="${AIRFLOW_CLIENT_SECRET:-XP2t3V0jEDoVv8HGek6FbDkEOeQe7E7I}"
AIRFLOW_URL="${AIRFLOW_URL:-https://airflow.sentpul.click}"

kc_init

kc_upsert_realm_role "airflow_admin" "Admin role for Apache Airflow"

kc_upsert_client "$(jq -n --arg s "$AIRFLOW_SECRET" --arg u "$AIRFLOW_URL" '{
  clientId: "airflow",
  name: "Airflow FAB Client",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  secret: $s,
  standardFlowEnabled: true,
  redirectUris: [($u + "/*")],
  webOrigins: [$u]
}')"

kc_upsert_client "$(jq -n --arg s "$AIRFLOW_SECRET" --arg u "$AIRFLOW_URL" '{
  clientId: "airflow-client",
  name: "Airflow OAuth2 Proxy Client",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  secret: $s,
  standardFlowEnabled: true,
  redirectUris: [($u + "/*")],
  webOrigins: [$u]
}')"

info "Client secrets:"
kc_print_secret airflow
kc_print_secret airflow-client
ok "Airflow Keycloak setup complete."
