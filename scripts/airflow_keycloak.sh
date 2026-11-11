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

# NO CLIENT SECRET HERE. This script used to carry one as a shell default, in
# a PUBLIC repository, and omitting `secret` from the client representation is
# what makes that unnecessary: Keycloak leaves an existing client's secret
# alone on update. Keeping the default was also a live footgun, because a
# re-run would have reset the client secret to a value anyone could read.
#
# To rotate a client secret, use scripts/rotate_keycloak_client_secret.sh,
# which regenerates it in Keycloak, seals it, and never prints it.
AIRFLOW_URL="${AIRFLOW_URL:-https://airflow.sentpul.click}"

kc_init

kc_upsert_realm_role "airflow_admin" "Admin role for Apache Airflow"

kc_upsert_client "$(jq -n --arg u "$AIRFLOW_URL" '{
  clientId: "airflow",
  name: "Airflow FAB Client",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  standardFlowEnabled: true,
  redirectUris: [($u + "/*")],
  webOrigins: [$u]
}')"

kc_upsert_client "$(jq -n --arg u "$AIRFLOW_URL" '{
  clientId: "airflow-client",
  name: "Airflow OAuth2 Proxy Client",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  standardFlowEnabled: true,
  redirectUris: [($u + "/*")],
  webOrigins: [$u]
}')"

info "Client secrets:"
kc_print_secret airflow
kc_print_secret airflow-client
ok "Airflow Keycloak setup complete."
