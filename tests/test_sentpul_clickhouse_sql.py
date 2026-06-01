"""Tests for sentpul's Avro -> ClickHouse DDL generator."""

import pytest

# Importing sentpul.cli pulls in jinja2/fastavro/requests; skip if absent locally.
cli = pytest.importorskip("sentpul.cli")


def test_generate_clickhouse_sql_maps_types_and_nullability():
    schema = {
        "type": "record",
        "name": "R",
        "fields": [
            {"name": "id", "type": "string"},
            {"name": "volume", "type": "long"},
            {"name": "price", "type": "float"},
            {"name": "ratio", "type": "double"},
            {"name": "note", "type": ["null", "string"]},
        ],
    }

    sql = cli.generate_clickhouse_sql(schema, "vn_stock")

    assert "CREATE TABLE IF NOT EXISTS vn_stock" in sql
    assert "`id` String" in sql
    assert "`volume` Int64" in sql
    assert "`price` Float32" in sql
    assert "`ratio` Float64" in sql
    assert "`note` Nullable(String)" in sql
    assert "ENGINE = ReplacingMergeTree()" in sql
    assert "ORDER BY id" in sql          # id present -> order by it


def test_generate_clickhouse_sql_without_id_orders_by_tuple():
    schema = {
        "type": "record",
        "name": "R",
        "fields": [{"name": "name", "type": "string"}],
    }

    sql = cli.generate_clickhouse_sql(schema, "t")

    assert "ORDER BY tuple()" in sql
