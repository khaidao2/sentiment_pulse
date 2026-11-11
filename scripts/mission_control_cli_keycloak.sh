#!/usr/bin/env bash
# Create the Keycloak client a CLI uses to log a person in.
#
# WHY THE DEVICE FLOW. The CLI runs on a bare-metal box that is often reached
# over SSH, where there is no browser to redirect and no loopback port a
# browser could reach. The device authorization grant is built for exactly
# that: the CLI prints a URL and a short code, the person logs in on whatever
# device has a browser, and the CLI polls until the token appears.
#
# WHY PUBLIC WITH NO SECRET. A secret shipped inside a CLI that every machine
# runs is not a secret. The device flow does not need one, and a public client
# with no service account cannot be used for client_credentials, so a token
# from this client is always a person.
#
# WHAT IT IS NOT FOR. This client logs a human in. It does not talk to the
# chat: the CLI exchanges the resulting token for a Mission Control session
# key, which is machine-scoped and revocable, and uses that afterwards.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

CLIENT_ID="mission-control-cli"

kc_init

kc_upsert_client "$(jq -n '{
  clientId:                  "mission-control-cli",
  name:                      "Mission Control CLI",
  description:               "Logs a person in from a terminal, usually over SSH, with the device authorization grant.",
  enabled:                   true,
  protocol:                  "openid-connect",
  publicClient:              true,
  standardFlowEnabled:       false,
  implicitFlowEnabled:       false,
  directAccessGrantsEnabled: false,
  serviceAccountsEnabled:    false,
  attributes: {
    "oauth2.device.authorization.grant.enabled": "true",
    "pkce.code.challenge.method": "S256"
  }
}')"

uuid="$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "${KC_URL}/admin/realms/${REALM_NAME}/clients?clientId=${CLIENT_ID}" \
  | jq -r '.[0].id // empty')"
[ -n "$uuid" ] || die "client $CLIENT_ID was not created"

device_enabled="$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "${KC_URL}/admin/realms/${REALM_NAME}/clients/${uuid}" \
  | jq -r '.attributes["oauth2.device.authorization.grant.enabled"] // "false"')"
[ "$device_enabled" = "true" ] || die "the device grant is not enabled on $CLIENT_ID"
ok "client $CLIENT_ID is public with the device grant enabled"

# Prove the grant answers before a CLI depends on it. No credential needed,
# which is the point of a public client.
#
# THE CHALLENGE IS NOT OPTIONAL HERE. This client requires PKCE, so Keycloak
# refuses a device request without one:
#   {"error":"invalid_request","error_description":"Missing parameter: code_challenge_method"}
# That is the same trap that broke the Mission Control browser login, where a
# client required PKCE and the proxy in front of it sent none. A probe that
# dropped PKCE would pass here and leave a CLI that cannot log in.
verifier="$(head -c 32 /dev/urandom | base64 | tr -d '=+/' | cut -c1-43)"
challenge="$(printf '%s' "$verifier" | openssl dgst -sha256 -binary | base64 | tr '+/' '-_' | tr -d '=')"
resp="$(curl -sf -d "client_id=${CLIENT_ID}" \
  -d "code_challenge=${challenge}" -d "code_challenge_method=S256" \
  "${KC_URL}/realms/${REALM_NAME}/protocol/openid-connect/auth/device")" \
  || die "the device authorization endpoint refused $CLIENT_ID"
echo "$resp" | jq -e '.device_code and .user_code and .verification_uri' >/dev/null \
  || die "the device endpoint answered without a device_code"
ok "device authorization endpoint issues codes (expires in $(echo "$resp" | jq -r .expires_in)s)"

echo
echo "Mission Control must be told to accept tokens from it:"
echo "  A2A_CLI_CLIENTS=${CLIENT_ID}"
