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

# NO CLIENT SECRET HERE. This script used to carry one as a shell default, in
# a PUBLIC repository, and omitting `secret` from the client representation is
# what makes that unnecessary: Keycloak leaves an existing client's secret
# alone on update. Keeping the default was also a live footgun, because a
# re-run would have reset the client secret to a value anyone could read.
#
# To rotate a client secret, use scripts/rotate_keycloak_client_secret.sh,
# which regenerates it in Keycloak, seals it, and never prints it.
CLOUDBEAVER_URL="${CLOUDBEAVER_URL:-https://cloudbeaver.sentpul.click}"

kc_init

kc_upsert_client "$(jq -n --arg u "$CLOUDBEAVER_URL" '{
  clientId: "cloudbeaver",
  name: "CloudBeaver (oauth2-proxy)",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  standardFlowEnabled: true,
  redirectUris: [($u + "/oauth2/callback")],
  webOrigins: [$u]
}')"

info "Client secret:"
kc_print_secret cloudbeaver
ok "CloudBeaver Keycloak setup complete."
warn "One-time: open CloudBeaver as admin (cbadmin) and enable anonymous access"
warn "so users are not prompted to log in again after Keycloak."
