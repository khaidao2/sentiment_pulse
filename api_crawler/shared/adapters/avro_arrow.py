"""Derive a stable pyarrow schema from an Avro record schema.

The sink writes Parquet. Without an explicit schema, pyarrow infers types from
the record dicts — and a batch where a column is ENTIRELY null (e.g.
salary_min_usd, always hidden for anonymous crawls) infers the Arrow `null`
type. ClickHouse's s3() reader can't map `null`, so the bronze read fails.

Deriving the Arrow schema from the Avro schema (the single source of truth,
same file the producer serializes with) fixes this at the source: every column
gets a fixed, self-describing type regardless of what a given batch contains.

The mapping mirrors sentpul/ddl.py's Avro->ClickHouse mapping so bronze Parquet
and the ClickHouse DDL agree on types.
"""

from __future__ import annotations

import pyarrow as pa

_PRIMITIVE = {
    "string": pa.string(),
    "int": pa.int32(),
    "long": pa.int64(),
    "float": pa.float32(),
    "double": pa.float64(),
    "boolean": pa.bool_(),
    "bytes": pa.binary(),
}


def _arrow_type(avro_type) -> pa.DataType:
    """Map a single (non-union) Avro type — str or dict — to an Arrow type."""
    if isinstance(avro_type, str):
        return _PRIMITIVE.get(avro_type, pa.string())
    if isinstance(avro_type, dict):
        t = avro_type.get("type")
        if t == "array":
            return pa.list_(_arrow_type(avro_type["items"]))
        if t in ("enum", "fixed"):
            return pa.string()
        # nested record/map or unknown -> JSON blob as string (parse in dbt silver)
        return _PRIMITIVE.get(t, pa.string())
    return pa.string()


def avro_to_arrow_schema(avro_schema: dict) -> pa.Schema:
    """Build a pyarrow schema from a parsed Avro record schema.

    A union containing "null" makes the field nullable; its non-null branch
    decides the type. Everything else is a required field.
    """
    fields: list[pa.Field] = []
    for f in avro_schema["fields"]:
        ftype = f["type"]
        nullable = False
        if isinstance(ftype, list):  # union, e.g. ["null", "int"]
            nullable = "null" in ftype
            non_null = [t for t in ftype if t != "null"]
            ftype = non_null[0] if non_null else "string"
        fields.append(pa.field(f["name"], _arrow_type(ftype), nullable=nullable))
    return pa.schema(fields)
