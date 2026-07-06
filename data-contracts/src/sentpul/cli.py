"""sent-gen — generate infra, Airflow DAGs and ClickHouse DDL from contracts.

This is the only layer that catches exceptions: the library modules raise, and
here we turn those into clean CLI errors (non-zero exit) via click.
"""

from __future__ import annotations

import json
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


def _streaming_context(c: Contract) -> dict:
    w = c.workload
    return {
        "name": c.name,
        "k8s_name": c.k8s_name,
        "namespace": w.get("namespace", "database"),
        "image": w["image"],
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
        for c in iter_contracts(contracts_dir):
            _render_contract(c, dry_run)
    except (ContractError, FileNotFoundError) as e:
        raise click.ClickException(str(e))


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
        raise click.ClickException(str(e))


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
        raise click.ClickException(str(e))


@main.command(name="run-all")
@click.option("--contracts-dir", default=None, help="Root of contracts/.")
@click.pass_context
def run_all(ctx, contracts_dir) -> None:
    """Register schemas, then render everything."""
    ctx.invoke(register, contracts_dir=contracts_dir)
    ctx.invoke(render, contracts_dir=contracts_dir, dry_run=False)


if __name__ == "__main__":
    main()
