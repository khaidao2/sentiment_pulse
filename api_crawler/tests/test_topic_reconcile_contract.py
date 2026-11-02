"""The topic reconciler's desired state must match what the contracts declare.

Topics used to be declared nowhere, so they existed only because a producer had
auto-created them at some point. kafka-topic-reconcile.yaml now declares them,
which makes its ConfigMap a second copy of a fact the contracts already own.
This is the test that keeps the copy honest -- the same role
test_courses_contract.py plays for the bronze view's declared columns: the
contracts stay the single source of truth, and adding a contract without adding
its topic to the reconciler fails here rather than as a crash-looping sink at
3am.

The expected set is derived from the contract files via iter_contracts(), never
hardcoded, so a fifth contract is picked up automatically.

These tests also pin the reconciler's two safety properties -- create-only, and
a validation pattern that cannot be talked into touching an undeclared topic --
so a later edit that loosens either one fails CI.
"""

import re
from pathlib import Path

import pytest
import yaml

from sentpul.contract import iter_contracts

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "data-contracts" / "contracts"
_MANIFEST = _ROOT / "platform-gitops" / "manifests" / "kafka-topic-reconcile.yaml"
_TOPICS = _ROOT / "platform-gitops" / "manifests" / "kafka-topic-reconcile-topics.yaml"

# name:partitions:replicationFactor -- must stay in step with the grep -E
# pattern inside reconcile.sh, which test_validation_pattern_matches_script
# asserts directly.
_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*):([1-9][0-9]*):([1-9][0-9]*)$")


@pytest.fixture(scope="module")
def manifest_docs() -> list[dict]:
    docs = [d for d in yaml.safe_load_all(_MANIFEST.read_text(encoding="utf-8")) if d]
    assert docs, f"{_MANIFEST} parsed to nothing"
    return docs


@pytest.fixture(scope="module")
def configmap(manifest_docs) -> dict:
    """The hand-written mechanism ConfigMap: the script, and nothing else."""
    cms = [d for d in manifest_docs if d.get("kind") == "ConfigMap"]
    assert len(cms) == 1, f"expected exactly one ConfigMap, found {len(cms)}"
    return cms[0]


@pytest.fixture(scope="module")
def topics_doc() -> dict:
    """The generated desired-state ConfigMap."""
    doc = yaml.safe_load(_TOPICS.read_text(encoding="utf-8"))
    assert doc["kind"] == "ConfigMap"
    return doc


@pytest.fixture(scope="module")
def cronjob(manifest_docs) -> dict:
    cjs = [d for d in manifest_docs if d.get("kind") == "CronJob"]
    assert len(cjs) == 1, f"expected exactly one CronJob, found {len(cjs)}"
    return cjs[0]


@pytest.fixture(scope="module")
def script(configmap) -> str:
    return configmap["data"]["reconcile.sh"]


