#!/usr/bin/env bash
# Create the Keycloak client a peer machine uses to submit missions over A2A.
#
# WHY A CLIENT OF ITS OWN. Mission Control's existing `mission-control` client
# is for a person logging in through a browser. A peer is a machine with no
# browser and no user, so it needs the client credentials grant, and that is a
# capability you do not want attached to the client humans use. Separate client,
# separate secret, separate revocation.
#
# WHAT THIS GRANTS. A token from this client proves the caller is this peer. It
# does not decide what the peer may do. Mission Control holds that rule in
# A2A_ALLOWED_CLIENTS and A2A_BACKENDS, because a mission runs an agent that
# writes files and "any valid token in the realm" is the wrong rule.
#
# The secret is never printed. It is sealed for the cluster's record and, with
# --install-on <host>, written root-only to the peer that needs it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

CLIENT_ID="mission-control-peer"
INSTALL_ON=""
while [ $# -gt 0 ]; do
  case "$1" in
    --install-on) INSTALL_ON="$2"; shift 2;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) die "unknown argument: $1";;
  esac
done

kc_init

kc_upsert_client "$(jq -n '{
  clientId:                  "mission-control-peer",
  name:                      "Mission Control A2A peer",
  description:               "A machine that submits missions over A2A. No browser, no user.",
  enabled:                   true,
  protocol:                  "openid-connect",
  publicClient:              false,
  clientAuthenticatorType:   "client-secret",
  standardFlowEnabled:       false,
  implicitFlowEnabled:       false,
  directAccessGrantsEnabled: false,
  serviceAccountsEnabled:    true
}')"

d="$(mktemp -d)"; trap 'rm -rf "$d"' EXIT; umask 077

uuid="$(curl -sf -H "Authorization: Bearer $TOKEN" \
  "${KC_URL}/admin/realms/${REALM_NAME}/clients?clientId=${CLIENT_ID}" \
  | jq -r '.[0].id // empty')"
[ -n "$uuid" ] || die "client $CLIENT_ID was not created"

curl -sf -H "Authorization: Bearer $TOKEN" \
  "${KC_URL}/admin/realms/${REALM_NAME}/clients/${uuid}/client-secret" \
  | jq -r '.value' | tr -d '\n' > "$d/secret"
[ -s "$d/secret" ] || die "Keycloak returned no secret for $CLIENT_ID"
ok "client $CLIENT_ID ready, secret sha $(sha256sum < "$d/secret" | cut -c1-10)"

# Prove the grant works before anyone depends on it.
curl -sf -d grant_type=client_credentials -d "client_id=${CLIENT_ID}" \
  --data-urlencode "client_secret@$d/secret" \
  "${KC_URL}/realms/${REALM_NAME}/protocol/openid-connect/token" \
  | jq -e '.access_token | length > 0' >/dev/null \
  || die "the client credentials grant does not work for $CLIENT_ID"
ok "client credentials grant verified"

if [ -n "$INSTALL_ON" ]; then
  # Root-only on the peer, because that machine has no sealed-secrets
  # controller and no cluster membership. This is the one plaintext copy and it
  # exists because the peer is bare metal, not a pod.
  ssh -o BatchMode=yes "root@${INSTALL_ON}" 'umask 077; mkdir -p /root/a2a-verify' </dev/null
  ssh -o BatchMode=yes "root@${INSTALL_ON}" 'umask 077; cat > /root/a2a-verify/peer-client-secret' < "$d/secret"
  ok "secret installed root-only on ${INSTALL_ON}:/root/a2a-verify/peer-client-secret"
fi

echo
echo "Mission Control must be told to accept it:"
echo "  A2A_ISSUER=${KC_URL}/realms/${REALM_NAME}"
echo "  A2A_ALLOWED_CLIENTS=${CLIENT_ID}"
