#!/usr/bin/env bash
# skillbridge_keycloak.sh — Keycloak broker setup for the SkillBridge web app.
#
# Makes the app an OIDC client of the data-platform realm and puts Google
# behind Keycloak as an identity provider. The app sends kc_idp_hint=google,
# so Keycloak forwards straight to Google instead of rendering its own login
# page; the first-broker-login flow is configured with no profile review and
# an auto-link step, so the first sign-in does not stop on a Keycloak form
# either. The app's own sign-in page is the only login UI a person sees.
#
# IDEMPOTENT. Re-running updates the client and the IdP and re-asserts the
# flow requirements without rotating the client secret (an existing secret is
# left alone unless SKILLBRIDGE_CLIENT_SECRET is set).
#
# Google's OAuth client must have this redirect URI registered:
#   https://auth.sentpul.click/realms/data-platform/broker/google/endpoint
#
# EXISTING LOCAL ACCOUNTS need one manual check. Auto-linking a Google identity
# to an account that already exists in Keycloak keeps that account's
# emailVerified flag. If it is false, Keycloak's ID token says
# email_verified=false and the app (correctly) refuses to link it. Set the
# account to Email verified in the Keycloak admin UI once before its first
# Google sign-in; accounts created BY the broker get it from trustEmail.
#
# Usage:
#   ./scripts/skillbridge_keycloak.sh
#   KC_PORT_FORWARD=1 ./scripts/skillbridge_keycloak.sh
#   GOOGLE_CLIENT_FILE=~/.config/client_secret_x.json ./scripts/skillbridge_keycloak.sh
#
# The client secret is printed at the end. Seal it into
# platform-gitops/manifests/sentiment-pulse-keycloak-oauth.yaml; never commit
# the plaintext.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

SKILLBRIDGE_URL="${SKILLBRIDGE_URL:-https://wepower.sentpul.click}"
CLIENT_ID="skillbridge-web"
FLOW="SkillBridge first broker login"
GOOGLE_CLIENT_FILE="${GOOGLE_CLIENT_FILE:-}"

if [[ -z "${GOOGLE_CLIENT_ID:-}" || -z "${GOOGLE_CLIENT_SECRET:-}" ]]; then
  [[ -n "$GOOGLE_CLIENT_FILE" && -f "$GOOGLE_CLIENT_FILE" ]] \
    || die "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, or GOOGLE_CLIENT_FILE."
  GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-$(jq -r '.web.client_id' "$GOOGLE_CLIENT_FILE")}"
  GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-$(jq -r '.web.client_secret' "$GOOGLE_CLIENT_FILE")}"
fi

kc_init

api() { printf '%s/admin/realms/%s%s' "$KC_URL" "$REALM_NAME" "$1"; }
enc() { jq -rn --arg v "$1" '$v|@uri'; }

# ---------------------------------------------------------------------------
# 1. The app's confidential client. PKCE is enforced, not merely offered.
# ---------------------------------------------------------------------------
client_spec="$(jq -n --arg id "$CLIENT_ID" --arg url "$SKILLBRIDGE_URL" '{
  clientId: $id,
  name: "SkillBridge web",
  enabled: true,
  publicClient: false,
  clientAuthenticatorType: "client-secret",
  standardFlowEnabled: true,
  implicitFlowEnabled: false,
  directAccessGrantsEnabled: false,
  serviceAccountsEnabled: false,
  protocol: "openid-connect",
  redirectUris: [
    ($url + "/auth/oauth/keycloak/callback"),
    "http://localhost:8000/auth/oauth/keycloak/callback",
    "http://localhost:5173/auth/oauth/keycloak/callback"
  ],
  webOrigins: [$url, "http://localhost:5173"],
  attributes: {
    "pkce.code.challenge.method": "S256",
    "post.logout.redirect.uris": ($url + "/*")
  }
}')"

existing_client="$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "$(api "/clients?clientId=${CLIENT_ID}")" | jq -r '.[0].id // empty')"
if [[ -z "$existing_client" ]]; then
  new_secret="${SKILLBRIDGE_CLIENT_SECRET:-$(openssl rand -hex 16)}"
  jq --arg s "$new_secret" '. + {secret: $s}' <<<"$client_spec" \
    | curl -sf -X POST "$(api "/clients")" \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        --data-binary @- || die "Client create failed."
  ok "Client '${CLIENT_ID}' created."
