"""The ports-and-adapters layering, enforced.

`api_crawler` is built so the domain can be tested without a broker, a bucket
or a network: the ports are abstract, the pipeline services talk only to ports,
the parsers only turn bytes into records, and every concrete dependency
(confluent_kafka, minio, fastavro, pyarrow, requests) lives in an adapter that
a `__main__.py` wires up. That property is invisible in a diff — it is lost the
first time someone reaches for a Kafka handle inside a parser "just to log
something", and nothing fails until the test suite needs a broker to run.

So the rule is checked here rather than described in a README. These are
import-graph assertions over the AST; nothing is imported or executed, so a
violation is reported as a layering failure and not as a collection error.

Adding a legitimate dependency means adding it to the right set below, with a
reason. That edit is the point: it makes the choice visible in review.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
PKG_NAME = PKG.name  # "api_crawler"

# Concrete infrastructure. Allowed only in adapters and composition roots.
INFRA = {
    "confluent_kafka",  # Kafka producer/consumer
    "minio",            # object store
    "fastavro",         # Avro encode/decode
    "pyarrow",          # Parquet buffers
    "requests",         # HTTP, incl. the schema registry client
    "psycopg",          # Postgres
}

# HTML parsing. A parser's whole job, and nothing else's business.
HTML = {"bs4"}


def _module_imports(path: Path) -> set[str]:
    """Top-level module names imported by `path`, at any nesting depth."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports resolve inside the package; record them as such.
            names.add(node.module if node.level == 0 and node.module else PKG_NAME)
    return names


def _third_party(names: set[str]) -> set[str]:
    return {
        n for n in names
        if n.split(".")[0] not in sys.stdlib_module_names
        and n.split(".")[0] != PKG_NAME
        and n != "__future__"
    }


def _internal(names: set[str]) -> set[str]:
    return {n for n in names if n.split(".")[0] == PKG_NAME}


def _files(*rel: str) -> list[Path]:
    out: list[Path] = []
    for pattern in rel:
        out.extend(sorted(p for p in PKG.glob(pattern) if p.name != "__init__.py"))
    assert out, f"no files matched {rel!r} — did the layout move?"
    return out


def test_ports_depend_on_nothing():
    """A port is an ABC. It may not import the package or any third party.

    This is what lets a test build a fake source or sink in three lines.
    """
    for path in _files("shared/ports/*.py"):
        names = _module_imports(path)
        assert not _third_party(names), (
            f"{path.relative_to(PKG)} imports third-party {sorted(_third_party(names))}; "
            "a port must be abstract and dependency-free"
        )
        assert not _internal(names), (
            f"{path.relative_to(PKG)} imports {sorted(_internal(names))}; "
            "a port may not depend on anything in the package"
        )


def test_pipeline_talks_only_to_ports():
    """CrawlService/SinkService hold ports, never a concrete adapter.

    Breaking this is what would make the pipeline tests need a live broker.
    """
    for path in _files("shared/pipeline/*.py"):
        names = _module_imports(path)
        rel = path.relative_to(PKG)

        forbidden = _third_party(names) & (INFRA | HTML)
        assert not forbidden, (
            f"{rel} imports infrastructure {sorted(forbidden)}; "
            "the pipeline must receive it through a port instead"
        )

        for mod in _internal(names):
            assert ".ports." in mod or mod.endswith(".ports"), (
                f"{rel} imports {mod}; a pipeline service may only import "
                f"{PKG_NAME}.shared.ports.*"
            )


def test_parsers_only_parse():
    """A parser turns bytes into records. It owns no I/O.

    bs4 is its tool; a broker, a bucket or an HTTP session is not.
    """
    for path in _files("jobs/*/parser.py"):
        names = _module_imports(path)
        rel = path.relative_to(PKG)

        forbidden = _third_party(names) & INFRA
        assert not forbidden, (
            f"{rel} imports infrastructure {sorted(forbidden)}; a parser must be pure"
        )

        extra = _third_party(names) - INFRA - HTML
        assert not extra, (
            f"{rel} imports unexpected third-party {sorted(extra)}; "
            f"a parser is expected to need only {sorted(HTML)}"
        )

        for mod in _internal(names):
            assert ".ports." in mod or mod.endswith(".ports"), (
                f"{rel} imports {mod}; a parser may only import the parser port"
            )


def test_adapters_do_not_import_the_domain():
    """Dependencies point inward: an adapter implements a port.

    It must not know which job or pipeline uses it, or the dependency arrow
    reverses and the package stops being substitutable.
    """
    for path in _files("shared/adapters/*.py"):
        names = _module_imports(path)
        rel = path.relative_to(PKG)
        for mod in _internal(names):
            assert ".ports." in mod or mod.endswith(".ports"), (
                f"{rel} imports {mod}; an adapter may only depend on a port, "
                "never on jobs/ or pipeline/"
            )


def test_infrastructure_is_confined_to_adapters_and_entrypoints():
    """Nothing outside shared/adapters/ and a `__main__.py` may touch INFRA.

    The backstop for the three rules above: it catches a new module that none
    of the globs happen to cover.
    """
    allowed_prefixes = ("shared/adapters/",)
    offenders: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if rel.startswith("tests/") or path.name == "__main__.py":
            continue  # tests fake it; a composition root is where wiring belongs
        if rel.startswith(allowed_prefixes):
            continue
        used = _third_party(_module_imports(path)) & INFRA
        if used:
            offenders.append(f"{rel} -> {sorted(used)}")
    assert not offenders, (
        "infrastructure imported outside shared/adapters/ and __main__.py:\n  "
        + "\n  ".join(offenders)
        + "\nMove the dependency behind a port, or wire it in a __main__.py."
    )
