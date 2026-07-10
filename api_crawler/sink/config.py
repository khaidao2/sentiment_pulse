"""Config for the generic Kafka -> MinIO sink.

One image, one entrypoint (`python -m api_crawler.sink`), driven entirely by the
environment — exactly like the crawler. Each topic gets its own Deployment that
only differs by these env vars, injected by the sent-gen-generated manifest from
the streaming contract YAML (the single source of truth). Literals are dev
fallbacks for running locally against Redpanda + a local MinIO.
"""

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ── which topic to archive ───────────────────────────────────────────────
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sentiment-pulse.itviec")
KAFKA_GROUP_ID = os.environ.get("KAFKA_GROUP_ID", f"{KAFKA_TOPIC}-minio-sink")

# Avro schema for this topic — the single source of truth for the Parquet column
# types the sink writes. Ships at a fixed path inside the image (same file the
# producer serializes with). Default is itviec's; a second topic overrides via
# the SCHEMA_PATH env (sent-gen should inject it from the contract then).
SCHEMA_PATH = Path(
    os.environ.get(
        "SCHEMA_PATH",
        _REPO_ROOT / "data-contracts" / "schemas" / "raw" / "itviec.avsc",
    )
)

# ── MinIO / S3 target ─────────────────────────────────────────────────────
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "minioadminpassword")
MINIO_RAW_BUCKET = os.environ.get("MINIO_RAW_BUCKET", "sentiment-pulse-raw")
# Object key prefix under the bucket; defaults to a slug of the topic.
SINK_PREFIX = os.environ.get("SINK_PREFIX", KAFKA_TOPIC.split(".")[-1])

# ── batching thresholds ───────────────────────────────────────────────────
BATCH_MAX_RECORDS = int(os.environ.get("BATCH_MAX_RECORDS", "500"))
BATCH_MAX_SECONDS = float(os.environ.get("BATCH_MAX_SECONDS", "60"))
