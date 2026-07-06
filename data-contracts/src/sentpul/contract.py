"""Load and validate a YAML data contract into a typed `Contract`.

The Contract is the single source of truth for every derived name and output
path (table name, k8s resource name, DAG id, where each generated file goes),
so no other module string-builds those by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

from sentpul import config

VALID_KINDS = ("producer", "streaming")

# Contract names become file names, ClickHouse tables and (kebab-ized) DNS-1123
# k8s names — keep them to a safe snake_case slug.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ContractError(ValueError):
    """A contract YAML is malformed or missing required fields."""


@dataclass
class Contract:
    name: str                       # snake_case slug, e.g. "vn_stock"
    kind: str                       # "producer" | "streaming"
    schema_file: str                # repo-relative path to the .avsc
    apicurio: dict[str, Any]
    kafka: dict[str, Any]
    airflow: dict[str, Any] | None = None    # producer only
    workload: dict[str, Any] | None = None   # streaming only
    sink: dict[str, Any] | None = None       # optional (ClickHouse sink)
    crawler: dict[str, Any] | None = None    # optional per-source scraping knobs

    # ── derived names (single source of truth) ────────────────────────────
    @property
    def k8s_name(self) -> str:
        """DNS-1123 name for k8s resources / ArgoCD app (no underscores)."""
        return self.name.replace("_", "-")

    @property
    def dag_id(self) -> str:
        return f"crawl_{self.name}"

    @property
    def table(self) -> str:
        """ClickHouse table name; falls back to the contract name."""
        clickhouse = (self.sink or {}).get("clickhouse", {})
        return clickhouse.get("table", self.name)

    # ── input path ────────────────────────────────────────────────────────
    @property
    def schema_path(self) -> Path:
        return config.REPO_ROOT / self.schema_file

    # ── output paths ──────────────────────────────────────────────────────
    @property
    def sql_path(self) -> Path:
        return config.SQL_DIR / f"{self.name}.sql"

    @property
    def dag_path(self) -> Path:
        return config.DAGS_DIR / f"{self.name}_dag.py"

    @property
    def manifest_path(self) -> Path:
        return config.MANIFESTS_DIR / f"{self.name}.yaml"

    @property
    def app_path(self) -> Path:
        return config.APPS_DIR / f"{self.name}.yaml"


def load_contract(path: str | Path) -> Contract:
    """Parse and validate a single contract YAML file."""
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ContractError(f"{path}: contract must be a YAML mapping")

    for key in ("name", "kind", "schema_file", "apicurio", "kafka"):
        if key not in raw:
            raise ContractError(f"{path}: missing required key '{key}'")

    name = raw["name"]
    if not (isinstance(name, str) and _NAME_RE.match(name)):
        raise ContractError(
            f"{path}: name must be a snake_case slug matching {_NAME_RE.pattern!r}, "
            f"got {name!r}"
        )

    kind = raw["kind"]
    if kind not in VALID_KINDS:
        raise ContractError(f"{path}: kind must be one of {VALID_KINDS}, got {kind!r}")

    contract = Contract(
        name=name,
        kind=kind,
        schema_file=raw["schema_file"],
        apicurio=raw["apicurio"],
        kafka=raw["kafka"],
        airflow=raw.get("airflow"),
        workload=raw.get("workload"),
        sink=raw.get("sink"),
        crawler=raw.get("crawler"),
    )
    _validate_kind(contract, path)
    return contract


def _validate_kind(c: Contract, path: Path) -> None:
    """Enforce the per-kind required blocks."""
    if c.kind == "producer":
        if not c.airflow:
            raise ContractError(f"{path}: producer contract requires an 'airflow' block")
    elif c.kind == "streaming":
        if not c.workload:
            raise ContractError(f"{path}: streaming contract requires a 'workload' block")
        # 16GB single-node guard: an always-on Deployment must cap its RAM.
        limits = (c.workload.get("resources") or {}).get("limits")
        if not limits:
            raise ContractError(
                f"{path}: streaming contract requires workload.resources.limits "
                f"(single 16GB node — always-on workloads must cap RAM)"
            )


def iter_contracts(root: str | Path | None = None) -> Iterator[Contract]:
    """Yield every real contract under `root` (defaults to contracts/).

    Skips `default.yaml` — those are copy-me templates, not live contracts.
    """
    root = Path(root or config.CONTRACTS_DIR)
    for p in sorted(root.rglob("*.yaml")):
        if p.name == "default.yaml":
            continue
        yield load_contract(p)
