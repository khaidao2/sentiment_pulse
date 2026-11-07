"""sent-gen — generate infra, Airflow DAGs and ClickHouse DDL from contracts.

This is the only layer that catches exceptions: the library modules raise, and
here we turn those into clean CLI errors (non-zero exit) via click.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import click

from sentpul import config
from sentpul.client import ApicurioClient
from sentpul.contract import Contract, ContractError, iter_contracts, load_contract
from sentpul.ddl import generate_ddl
from sentpul.renderer import render_template, write_rendered_file
from sentpul.validator import load_avro_schema, validate_record


# ── per-kind template context ─────────────────────────────────────────────
def _producer_env(c: Contract) -> list[dict]:
    """Contract values the crawler pod reads from its environment.

    The YAML is the single source of truth; the crawler never parses it, it only
    reads these injected env vars. Canonical Kafka fields plus every key in the
    optional `crawler:` block (upper-cased) flow down here.
    """
    env: dict[str, str] = {
        "KAFKA_TOPIC": c.kafka["topic"],
        "KAFKA_BOOTSTRAP_SERVERS": c.kafka.get("bootstrap_servers", ""),
    }
    for key, value in (c.crawler or {}).items():
        env[key.upper()] = str(value)
    return [{"name": k, "value": v} for k, v in env.items() if v != ""]


def _producer_context(c: Contract) -> dict:
    a = c.airflow
    resources = a.get("resources") or {"cpu": "500m", "memory": "512Mi"}
    return {
        "dag_id": c.dag_id,
        "name": c.name,
        "k8s_name": c.k8s_name,
        "schedule": a["schedule"],
        "namespace": a.get("namespace", "airflow"),
        "image": a["image"],
        "command": a["command"],
        "resources": resources,
        "nodes": a.get("nodes") or [],
        "env": _producer_env(c),
        "env_from_secrets": a.get("env_from_secrets", []),
    }


def _streaming_env(c: Contract) -> list[dict]:
    """Plain env vars for a streaming workload (Kafka coords + workload.env).

    Same idea as the producer: the contract is the single source of truth and the
    workload reads only injected env. `kafka.topic`/`kafka.bootstrap_servers` plus
    every key under the optional `workload.env` block (upper-cased) flow down here.
    Secrets stay in `env_from_secrets`.
    """
    env: dict[str, str] = {
        "KAFKA_TOPIC": c.kafka["topic"],
        "KAFKA_BOOTSTRAP_SERVERS": c.kafka.get("bootstrap_servers", ""),
    }
    for key, value in ((c.workload or {}).get("env") or {}).items():
        env[key.upper()] = str(value)
    return [{"name": k, "value": v} for k, v in env.items() if v != ""]


# An `image:` line in a rendered manifest. `imagePullPolicy:` does not match
# (the char after "image" is "P", not ":").
_IMAGE_LINE_RE = re.compile(r"^\s*image:\s*(\S+)\s*$", re.M)

# The immutable tag CD writes: "sha-" + `git rev-parse --short HEAD`.
_PIN_RE = re.compile(r"^sha-[0-9a-f]{7,40}$")


def _split_image(ref: str) -> tuple[str, str]:
    """Split an image reference into (repository, tag); tag is "" if untagged.

    A ":" that is followed by a "/" is a registry port, not a tag separator.
    """
    repo, sep, tag = ref.rpartition(":")
    if not sep or "/" in tag:
        return ref, ""
    return repo, tag


def _preserve_pinned_image(image: str, manifest_path: Path) -> str:
    """Keep a CD-written `sha-` pin that already sits in the manifest.

    `.github/workflows/build-image.yml` seds the image tag in every generated
    `*_sink.yaml` to the immutable `sha-<sha>` tag (so ArgoCD sees a change and
    rolls the new build out) and commits that. The contract only carries the
    floating `:latest` default, so a plain re-render silently reverted the pin
    and would roll production back to whatever `:latest` points at. Treat an
    existing `sha-` pin for the SAME repository as the newer fact and keep it.

    Falls back to the contract's image when there is no manifest yet (a brand
    new contract), when the repository differs (the contract was deliberately
    repointed), or when the tag is not a pin (`:latest` and friends) — so the
    contract stays the source of truth for everything except this one tag.
    """
    repo, _ = _split_image(image)
    try:
        current = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return image
    for ref in _IMAGE_LINE_RE.findall(current):
        cur_repo, cur_tag = _split_image(ref)
        if cur_repo == repo and _PIN_RE.match(cur_tag):
            return f"{repo}:{cur_tag}"
    return image


def _streaming_context(c: Contract) -> dict:
    w = c.workload
    return {
        "name": c.name,
        "k8s_name": c.k8s_name,
        "namespace": w.get("namespace", "database"),
        "image": _preserve_pinned_image(w["image"], c.manifest_path),
        "command": w["command"],
        "replicas": w.get("replicas", 1),
        "port": w.get("port"),
        "readiness_path": w.get("readiness_path", "/health"),
        "resources": w.get("resources", {}),
        "env": _streaming_env(c),
        "env_from_secrets": w.get("env_from_secrets", []),
    }


def _app_context(c: Contract) -> dict:
    w = c.workload
    return {
        "app_name": c.k8s_name,
        "sync_wave": w.get("sync_wave", "3"),
        "repo_url": config.REPO_URL,
        "manifest_file": c.manifest_path.name,
        "namespace": w.get("namespace", "database"),
    }


def _emit(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        click.echo(f"\n----- {path} -----")
        click.echo(content)
    else:
        write_rendered_file(path, content)
        click.echo(f"  wrote {path}")


def _render_topics(contracts: list[Contract], dry_run: bool) -> None:
    """Emit the reconciler's desired topic set from every contract.

    Aggregate, so it cannot go through _render_contract: one file names every
    topic. Producer and streaming contracts for the same stream declare the
    same topic, so the set is deduped and ordered by name for a stable diff.
    """
    seen: dict[str, dict] = {}
    for c in contracts:
        topic = c.kafka["topic"]
        seen.setdefault(topic, {
            "name": topic,
            "partitions": c.kafka.get("partitions", config.TOPIC_PARTITIONS_DEFAULT),
            "replication_factor": c.kafka.get(
                "replication_factor", config.TOPIC_REPLICATION_FACTOR_DEFAULT),
        })
    topics = [seen[k] for k in sorted(seen)]
    click.echo(f"[topics] {len(topics)} topic(s)")
    _emit(config.TOPICS_MANIFEST,
          render_template("kafka_topics_configmap.yaml.jinja",
                          namespace=config.TOPICS_NAMESPACE, topics=topics),
          dry_run)


def _render_contract(c: Contract, dry_run: bool) -> None:
    click.echo(f"[{c.kind}] {c.name}")
    # ClickHouse DDL for any kind that declares a sink.
    if c.sink:
        schema = load_avro_schema(c.schema_path)
        _emit(c.sql_path, generate_ddl(schema, c.table), dry_run)

    if c.kind == "producer":
        _emit(c.dag_path, render_template("dag.py.jinja", **_producer_context(c)), dry_run)
    elif c.kind == "streaming":
        _emit(c.manifest_path, render_template("k8s_deployment.yaml.jinja", **_streaming_context(c)), dry_run)
        _emit(c.app_path, render_template("argocd_app.yaml.jinja", **_app_context(c)), dry_run)


# ── CLI ───────────────────────────────────────────────────────────────────
@click.group()
def main() -> None:
    """Sentiment Pulse data-contract generator (sent-gen)."""


@main.command()
@click.option("--contracts-dir", default=None, help="Root of contracts/ (default: package contracts dir).")
@click.option("--dry-run", is_flag=True, help="Print generated files instead of writing them.")
def render(contracts_dir, dry_run) -> None:
    """Generate DAGs, k8s manifests, ArgoCD apps and DDL from every contract."""
    try:
        contracts = list(iter_contracts(contracts_dir))
        for c in contracts:
            _render_contract(c, dry_run)
        _render_topics(contracts, dry_run)
    except (ContractError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e


@main.command()
@click.option("--contracts-dir", default=None, help="Root of contracts/.")
def register(contracts_dir) -> None:
    """Register every contract's Avro schema into the registry (idempotent).

    Uses the ccompat subject "<topic>-value" and the RAW schema — the same
    identity the crawler's producer registers, so there is one id per schema.
    """
    client = ApicurioClient()
    try:
        for c in iter_contracts(contracts_dir):
            # RAW schema (not fastavro-parsed) so the serialized string matches
            # the producer's, keeping a single subject version / schema id.
            schema = json.loads(c.schema_path.read_text(encoding="utf-8"))
            subject = f"{c.kafka['topic']}-value"
            schema_id = client.register_schema(subject, schema)
            click.echo(f"registered {subject} (id={schema_id})")
    except (ContractError, FileNotFoundError, RuntimeError, KeyError) as e:
        raise click.ClickException(str(e)) from e


@main.command()
@click.option("--contract", "contract_path", required=True, help="Path to a contract YAML.")
@click.option("--data", "data_path", required=True, help="Path to a JSON record (or list of records).")
def validate(contract_path, data_path) -> None:
    """Validate a JSON record against a contract's Avro schema."""
    try:
        c = load_contract(contract_path)
        schema = load_avro_schema(c.schema_path)
        records = json.loads(Path(data_path).read_text(encoding="utf-8"))
        records = records if isinstance(records, list) else [records]
        for r in records:
            validate_record(r, schema)
        click.echo(f"OK — {len(records)} record(s) valid against '{c.name}'")
    except (ContractError, ValueError, FileNotFoundError) as e:
        raise click.ClickException(str(e)) from e


@main.command(name="run-all")
@click.option("--contracts-dir", default=None, help="Root of contracts/.")
@click.pass_context
def run_all(ctx, contracts_dir) -> None:
    """Register schemas, then render everything."""
    ctx.invoke(register, contracts_dir=contracts_dir)
    ctx.invoke(render, contracts_dir=contracts_dir, dry_run=False)


if __name__ == "__main__":
    main()
