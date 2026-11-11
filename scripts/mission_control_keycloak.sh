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

# SEAL IT HERE, DO NOT PRINT IT. The first version of this script printed the
# secret and asked the operator to paste it into a kubeseal pipeline. That put a
# live credential on a terminal, into shell history, and into any transcript of
# the session, to save writing fifteen lines. The script already holds the
# value, so the script is what should seal it.
MC_NS="${MISSION_CONTROL_NAMESPACE:-mission-control}"
MC_OUT="platform-gitops/manifests/mission-control-oidc-sealed-secret.yaml"
MC_CREDS="platform-gitops/credentials"

if ! command -v kubeseal >/dev/null; then
  warn "kubeseal is not installed, so the client secret could not be sealed."
  warn "Re-run this script on a machine with kubeseal. Nothing was printed."
  exit 1
fi
if ! kubectl --context "$KC_KUBE_CONTEXT" get ns "$MC_NS" >/dev/null 2>&1; then
  warn "namespace $MC_NS does not exist on context $KC_KUBE_CONTEXT, so the"
  warn "client secret could not be sealed. Nothing was printed. Create the"
  warn "namespace, then re-run with MISSION_CONTROL_CLIENT_SECRET set to reuse"
  warn "this client, or re-run plain to roll a new secret."
  exit 1
fi

_d="$(mktemp -d)"; umask 077
# keycloak_common.sh already traps EXIT to kill its port-forward, and a bare
# `trap ... EXIT` here would replace that handler rather than add to it, leaking
# the forwarded port for the life of the shell. So call both.
trap '_kc_cleanup; rm -rf "$_d"' EXIT
printf %s "$MC_SECRET" > "$_d/client_secret"
kubectl --context "$KC_KUBE_CONTEXT" create secret generic mission-control-oidc \
    -n "$MC_NS" --from-file="client_secret=$_d/client_secret" --dry-run=client -o yaml \
  | kubeseal --context "$KC_KUBE_CONTEXT" --format yaml > "$_d/sealed.yaml"
grep -q 'kind: SealedSecret' "$_d/sealed.yaml" || die "kubeseal produced no SealedSecret"

{ echo "# Generated by scripts/mission_control_keycloak.sh on $(date -Is)."
  echo "# The Keycloak client secret for the mission-control OIDC client."
  echo "# Sealed by the script that created it, so the value never reached a"
  echo "# terminal, a shell history or a session transcript. To roll it, re-run"
  echo "# the script: it updates the client in Keycloak and reseals here."
  grep -v '^\s*creationTimestamp' "$_d/sealed.yaml"; } > "$MC_OUT"
kubectl --context "$KC_KUBE_CONTEXT" apply -f "$MC_OUT" >/dev/null

for _ in $(seq 1 30); do
  kubectl --context "$KC_KUBE_CONTEXT" -n "$MC_NS" get secret mission-control-oidc >/dev/null 2>&1 && break
  sleep 1
done
kubectl --context "$KC_KUBE_CONTEXT" -n "$MC_NS" get secret mission-control-oidc >/dev/null 2>&1 \
  || die "mission-control-oidc never materialised, so do NOT commit $MC_OUT"
ok "sealed, applied and materialised: $MC_NS/mission-control-oidc -> $MC_OUT"

# The gate fails on a credential with no declaration, so declare it here rather
# than leaving a step for a person to forget. One file per credential, named for
# the credential it declares, which is what the gate checks.
MC_DECL="$MC_CREDS/${MC_NS}.mission-control-oidc.yaml"
if [ -d "$MC_CREDS" ] && [ ! -f "$MC_DECL" ]; then
  { echo "namespace: $MC_NS"
    echo "name: mission-control-oidc"
    echo "class: persistent"
    echo "store: sealed"
    echo 'reason: ""'; } > "$MC_DECL"
  ok "declared $MC_NS/mission-control-oidc in $MC_DECL"
fi
warn "Commit $MC_OUT and $MC_DECL, or ArgoCD selfHeal reverts this."
info "Assign mission-control-admin to yourself before the first login, or the"
info "deployed app will authenticate you with no permissions at all."
