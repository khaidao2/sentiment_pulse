import fastavro
import json

def load_avro_schema(path: str) -> dict:
    """Load Avro schema from a file."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        return fastavro.parse_schema(schema)
    except Exception as e:
        raise ValueError(f"Failed to load Avro schema from {path}: {e}")

def validate_record(data, schema):
    """Validate a record against an Avro schema."""
    try:
        fastavro.validate(data, schema)
        return True
    except Exception as e:
        raise ValueError(f"Record validation failed: {e}")
