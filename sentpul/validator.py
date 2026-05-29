import json
import fastavro

def load_avro_schema(schema_path: str) -> dict:
    """Reads and parses an Avro schema file (.avsc)."""
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)

def validate_record(data: dict, schema: dict) -> bool:
    """Validates a data record against a parsed Avro schema."""
    try:
        fastavro.validate(data, schema)
        return True
    except Exception as e:
        raise ValueError(f"Avro validation failed: {e}")
