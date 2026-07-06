"""Avro Kafka producer with Apicurio Schema Registry (Confluent-compat wire format).

Wire format per message:
    [0x00] [4-byte schema ID big-endian] [fastavro schemaless bytes]

This is identical to the Confluent Schema Registry wire format, which Apicurio
exposes at /apis/ccompat/v6.
"""

from __future__ import annotations

import io
import json
import logging
import struct
from pathlib import Path
from typing import Any

import fastavro
import requests
from confluent_kafka import Producer

from api_crawler.shared.ports.publisher import IPublisher

_MAGIC_BYTE = 0
_LOG = logging.getLogger(__name__)


class AvroKafkaProducer(IPublisher):
    """
    Produces Avro-encoded messages to a Kafka topic.

    Registers the schema with Apicurio on first use (idempotent) and
    caches the schema ID for the lifetime of the producer.

    Args:
        topic:              Kafka topic name (e.g. "raw.realestate.batdongsan").
        schema_path:        Path to the .avsc schema file.
        subject:            Schema Registry subject name. Convention: "<topic>-value".
        bootstrap_servers:  Kafka/Redpanda broker address.
        registry_url:       Schema Registry base URL (Apicurio /apis/ccompat/v6).

    Infra endpoints are injected (not read from a global config module) so the
    adapter stays a pure port implementation — the composition root wires them.
    """

    def __init__(
        self,
        topic: str,
        schema_path: Path,
        subject: str,
        bootstrap_servers: str,
        registry_url: str,
    ) -> None:
        self._topic = topic
        self._registry_url = registry_url.rstrip("/")
        with open(schema_path) as f:
            schema_dict = json.load(f)
        self._parsed_schema = fastavro.parse_schema(schema_dict)
        self._schema_str = json.dumps(schema_dict)
        self._schema_id = self._register_schema(subject)
        self._producer = Producer({"bootstrap.servers": bootstrap_servers})
        _LOG.info("AvroKafkaProducer ready | topic=%s | schema_id=%d", topic, self._schema_id)

    def _register_schema(self, subject: str) -> int:
        """Register schema with Apicurio and return the schema ID."""
        url = f"{self._registry_url}/subjects/{subject}/versions"
        payload = {"schema": self._schema_str, "schemaType": "AVRO"}
        headers = {"Content-Type": "application/vnd.schemaregistry.v1+json"}

        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            schema_id: int = resp.json()["id"]
            _LOG.info("Schema registered | subject=%s | id=%d", subject, schema_id)
            return schema_id

        # Schema may already be registered — look it up
        resp = requests.post(
            f"{self._registry_url}/subjects/{subject}",
            json=payload,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        schema_id = resp.json()["id"]
        _LOG.info("Schema already registered | subject=%s | id=%d", subject, schema_id)
        return schema_id

    def produce(self, key: str, record: dict[str, Any]) -> None:
        """Serialize record as Avro with Confluent wire format and produce to Kafka."""
        buf = io.BytesIO()
        buf.write(struct.pack(">bI", _MAGIC_BYTE, self._schema_id))
        fastavro.schemaless_writer(buf, self._parsed_schema, record)

        self._producer.produce(
            topic=self._topic,
            key=key.encode("utf-8"),
            value=buf.getvalue(),
            on_delivery=_delivery_report,
        )
        # Poll to trigger delivery callbacks and prevent queue overflow
        self._producer.poll(0)

    def flush(self, timeout: float = 30.0) -> None:
        remaining = self._producer.flush(timeout)
        if remaining > 0:
            _LOG.warning("%d message(s) not delivered after flush", remaining)


def _delivery_report(err: Any, msg: Any) -> None:
    if err:
        _LOG.error("Kafka delivery failed | topic=%s | err=%s", msg.topic(), err)
    else:
        _LOG.debug("Delivered | topic=%s | partition=%d | offset=%d", msg.topic(), msg.partition(), msg.offset())
