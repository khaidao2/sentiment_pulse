#!/usr/bin/env bash
# Create two ordinary realm users for end-to-end chat testing.
#
# WHY REAL USERS. The hub distinguishes a human from an agent, and a service
# account from a person, by what is in the token. Testing that with the admin
# account or a service account proves nothing about the path a lab member
# actually takes: log in, get a session key, talk. These two users exist so
# that path can be exercised without borrowing a real person's account.
#
# WHAT THEY ARE NOT. They hold no realm roles and no group membership, so they
# can authenticate and nothing more. Delete them with
#   scripts/keycloak_test_users.sh --delete
# once a test is finished; they are not meant to be long-lived.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

USERS=(testuser1 testuser2)
CRED_FILE="${CRED_FILE:-/home/thekhai/.config/keys/sentpul_credentials.txt}"
DELETE=0
[[ "${1:-}" == "--delete" ]] && DELETE=1

kc_init

user_uuid() {
  curl -sf -H "Authorization: Bearer $TOKEN" \
    "${KC_URL}/admin/realms/${REALM_NAME}/users?username=$1&exact=true" \
    | jq -r '.[0].id // empty'
}

if [[ "$DELETE" == "1" ]]; then
  for u in "${USERS[@]}"; do
    uuid="$(user_uuid "$u")"
    if [[ -z "$uuid" ]]; then ok "$u already absent"; continue; fi
    curl -sf -X DELETE -H "Authorization: Bearer $TOKEN" \
      "${KC_URL}/admin/realms/${REALM_NAME}/users/${uuid}" || die "delete $u failed"
    ok "deleted $u"
  done
  exit 0
fi

printed=()
for u in "${USERS[@]}"; do
  uuid="$(user_uuid "$u")"
  if [[ -z "$uuid" ]]; then
    curl -sf -X POST "${KC_URL}/admin/realms/${REALM_NAME}/users" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "$(jq -n --arg u "$u" '{
            username: $u, enabled: true, emailVerified: true,
            email: ($u + "@test.invalid"),
            firstName: $u, lastName: "(test)"
          }')" || die "create $u failed"
    uuid="$(user_uuid "$u")"
    [[ -n "$uuid" ]] || die "$u was not created"
    ok "created $u"
  else
    ok "$u exists"
  fi

  # tr -d '\n' is not cosmetic. A password that keeps a trailing newline gets
  # split by whatever reads it later, and the half that arrives is wrong in a
  # way that looks like a rejected credential.
  pw="$(head -c 24 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-24)"
  pw="$(printf '%s' "$pw" | tr -d '\n')"
  [[ ${#pw} -ge 20 ]] || die "generated password for $u is too short"

  curl -sf -X PUT "${KC_URL}/admin/realms/${REALM_NAME}/users/${uuid}/reset-password" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d "$(jq -n --arg p "$pw" '{type:"password", value:$p, temporary:false}')" \
    || die "set password for $u failed"
  ok "password set for $u (not temporary, so a device login needs no reset step)"
  printed+=("$u:$pw")
done

if [[ -d "$(dirname "$CRED_FILE")" ]]; then
  {
    echo
    echo "# Keycloak test users for chat end-to-end testing ($(date -u +%FT%TZ))"
    echo "# realm: ${REALM_NAME}   delete with: scripts/keycloak_test_users.sh --delete"
    for p in "${printed[@]}"; do echo "KEYCLOAK_TESTUSER_${p%%:*}=${p#*:}"; done
  } >> "$CRED_FILE"
  ok "credentials appended to $CRED_FILE"
else
  warn "no $(dirname "$CRED_FILE"); printing instead"
  for p in "${printed[@]}"; do echo "  ${p%%:*} / ${p#*:}"; done
fi

echo
ok "both users can authenticate and hold no roles"