@pytest.fixture(scope="module")
def declared(topics_doc) -> dict[str, tuple[int, int]]:
    """{topic: (partitions, replicationFactor)} parsed from the generated file."""
    out: dict[str, tuple[int, int]] = {}
    for lineno, raw in enumerate(topics_doc["data"]["topics"].splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = _ENTRY.match(line)
        assert m, f"topics:{lineno} does not match name:partitions:rf -- {raw!r}"
        assert m.group(1) not in out, f"topics:{lineno} duplicate topic {m.group(1)}"
        out[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    assert out, "no topics declared"
    return out


@pytest.fixture(scope="module")
def contract_topics() -> set[str]:
    """Every kafka.topic across the contracts (default.yaml is skipped)."""
    topics = {c.kafka["topic"] for c in iter_contracts(_CONTRACTS)}
    assert topics, "no contracts found -- fixture is not exercising anything"
    return topics


# ── the copy stays honest ─────────────────────────────────────────────────


def test_declared_topics_match_contracts(declared, contract_topics):
    """The generated file must still match the contracts it came from.

    This now catches a STALE generated file -- someone added a contract and
    did not re-run `sent-gen render` -- rather than a forgotten hand-edit.
    """
    assert set(declared) == contract_topics, (
        "kafka-topic-reconcile-topics.yaml is stale. Run `sent-gen render`. "
        f"missing from reconciler: {sorted(contract_topics - set(declared))}; "
        f"declared but no contract: {sorted(set(declared) - contract_topics)}"
    )


def test_the_generated_file_is_what_the_generator_produces(topics_doc):
    """Byte-for-byte: regenerate in memory and compare.

    Stops a hand-edit of an AUTO-GENERATED file from surviving review, and
    proves the committed file is current.
    """
    import sys
    sys.path.insert(0, str(_ROOT / "data-contracts" / "src"))
    from sentpul import config
    from sentpul.cli import _render_topics
    import click.testing

    seen = {}
    for c in iter_contracts(_CONTRACTS):
        seen.setdefault(c.kafka["topic"], {
            "name": c.kafka["topic"],
            "partitions": c.kafka.get("partitions", config.TOPIC_PARTITIONS_DEFAULT),
            "replication_factor": c.kafka.get(
                "replication_factor", config.TOPIC_REPLICATION_FACTOR_DEFAULT),
        })
    from sentpul.renderer import render_template
    expected = render_template("kafka_topics_configmap.yaml.jinja",
                               namespace=config.TOPICS_NAMESPACE,
                               topics=[seen[k] for k in sorted(seen)])
    assert _TOPICS.read_text(encoding="utf-8") == expected, (
        "kafka-topic-reconcile-topics.yaml differs from `sent-gen render` output")


def test_the_generated_file_is_marked_generated(topics_doc):
    head = _TOPICS.read_text(encoding="utf-8").splitlines()[0]
    assert "AUTO-GENERATED by sent-gen" in head, (
        "the repo convention is that generated manifests say so on line 1")


def test_the_mechanism_holds_no_topic_list(configmap):
    """The duplicate source of truth must stay gone."""
    assert "topics" not in configmap["data"]
    assert sorted(configmap["data"]) == ["reconcile.sh"]


def test_partitions_and_replication_are_single_broker(declared):
    """The chart runs replicaCount=1, so RF>1 cannot be satisfied.

    Every existing sentiment-pulse.* topic reports PartitionCount 1 /
    ReplicationFactor 1; these values were read off the broker, not chosen.
    """
    for topic, (partitions, rf) in declared.items():
        assert rf == 1, f"{topic}: replicationFactor {rf} exceeds the single broker"
        assert partitions >= 1, f"{topic}: partitions must be >= 1"


# ── safety properties ─────────────────────────────────────────────────────


def test_reconciler_is_create_only(script):
    """No flag that could alter, delete or reassign a topic may appear."""
    for forbidden in ("--delete", "--alter", "--bootstrap-controller", "--replica-assignment"):
        assert forbidden not in script, f"reconcile.sh must not use {forbidden}"
    assert "--create" in script and "--if-not-exists" in script


def test_validation_pattern_matches_script(script):
    """The script's own grep pattern must be the one this test validates with.

    If they drift, this file would be asserting a shape the reconciler does
    not actually enforce.
    """
    assert "^[A-Za-z0-9][A-Za-z0-9._-]*:[1-9][0-9]*:[1-9][0-9]*$" in script


def test_malformed_input_is_fatal_not_skipped(script):
    """A bad line must abort, never be skipped.

    Skipping would mean a topic believed to be declared is never reconciled.
    """
    assert "FATAL" in script and "invalid entry" in script


def test_failures_propagate(script):
    """Unreachable broker and failed creates must both exit non-zero."""
    assert "set -euo pipefail" in script
    assert script.count("exit 1") >= 4, "each failure path needs a non-zero exit"


# ── workload stays unprivileged ───────────────────────────────────────────


def test_workload_is_unprivileged(cronjob):
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod, "reconciler needs no ServiceAccount"
    assert pod["securityContext"]["runAsNonRoot"] is True
    container = pod["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_no_rbac_resources(manifest_docs):
    """It makes no Kubernetes API calls, so it must request no permissions."""
    kinds = {d.get("kind") for d in manifest_docs}
    assert kinds == {"ConfigMap", "CronJob"}, f"unexpected resources: {kinds}"


def test_the_cronjob_mounts_the_generated_topics(cronjob, script):
    """The script must read desired state from the generated mount."""
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    vols = {v["name"]: v["configMap"]["name"] for v in pod["volumes"]}
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert vols["desired"] == "kafka-topic-reconcile-topics"
    assert vols["script"] == "kafka-topic-reconcile"
    assert mounts["desired"] == "/desired"
    assert mounts["script"] == "/config"
    assert "/desired/topics" in script, "script still points at the old path"
