# Seed ytu-staging Odoo for the agent (KAN-26). Run in `odoo shell -d ytu_odoo` with
# YTU_WORKSPACE_ID set; idempotent except for the API key:
# - service user `ytu-agent-staging` (internal, no password: it signs in only with its API key),
#   linked to the workspace through ytu.commerce.service;
# - one ytu.commerce.sku per Velura SKU in YTU_SKUS (space-separated), on_hand 20, plus
#   YTU-TEST-1 with 5 units for the gateway integration test; existing stock is left alone;
# - with ROTATE_KEY=1, a new `rpc` API key for the service user, printed once as `KEY=<key>` for
#   the caller to seal; older keys of that user are removed. Nothing else prints it.
import os

from odoo import Command

env = globals()["env"]  # odoo shell injects the environment
workspace = os.environ["YTU_WORKSPACE_ID"]
Users = env["res.users"].sudo()
user = Users.with_context(active_test=False).search([("login", "=", "ytu-agent-staging")], limit=1)
if not user:
    user = Users.create(
        {
            "name": "YTU agent (staging)",
            "login": "ytu-agent-staging",
            "group_ids": [Command.set([env.ref("base.group_user").id])],
        }
    )
Service = env["ytu.commerce.service"].sudo()
if not Service.search([("workspace", "=", workspace)]):
    Service.create({"workspace": workspace, "user_id": user.id})

Sku = env["ytu.commerce.sku"].sudo()
codes = os.environ.get("YTU_SKUS", "").split()
existing = set(Sku.search([("workspace", "=", workspace)]).mapped("code"))
stock = dict.fromkeys(codes, 20)
stock["YTU-TEST-1"] = 5
Sku.create(
    [
        {"workspace": workspace, "code": c, "on_hand": n}
        for c, n in stock.items()
        if c not in existing
    ]
)

if os.environ.get("ROTATE_KEY") == "1":
    Keys = env["res.users.apikeys"].sudo()
    Keys.search([("user_id", "=", user.id)]).unlink()
    key = (
        Keys.with_user(user)
        .sudo()
        ._generate(scope="rpc", name="ytu-agent staging", expiration_date=False)
    )
    print(f"KEY={key}")
env.cr.commit()
print(f"SEED_OK user={user.id} skus={Sku.search_count([('workspace', '=', workspace)])}")
