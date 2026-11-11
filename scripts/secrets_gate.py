#!/usr/bin/env python3
"""Fail if any platform credential is owned by nobody.

The 2026-11-07 MinIO root rotation broke Airflow's remote task logging for four
days because airflow/airflow-s3-credentials was a hand-made Secret that no
manifest and no Application owned. Nothing could have reported it. ArgoCD said
Synced, which is true and useless, because a Secret that exists only in the
cluster is invisible to the thing that syncs git. This gate is that missing
report.

platform-gitops/credentials/ is the declared state, ONE FILE PER CREDENTIAL,
named <namespace>.<name>.yaml. This script joins those declarations against the
live cluster and fails on any disagreement:

    store=sealed             the live Secret must be owned by a SealedSecret
    store=vault-kv           ... by an ExternalSecret reading Vault KV
    store=vault-db           ... by an ExternalSecret reading Vault's database
                             engine, so the credential is issued per lease
    store=none               nothing may own it, and the file must say why.
                             Allowed only for class bootstrap (ArgoCD's own
                             credentials, which it would have to be running to
                             deliver) and class chart (generated per release).
    a live Secret with no file   undeclared and unowned. This is the 11-07 bug.
    a file with no live Secret    the declaration claims a credential the
                                  platform does not have.

WHY ONE FILE PER CREDENTIAL. This was a single TSV until 2026-11-11. Every
change to any credential appended a line to it, so every branch that touched a
credential conflicted with every other, and resolving those conflicts by hand
risks dropping a row. That happened: a conflict on the TSV was the only thing
blocking a pull request whose content was already merged, and a hand resolution
would have silently deleted declarations for credentials that were live. One
file per credential has no shared line to conflict on, and a new credential is a
new file rather than an edit.

The filename must match the declaration inside it. A file named for one
credential that declares another is a rename someone did halfway.

Requires PyYAML, which this repo's tooling already depends on.

Usage:
    python scripts/secrets_gate.py [--context sentpul] [--credentials DIR]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_CREDENTIALS = Path("platform-gitops/credentials")
DEFAULT_CONTEXT = "sentpul"

OWNER_FOR_STORE = {
    "sealed": "SealedSecret",
    "vault-kv": "ExternalSecret",
    "vault-db": "ExternalSecret",
}
STORELESS_CLASSES = {"bootstrap", "chart"}
FIELDS = ("namespace", "name", "class", "store", "reason")
# A registry pull credential is a credential. Keeping this to Opaque alone is
# what let webapp/ghcr-pull sit unowned and unseen.
IN_SCOPE_TYPES = {"Opaque", "kubernetes.io/dockerconfigjson"}


def read_declarations(directory: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not directory.is_dir():
        raise SystemExit(f"{directory} is not a directory")
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise SystemExit(f"{directory} declares no credentials")
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for path in files:
        doc = yaml.safe_load(path.read_text())
        if not isinstance(doc, dict):
            raise SystemExit(f"{path} is not a YAML mapping")
        missing = [f for f in FIELDS if f not in doc]
        if missing:
            raise SystemExit(f"{path} is missing {', '.join(missing)}")
        row = {f: "" if doc[f] is None else str(doc[f]) for f in FIELDS}
        expected = f"{row['namespace']}.{row['name']}.yaml"
        if path.name != expected:
            raise SystemExit(
                f"{path} declares {row['namespace']}/{row['name']}, so it should be named {expected}"
            )
        key = (row["namespace"], row["name"])
        if key in rows:
            raise SystemExit(f"{path} duplicates {key[0]}/{key[1]}")
        rows[key] = row
    return rows


def read_cluster(context: str, namespaces: set[str]) -> dict[tuple[str, str], list[str]]:
    """Map (namespace, name) -> every owning kind, for in-scope Secrets in `namespaces`.

    Every kind, not just ownerReferences[0]. A Secret can carry more than one
    owner, and reading only the first lets the gate pass a Secret whose declared
    store is nowhere in the list. An empty list is the state this gate exists to
    catch.
    """
    out = subprocess.run(
        ["kubectl", "--context", context, "get", "secrets", "--all-namespaces", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"kubectl failed: {out.stderr.strip()}")
    live: dict[tuple[str, str], list[str]] = {}
    for item in json.loads(out.stdout)["items"]:
        if item.get("type") not in IN_SCOPE_TYPES:
            continue
        meta = item["metadata"]
        if meta["namespace"] not in namespaces:
            continue
        owners = meta.get("ownerReferences") or []
        live[(meta["namespace"], meta["name"])] = [o["kind"] for o in owners]
    return live


def compare(
    rows: dict[tuple[str, str], dict[str, str]],
    live: dict[tuple[str, str], list[str]],
) -> list[str]:
    """Join the declared state against the live one and name every disagreement.

    Pure, so the branches can be exercised without a cluster.
    """
    violations: list[str] = []

    for key, row in sorted(rows.items()):
        ns, name = key
        store, klass, reason = row["store"], row["class"], row["reason"]
        if key not in live:
            violations.append(f"{ns}/{name}: declared but no such Secret exists")
            continue
        owners = live[key]
        have = ", ".join(owners) if owners else "nothing"
        if store == "none":
            if klass not in STORELESS_CLASSES:
                violations.append(
                    f"{ns}/{name}: class {klass} may not have store=none, "
                    f"only {'/'.join(sorted(STORELESS_CLASSES))} may"
                )
            elif not reason.strip():
                violations.append(f"{ns}/{name}: store=none needs a reason")
            elif owners:
                violations.append(f"{ns}/{name}: declared as owned by nothing, but {have} owns it")
            continue
        want = OWNER_FOR_STORE.get(store)
        if want is None:
            violations.append(
                f"{ns}/{name}: unknown store '{store}', expected one of "
                f"{'/'.join(sorted(OWNER_FOR_STORE))}/none"
            )
        elif want not in owners:
            violations.append(f"{ns}/{name}: store={store} wants a {want} owner, {have} owns it")

    for key in sorted(live):
        if key not in rows:
            ns, name = key
            violations.append(
                f"{ns}/{name}: exists in the cluster with no declaration. "
                "This is the shape of the 2026-11-07 outage."
            )

    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    ap.add_argument("--context", default=DEFAULT_CONTEXT)
    args = ap.parse_args(argv)

    rows = read_declarations(args.credentials)
    violations = compare(rows, read_cluster(args.context, {ns for ns, _ in rows}))

    declared = len(rows)
    if violations:
        print(
            f"FAIL {len(violations)} of {declared} declared credentials disagree with the cluster",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            f"\nEvery credential this platform runs on belongs to a store that git "
            f"describes. Fix the store, or add a file in {args.credentials} saying why "
            f"it has none.",
            file=sys.stderr,
        )
        return 1

    print(f"OK all {declared} declared credentials are owned by the store their file names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
