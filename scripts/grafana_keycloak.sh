#!/usr/bin/env bash
# grafana_keycloak.sh — create/update the Keycloak OIDC client used by Grafana
# (grafana.sentpul.click) for Generic OAuth authentication.
#
# Grafana is configured via GF_AUTH_GENERIC_OAUTH_* in grafana-values.yaml.
# This script only sets up the Keycloak side (client + roles).
#
# Usage:
#   ./scripts/grafana_keycloak.sh
#   KC_PORT_FORWARD=1 ./scripts/grafana_keycloak.sh
#
# Env overrides:
#   GRAFANA_URL             Grafana public URL     (default: https://grafana.sentpul.click)
#   (all KC_* vars from keycloak_common.sh are also respected)

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
GRAFANA_URL="${GRAFANA_URL:-https://grafana.sentpul.click}"

kc_init

# ── Grafana OIDC client ───────────────────────────────────────────────────────
# Grafana Generic OAuth callback path is /login/generic_oauth.
kc_upsert_client "$(jq -n --arg u "$GRAFANA_URL" '{
  clientId:                  "grafana",
  name:                      "Grafana (Generic OAuth)",
  description:               "SSO for grafana.sentpul.click",
  enabled:                   true,
  protocol:                  "openid-connect",
  publicClient:              false,
  clientAuthenticatorType:   "client-secret",
  standardFlowEnabled:       true,
  implicitFlowEnabled:       false,
  directAccessGrantsEnabled: false,
  redirectUris:              [($u + "/login/generic_oauth")],
  webOrigins:                [$u],
  attributes: {
    "pkce.code.challenge.method": "S256"
  },
  protocolMappers: [
    {
      name:             "realm-roles",
      protocol:         "openid-connect",
      protocolMapper:   "oidc-usermodel-realm-role-mapper",
      consentRequired:  false,
      config: {
        "multivalued":           "true",
        "userinfo.token.claim":  "true",
        "id.token.claim":        "true",
        "access.token.claim":    "true",
        "claim.name":            "realm_access.roles",
        "jsonType.label":        "String"
      }
    }
  ]
}')"

# ── Realm roles used by Grafana role mapping ──────────────────────────────────
# grafana-values.yaml maps:
#   airflow_admin  →  Grafana Admin
#   (everyone else) → Grafana Viewer
#
# Ensure airflow_admin exists (shared with the Airflow client).
kc_upsert_realm_role "airflow_admin" "Platform admin — grants Admin in Airflow and Grafana"

ok "Grafana Keycloak setup complete."
info "Role mapping (set in grafana-values.yaml):"
info "  Keycloak realm role 'airflow_admin'  →  Grafana Admin"
info "  (all other authenticated users)      →  Grafana Viewer"
warn "Assign realm role 'airflow_admin' to users who need Grafana Admin access."
