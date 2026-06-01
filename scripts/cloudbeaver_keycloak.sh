#!/usr/bin/env bash
# cloudbeaver_keycloak.sh — create/update the Keycloak OIDC client used by
# oauth2-proxy to gate CloudBeaver (cloudbeaver.sentpul.click).
#
# Usage:
#   ./scripts/cloudbeaver_keycloak.sh
#   KC_PORT_FORWARD=1 ./scripts/cloudbeaver_keycloak.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

# Must match clientSecret in helm-values/oauth2-proxy-cloudbeaver-values.yaml.
CLOUDBEAVER_SECRET="${CLOUDBEAVER_CLIENT_SECRET:-ec7b3de27e9af4bc08d35dad623abc9acd9081e2a310af45}"
CLOUDBEAVER_URL="${CLOUDBEAVER_URL:-https://cloudbeaver.sentpul.click}"

kc_init

kc_upsert_client "$(jq -n --arg s "$CLOUDBEAVER_SECRET" --arg u "$CLOUDBEAVER_URL" '{
  clientId: "cloudbeaver",
  name: "CloudBeaver (oauth2-proxy)",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  secret: $s,
  standardFlowEnabled: true,
  redirectUris: [($u + "/oauth2/callback")],
  webOrigins: [$u]
}')"

info "Client secret:"
kc_print_secret cloudbeaver
ok "CloudBeaver Keycloak setup complete."
warn "One-time: open CloudBeaver as admin (cbadmin) and enable anonymous access"
warn "so users are not prompted to log in again after Keycloak."
