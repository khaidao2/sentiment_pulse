"""The courses contract, its Avro schema and the dbt bronze view must agree.

bronze_courses DECLARES its column structure instead of inferring it, because
`dbt build` takes no selector and schema inference over a glob matching no files
fails the whole medallion build until the first crawl lands. A declared
structure is a second copy of the schema, so this is the test that keeps the
copy honest: the Avro schema stays the single source of truth, and adding a
field there without adding it to the view fails here rather than in a nightly
dbt run.
"""

import json
import re
from pathlib import Path

import pytest

from sentpul.contract import load_contract
from sentpul.ddl import clickhouse_type

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACTS = _ROOT / "data-contracts" / "contracts"
_AVSC = _ROOT / "data-contracts" / "schemas" / "learning" / "courses.avsc"
_BRONZE = _ROOT / "dbt" / "models" / "bronze" / "bronze_courses.sql"
_SILVER = _ROOT / "dbt" / "models" / "silver" / "silver_courses.sql"


@pytest.fixture(scope="module")
def avro_columns() -> list[tuple[str, str]]:
    schema = json.loads(_AVSC.read_text(encoding="utf-8"))
    return [(f["name"], clickhouse_type(f["type"])) for f in schema["fields"]]


@pytest.fixture(scope="module")
def declared_columns() -> list[tuple[str, str]]:
    """Parse the structure string out of the bronze view's s3() call."""
    sql = _BRONZE.read_text(encoding="utf-8")
    block = re.search(r"'(id String,.*?)'", sql, re.S).group(1)
    return [tuple(part.split(None, 1)) for part in
            (p.strip() for p in block.split(","))]


def test_bronze_structure_matches_the_avro_schema(avro_columns, declared_columns):
    assert declared_columns == avro_columns


def test_the_crawler_and_the_sink_share_one_schema_file():
    producer = load_contract(_CONTRACTS / "producer" / "courses.yaml")
    sink = load_contract(_CONTRACTS / "streaming" / "courses_sink.yaml")
    # Different Avro files would mean the sink writes Parquet shaped unlike what
    # the producer serialized, and every batch would fail.
    assert producer.schema_file == sink.schema_file
    assert producer.kafka["topic"] == sink.kafka["topic"]


def test_the_sink_overrides_the_schema_path():
    """The sink image defaults SCHEMA_PATH to itviec's, so a second topic must
    set it or the Parquet gets the job schema's columns."""
    sink = load_contract(_CONTRACTS / "streaming" / "courses_sink.yaml")
    assert sink.workload["env"]["schema_path"].endswith("learning/courses.avsc")


def test_the_crawl_lands_before_the_nightly_dbt_build():
    """dbt build runs 03:00 daily and has no selector, so it builds the courses
    models too. Crawling at 03:00 would race it and slip a week."""
    producer = load_contract(_CONTRACTS / "producer" / "courses.yaml")
    minute, hour, _, _, dow = producer.airflow["schedule"].split()
    assert int(hour) < 3, "the crawl must finish before the 03:00 dbt build"
    assert dow == "0", "weekly, on Sunday"


def test_silver_dedups_on_the_natural_key():
    sql = _SILVER.read_text(encoding="utf-8")
    assert "limit 1 by id" in sql.lower()
    assert 'ref("bronze_courses")' in sql
