#!/usr/bin/env python3
"""Fail if any platform credential is owned by nobody.

The 2026-11-07 MinIO root rotation broke remote task logging for four days
because airflow/airflow-s3-credentials was a hand-made Secret that no manifest
and no Application owned. Nothing could have reported it. ArgoCD said Synced,
which is true and useless, because a Secret that exists only in the cluster is
invisible to the thing that syncs git. This gate is that missing report.

platform-gitops/secrets-inventory.tsv is the declared state, one row per
credential, naming the store that owns it. This script joins the live cluster
against that table and fails on any disagreement:

    store=sealed             the live Secret must be owned by a SealedSecret
    store=vault-kv           ... by an ExternalSecret reading Vault KV
    store=vault-db           ... by an ExternalSecret reading Vault's database
                             engine, so the credential is issued per lease
    store=none               nothing may own it, and the row must say why.
                             Allowed only for class bootstrap (ArgoCD's own
                             credentials, which it would have to be running to
                             deliver) and class chart (generated per release).
    a live Secret with no row    undeclared and unowned. This is the 11-07 bug.
    a row with no live Secret    the inventory is claiming a credential the
                                 platform does not have.

Only Opaque Secrets in the namespaces the inventory names are considered.
Service-account tokens and helm release state are not credentials this platform
owns, and they carry their own types.

Usage:
    python scripts/secrets_gate.py [--context sentpul] [--inventory PATH]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DEFAULT_INVENTORY = Path("platform-gitops/secrets-inventory.tsv")
DEFAULT_CONTEXT = "sentpul"

OWNER_FOR_STORE = {
    "sealed": "SealedSecret",
    "vault-kv": "ExternalSecret",
    "vault-db": "ExternalSecret",
}
STORELESS_CLASSES = {"bootstrap", "chart"}
COLUMNS = ("namespace", "name", "class", "store", "reason")


def read_inventory(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    lines = path.read_text().splitlines()
    if not lines:
        raise SystemExit(f"{path} is empty")
    header = tuple(lines[0].split("\t"))
    if header != COLUMNS:
        raise SystemExit(f"{path} header is {header}, expected {COLUMNS}")
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for n, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) != len(COLUMNS):
            raise SystemExit(f"{path}:{n} has {len(cells)} cells, expected {len(COLUMNS)}")
        row = dict(zip(COLUMNS, cells, strict=True))
        key = (row["namespace"], row["name"])
        if key in rows:
            raise SystemExit(f"{path}:{n} duplicates {key[0]}/{key[1]}")
        rows[key] = row
    return rows


def read_cluster(context: str, namespaces: set[str]) -> dict[tuple[str, str], str]:
    """Map (namespace, name) -> owning kind, for Opaque Secrets in `namespaces`.

    The owning kind is "" when nothing owns the Secret, which is the state this
    gate exists to catch.
    """
    out = subprocess.run(
        ["kubectl", "--context", context, "get", "secrets", "--all-namespaces", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"kubectl failed: {out.stderr.strip()}")
    live: dict[tuple[str, str], str] = {}
    for item in json.loads(out.stdout)["items"]:
        if item.get("type") != "Opaque":
            continue
        meta = item["metadata"]
        if meta["namespace"] not in namespaces:
            continue
        owners = meta.get("ownerReferences") or []
        live[(meta["namespace"], meta["name"])] = owners[0]["kind"] if owners else ""
    return live


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    ap.add_argument("--context", default=DEFAULT_CONTEXT)
    args = ap.parse_args(argv)

    rows = read_inventory(args.inventory)
    namespaces = {ns for ns, _ in rows}
    live = read_cluster(args.context, namespaces)

    violations: list[str] = []

    for key, row in sorted(rows.items()):
        ns, name = key
        store, klass, reason = row["store"], row["class"], row["reason"]
        if key not in live:
            violations.append(f"{ns}/{name}: declared in the inventory but no such Secret exists")
            continue
        owner = live[key]
        if store == "none":
            if klass not in STORELESS_CLASSES:
                violations.append(
                    f"{ns}/{name}: class {klass} may not have store=none, "
                    f"only {'/'.join(sorted(STORELESS_CLASSES))} may"
                )
            elif not reason.strip():
                violations.append(f"{ns}/{name}: store=none needs a reason in the inventory")
            elif owner:
                violations.append(
                    f"{ns}/{name}: inventory says nothing owns it, but {owner} does"
                )
            continue
        want = OWNER_FOR_STORE.get(store)
        if want is None:
            violations.append(
                f"{ns}/{name}: unknown store '{store}', expected one of "
                f"{'/'.join(sorted(OWNER_FOR_STORE))}/none"
            )
        elif owner != want:
            have = owner or "nothing"
            violations.append(f"{ns}/{name}: store={store} wants a {want} owner, {have} owns it")

    for key in sorted(live):
        if key not in rows:
            ns, name = key
            violations.append(
                f"{ns}/{name}: exists in the cluster with no inventory row. "
                "This is the shape of the 2026-11-07 outage."
            )

    declared = len(rows)
    if violations:
        print(f"FAIL {len(violations)} of {declared} declared credentials disagree with the cluster", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nEvery credential this platform runs on belongs to a store that git "
            "describes. Fix the store, or add the row and say why it has none.",
            file=sys.stderr,
        )
        return 1

    print(f"OK all {declared} declared credentials are owned by the store the inventory names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
