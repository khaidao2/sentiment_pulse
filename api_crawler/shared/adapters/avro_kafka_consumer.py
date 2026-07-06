"""Avro Kafka consumer — the inverse of AvroKafkaProducer.

Reads the Confluent wire format the producer writes:
    [0x00] [4-byte schema ID big-endian] [fastavro schemaless bytes]

The writer schema is fetched from the registry by ID (GET /schemas/ids/{id})
and cached, so a message can be decoded without knowing the schema up front.
Offsets are committed manually by the pipeline after a successful sink flush
(at-least-once) — auto-commit is disabled.
"""

from __future__ import annotations

import io
import json
import logging
import struct
from typing import Any

import fastavro
import requests
from confluent_kafka import Consumer, KafkaError

from api_crawler.shared.ports.subscriber import ISubscriber

_MAGIC_BYTE = 0
_LOG = logging.getLogger(__name__)


class AvroKafkaConsumer(ISubscriber):
    def __init__(
        self,
        topic: str,
        group_id: str,
        bootstrap_servers: str,
        registry_url: str,
    ) -> None:
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        self._consumer.subscribe([topic])
        self._registry_url = registry_url.rstrip("/")
        self._schema_cache: dict[int, Any] = {}
        _LOG.info("AvroKafkaConsumer ready | topic=%s | group=%s", topic, group_id)

    def poll(self, timeout: float = 1.0) -> dict | None:
        msg = self._consumer.poll(timeout)
        if msg is None:
            return None
        if msg.error():
            # End-of-partition is informational, not a failure.
            if msg.error().code() == KafkaError._PARTITION_EOF:
                return None
            raise RuntimeError(f"kafka consume error: {msg.error()}")
        return self._decode(msg.value())

    def _decode(self, data: bytes) -> dict:
        magic, schema_id = struct.unpack(">bI", data[:5])
        if magic != _MAGIC_BYTE:
            raise ValueError(f"unexpected magic byte {magic}, not a Confluent Avro message")
        schema = self._get_schema(schema_id)
        return fastavro.schemaless_reader(io.BytesIO(data[5:]), schema)

    def _get_schema(self, schema_id: int) -> Any:
        if schema_id not in self._schema_cache:
            resp = requests.get(f"{self._registry_url}/schemas/ids/{schema_id}", timeout=15)
            resp.raise_for_status()
            raw = json.loads(resp.json()["schema"])
            self._schema_cache[schema_id] = fastavro.parse_schema(raw)
            _LOG.info("Fetched writer schema | id=%d", schema_id)
        return self._schema_cache[schema_id]

    def commit(self) -> None:
        self._consumer.commit(asynchronous=False)

    def close(self) -> None:
        self._consumer.close()
