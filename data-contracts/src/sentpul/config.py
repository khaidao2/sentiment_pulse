"""Filesystem roots for sentpul. Single source of truth for where things live.

This package sits at  <repo>/data-contracts/src/sentpul/  — so:
  BASE_DIR  = <repo>/data-contracts
  REPO_ROOT = <repo>
The generated DAGs and GitOps manifests live at the repo root, while schemas
and generated SQL live under data-contracts/.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent   # data-contracts/
REPO_ROOT = BASE_DIR.parent                                # repo root

# GitOps repo the generated ArgoCD Applications point at.
REPO_URL = "https://github.com/khaidao2/sentiment_pulse"

# ── inputs ────────────────────────────────────────────────────────────────
CONTRACTS_DIR = BASE_DIR / "contracts"
SCHEMA_DIR = BASE_DIR / "schemas"

# ── outputs ───────────────────────────────────────────────────────────────
SQL_DIR = BASE_DIR / "generated" / "sql"
DAGS_DIR = REPO_ROOT / "dags"
MANIFESTS_DIR = REPO_ROOT / "platform-gitops" / "manifests"
APPS_DIR = REPO_ROOT / "platform-gitops" / "apps"

# NOTE: the Jinja templates dir is intentionally NOT here — renderer.py owns it
# (it resolves templates relative to its own file, which is the correct root).