else
  jq --arg id "$existing_client" '. + {id: $id}' <<<"$client_spec" \
    | curl -sf -X PUT "$(api "/clients/${existing_client}")" \
        -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        --data-binary @- || die "Client update failed."
  ok "Client '${CLIENT_ID}' updated (existing secret kept)."
fi

# ---------------------------------------------------------------------------
# 2. Google as an identity provider. trustEmail is what lets Keycloak mark the
#    brokered address verified; without it the app's resolver refuses the link.
# ---------------------------------------------------------------------------
idp_spec="$(jq -n --arg cid "$GOOGLE_CLIENT_ID" --arg sec "$GOOGLE_CLIENT_SECRET" \
  --arg flow "$FLOW" '{
  alias: "google",
  displayName: "Google",
  providerId: "google",
  enabled: true,
  trustEmail: true,
  storeToken: false,
  linkOnly: false,
  addReadTokenRoleOnCreate: false,
  firstBrokerLoginFlowAlias: $flow,
  config: {
    clientId: $cid,
    clientSecret: $sec,
    defaultScope: "openid email profile",
    syncMode: "IMPORT",
    useJwksUrl: "true",
    disableUserInfo: "false"
  }
}')"

if curl -sf -o /dev/null -H "Authorization: Bearer $TOKEN" \
    "$(api "/identity-provider/instances/google")"; then
  curl -sf -X PUT "$(api "/identity-provider/instances/google")" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    --data-binary "$idp_spec" || die "IdP update failed."
  ok "Google identity provider updated."
else
  curl -sf -X POST "$(api "/identity-provider/instances")" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    --data-binary "$idp_spec" || die "IdP create failed."
  ok "Google identity provider created."
fi

# ---------------------------------------------------------------------------
# 3. A first-broker-login flow with NO Keycloak form:
#      Review Profile              -> DISABLED
#      Handle Existing Account     -> DISABLED
#      Create User If Unique       -> ALTERNATIVE (new people)
#      Automatically Set Existing  -> ALTERNATIVE (verified email matches)
# ---------------------------------------------------------------------------
flow_execs() {
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "$(api "/authentication/flows/$(enc "$FLOW")/executions")"
}

if ! flow_execs >/dev/null 2>&1; then
  curl -sf -X POST \
    "$(api "/authentication/flows/$(enc "first broker login")/copy")" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"newName\":\"${FLOW}\"}" || die "Flow copy failed."
  ok "First-broker-login flow copied."
fi

execs="$(flow_execs)"
set_requirement() {
  local exec_id="$1" requirement="$2"
  [[ -n "$exec_id" ]] || return 0
  curl -sf -o /dev/null -X PUT \
    "$(api "/authentication/flows/$(enc "$FLOW")/executions")" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "{\"id\":\"${exec_id}\",\"requirement\":\"${requirement}\"}" \
    || die "Could not set requirement ${requirement} on ${exec_id}."
}

set_requirement "$(jq -r '.[] | select(.displayName=="Review Profile") | .id' <<<"$execs")" DISABLED
set_requirement "$(jq -r '.[] | select(.displayName|endswith("Handle Existing Account")) | .id' <<<"$execs")" DISABLED

auto_id="$(jq -r '.[] | select(.displayName|ascii_downcase=="automatically set existing user") | .id' <<<"$execs")"
if [[ -z "$auto_id" ]]; then
  subflow="$(enc "${FLOW} User creation or linking")"
  curl -sf -o /dev/null -X POST \
    "$(api "/authentication/flows/${subflow}/executions/execution")" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"provider":"idp-auto-link"}' || die "Auto-link step could not be added."
  execs="$(flow_execs)"
  auto_id="$(jq -r '.[] | select(.displayName|ascii_downcase=="automatically set existing user") | .id' <<<"$execs")"
fi
set_requirement "$auto_id" ALTERNATIVE
ok "First-broker-login flow is UI-less (verified email auto-links)."

echo
info "Keycloak client secret (seal it; do NOT commit it):"
kc_print_secret "$CLIENT_ID"
echo
info "Google console must list this redirect URI:"
echo "    https://auth.sentpul.click/realms/${REALM_NAME}/broker/google/endpoint"
ok "SkillBridge Keycloak setup complete."
