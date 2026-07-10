"""Composition root for the generic Kafka -> MinIO Parquet sink.

Run with:  python -m api_crawler.sink

The ONLY place that knows the concrete adapters: it wires an AvroKafkaConsumer to
a MinioSink through the generic SinkService and runs until the pod is stopped.
"""

from __future__ import annotations

import json
import logging

from api_crawler.shared.adapters.avro_arrow import avro_to_arrow_schema
from api_crawler.shared.adapters.avro_kafka_consumer import AvroKafkaConsumer
from api_crawler.shared.adapters.minio_sink import MinioSink
from api_crawler.shared.config import KAFKA_BOOTSTRAP_SERVERS, SCHEMA_REGISTRY_URL
from api_crawler.shared.pipeline.sink_service import SinkService
from api_crawler.sink import config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Fixed Parquet schema derived from the Avro contract, so all-null columns
    # keep their real type instead of Arrow's `null` (which ClickHouse rejects).
    with open(config.SCHEMA_PATH) as f:
        arrow_schema = avro_to_arrow_schema(json.load(f))

    subscriber = AvroKafkaConsumer(
        topic=config.KAFKA_TOPIC,
        group_id=config.KAFKA_GROUP_ID,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        registry_url=SCHEMA_REGISTRY_URL,
    )
    sink = MinioSink(
        endpoint=config.MINIO_ENDPOINT,
        access_key=config.MINIO_ACCESS_KEY,
        secret_key=config.MINIO_SECRET_KEY,
        bucket=config.MINIO_RAW_BUCKET,
        prefix=config.SINK_PREFIX,
        max_records=config.BATCH_MAX_RECORDS,
        max_seconds=config.BATCH_MAX_SECONDS,
        schema=arrow_schema,
    )
    SinkService(subscriber, sink).run()


if __name__ == "__main__":
    main()
