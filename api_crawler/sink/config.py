"""Config for the generic Kafka -> MinIO sink.

One image, one entrypoint (`python -m api_crawler.sink`), driven entirely by the
environment — exactly like the crawler. Each topic gets its own Deployment that
only differs by these env vars, injected by the sent-gen-generated manifest from
the streaming contract YAML (the single source of truth). Literals are dev
fallbacks for running locally against Redpanda + a local MinIO.
"""

import os

# ── which topic to archive ───────────────────────────────────────────────
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "sentiment-pulse.itviec")
KAFKA_GROUP_ID = os.environ.get("KAFKA_GROUP_ID", f"{KAFKA_TOPIC}-minio-sink")

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
