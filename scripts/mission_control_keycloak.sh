#!/usr/bin/env bash
# mission_control_keycloak.sh - create or update the Keycloak OIDC client that
# Mission Control authenticates against, plus the realm roles its authorization
# model reads.
#
# Mission Control does its own OIDC rather than sitting behind oauth2-proxy,
# because it needs the user's identity and roles INSIDE the application. Its
# approval flow has to record which human approved a thing, and its workspace
# model has to scope what each user can see. A proxy that only answers "this
# request is authenticated" cannot tell it either of those.
#
# THE SECRET IS NOT IN THIS FILE. grafana_keycloak.sh carries its client secret
# as a default literal, which is the kind of committed credential the 2026-11-07
# rotation work spent four commits removing. Here you either pass one in or the
# script generates one, and it prints the value once so you can seal it.
#
# Usage:
#   ./scripts/mission_control_keycloak.sh
#   MISSION_CONTROL_CLIENT_SECRET=<existing> ./scripts/mission_control_keycloak.sh
#   KC_PORT_FORWARD=1 ./scripts/mission_control_keycloak.sh
#
# Env overrides:
#   MISSION_CONTROL_CLIENT_SECRET   reuse an existing secret instead of generating one
#   MISSION_CONTROL_URL             public URL (default: https://mission-control.sentpul.click)
#   (all KC_* vars from keycloak_common.sh are respected)
#
# This script does NOT publish anything. It declares a redirect URI. Routing that
# hostname through cloudflared is a separate step, and an outward-facing one, so
# it stays an operator decision.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/keycloak_common.sh
source "${SCRIPT_DIR}/keycloak_common.sh"

MC_URL="${MISSION_CONTROL_URL:-https://mission-control.sentpul.click}"
# openssl rather than a literal. Base64 with the URL-unsafe characters stripped,
# so the value survives an env var, a DSN and a URL without quoting surprises.
MC_SECRET="${MISSION_CONTROL_CLIENT_SECRET:-$(openssl rand -base64 36 | tr -d '\n=+/' | cut -c1-32)}"

kc_init

# The callback path the application serves. Changing it here without changing
# the app leaves a client that authenticates and then cannot complete a login.
kc_upsert_client "$(jq -n --arg s "$MC_SECRET" --arg u "$MC_URL" '{
  clientId:                  "mission-control",
  name:                      "Mission Control",
  description:               "Agent Operations Platform",
  enabled:                   true,
  protocol:                  "openid-connect",
  publicClient:              false,
  clientAuthenticatorType:   "client-secret",
  secret:                    $s,
  standardFlowEnabled:       true,
  implicitFlowEnabled:       false,
  directAccessGrantsEnabled: false,
  serviceAccountsEnabled:    false,
  redirectUris:              [($u + "/auth/callback")],
  webOrigins:                [$u],
  attributes: {
    "pkce.code.challenge.method": "S256",
    "post.logout.redirect.uris":  ($u + "/*")
  },
  protocolMappers: [
    {
      name:            "realm-roles",
      protocol:        "openid-connect",
      protocolMapper:  "oidc-usermodel-realm-role-mapper",
      consentRequired: false,
      config: {
        "multivalued":          "true",
        "userinfo.token.claim": "true",
        "id.token.claim":       "true",
        "access.token.claim":   "true",
        "claim.name":           "realm_access.roles",
        "jsonType.label":       "String"
      }
    },
    {
      name:            "email",
      protocol:        "openid-connect",
      protocolMapper:  "oidc-usermodel-property-mapper",
      consentRequired: false,
      config: {
        "user.attribute":       "email",
        "userinfo.token.claim": "true",
        "id.token.claim":       "true",
        "access.token.claim":   "true",
        "claim.name":           "email",
        "jsonType.label":       "String"
      }
    }
  ]
}')"

# Three roles, because the product needs three genuinely different answers.
# A viewer reads. An operator acts on agents and runs. An admin changes policy
# and membership. Approvals are recorded against the human who gave them, so an
# approver has to be distinguishable from a viewer in the token itself.
kc_upsert_realm_role "mission-control-viewer"   "Read sessions, runs, decisions and evidence"
kc_upsert_realm_role "mission-control-operator" "Act on agents and runs, and request approvals"
kc_upsert_realm_role "mission-control-admin"    "Change policy, integrations and workspace membership"

ok "Mission Control Keycloak setup complete."
info "Client secret for mission-control, shown once:"
info "  ${MC_SECRET}"
warn "Seal it, do not commit it in plaintext:"
warn "  kubectl --context sentpul create secret generic mission-control-oidc \\"
warn "    -n mission-control --from-literal=client_secret=<the value above> \\"
warn "    --dry-run=client -o yaml | kubeseal --context sentpul --format yaml \\"
warn "    > platform-gitops/manifests/mission-control-oidc-sealed-secret.yaml"
warn "Then add its row to platform-gitops/secrets-inventory.tsv, or"
warn "scripts/secrets_gate.py will fail on a credential with no declared store."
info "Assign mission-control-admin to yourself before the first login, or the"
info "deployed app will authenticate you with no permissions at all."
