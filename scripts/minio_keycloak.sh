#!/usr/bin/env bash
# minio_keycloak.sh — create/update MinIO's Keycloak OIDC client.
#
# Creates the `minio` client in the data-platform realm (idempotent). MinIO is
# already wired for OIDC in manifests/minio.yaml (MINIO_IDENTITY_OPENID_*), but
# that only works once this client actually exists in Keycloak.
#
# The hardcoded `policy=consoleAdmin` claim mapper matches
# MINIO_IDENTITY_OPENID_CLAIM_NAME=policy, granting SSO logins the consoleAdmin
# MinIO policy.
#
# Usage:
#   ./scripts/minio_keycloak.sh
#   KC_PORT_FORWARD=1 ./scripts/minio_keycloak.sh

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
MINIO_URL="${MINIO_URL:-https://minio.sentpul.click}"

kc_init

kc_upsert_client "$(jq -n --arg u "$MINIO_URL" '{
  clientId: "minio",
  name: "MinIO Console Client",
  enabled: true,
  protocol: "openid-connect",
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  standardFlowEnabled: true,
  redirectUris: [($u + "/*"), ($u + "/oauth_callback")],
  webOrigins: [$u],
  protocolMappers: [{
    name: "minio-policy",
    protocol: "openid-connect",
    protocolMapper: "oidc-hardcoded-claim-mapper",
    consentRequired: false,
    config: {
      "claim.name": "policy",
      "claim.value": "consoleAdmin",
      "jsonType.label": "String",
      "id.token.claim": "true",
      "access.token.claim": "true",
      "userinfo.token.claim": "true"
    }
  }]
}')"

info "Client secret:"
kc_print_secret minio
ok "MinIO Keycloak setup complete."
