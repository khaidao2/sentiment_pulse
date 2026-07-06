"""Generate ClickHouse DDL (CREATE TABLE) from a parsed Avro schema.

Pure functions: schema dict in -> SQL string out. This module never reads the
schema file nor decides where to write — the caller (cli) loads the schema and
writes the result to `contract.sql_path`.
"""

from __future__ import annotations

# Avro primitive type -> ClickHouse type.
_PRIMITIVE = {
    "string": "String",
    "int": "Int32",
    "long": "Int64",
    "float": "Float32",
    "double": "Float64",
    "boolean": "Bool",
    "bytes": "String",
}

# Avro logicalType -> ClickHouse type. Takes precedence over the base type.
_LOGICAL = {
    "date": "Date",
    "timestamp-millis": "DateTime64(3)",
    "timestamp-micros": "DateTime64(6)",
}


def _base_type(avro_type) -> str:
    """Map a single (non-union) Avro type — str or dict — to ClickHouse."""
    if isinstance(avro_type, str):
        return _PRIMITIVE.get(avro_type, "String")
    if isinstance(avro_type, dict):
        logical = avro_type.get("logicalType")
        if logical in _LOGICAL:
            return _LOGICAL[logical]
        t = avro_type.get("type")
        if t == "array":
            return f"Array({_base_type(avro_type['items'])})"
        if t in ("enum", "fixed"):
            return "String"
        # Nested record/map or unknown -> store as String (JSON blob).
        return _PRIMITIVE.get(t, "String")
    return "String"


def clickhouse_type(avro_type) -> str:
    """Map an Avro field type (including ["null", X] unions) to ClickHouse.

    A union containing "null" makes the column Nullable, except for Array —
    ClickHouse does not allow Nullable(Array(...)).
    """
    if isinstance(avro_type, list):
        nullable = "null" in avro_type
        non_null = [t for t in avro_type if t != "null"]
        inner = _base_type(non_null[0]) if non_null else "String"
        if nullable and not inner.startswith("Array("):
            return f"Nullable({inner})"
        return inner
    return _base_type(avro_type)


def generate_ddl(schema: dict, table: str) -> str:
    """Render a ClickHouse CREATE TABLE from a parsed Avro schema.

    Args:
        schema: Parsed Avro record schema (dict with a "fields" list).
        table:  Target table name — comes from the contract, not the schema.
    """
    columns = [
        f"    `{field['name']}` {clickhouse_type(field['type'])}"
        for field in schema["fields"]
    ]
    has_id = any(field["name"] == "id" for field in schema["fields"])
    order_by = "id" if has_id else "tuple()"
    body = ",\n".join(columns)
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        f"{body}\n"
        f") ENGINE = ReplacingMergeTree()\n"
        f"ORDER BY {order_by};\n"
    )
