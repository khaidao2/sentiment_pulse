"""Streaming NLP worker.

Consumes raw articles/posts from ``sentiment-pulse.news`` and
``sentiment-pulse.community``, runs them through the sentiment classifier and
ticker extractor, and produces one ``SentimentScoreRecord`` per mentioned
ticker to ``sentiment-pulse.sentiment-scores``.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import struct
import urllib.error
import urllib.request
from datetime import datetime, timezone

import fastavro
from kafka import KafkaConsumer

from api_crawler.sentiment_scores.producer import SentimentScoresProducer
from nlp_pipeline.classifier import SentimentClassifier, TickerExtractor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nlp_pipeline.worker")

# Apicurio wire format: 1 magic byte (0x0) + 4-byte big-endian schema globalId + Avro payload.
APICURIO_MAGIC_BYTE = 0
APICURIO_HEADER_FORMAT = ">bI"
APICURIO_HEADER_SIZE = struct.calcsize(APICURIO_HEADER_FORMAT)

SOURCE_TOPICS = ["sentiment-pulse.news", "sentiment-pulse.community"]


def get_apicurio_url() -> str:
    return os.environ.get("APICURIO_REGISTRY_URL", "http://apicurio.kafka.svc.cluster.local:80").rstrip("/")


def fetch_schema_by_global_id(global_id: int) -> dict:
    """Fetches the exact Avro schema content registered under the given Apicurio globalId."""
    url = f"{get_apicurio_url()}/apis/registry/v2/ids/globalIds/{global_id}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Apicurio returned {error.code} for globalId={global_id}: {error.read().decode('utf-8')}")


def _parse_event_time(value: str | None) -> datetime:
    """Best-effort parse of the source record's ``published_at`` field."""
    if value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning(f"Could not parse published_at='{value}'; falling back to now()")
    return datetime.now(timezone.utc)


class NlpWorker:
    """Kafka consumer/producer loop tying the classifier to the sentiment-scores topic."""

    def __init__(self, bootstrap_servers: str | None = None):
        if bootstrap_servers is None:
            bootstrap_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka.kafka.svc.cluster.local:9092")

        self.consumer = KafkaConsumer(
            *SOURCE_TOPICS,
            bootstrap_servers=bootstrap_servers,
            group_id="nlp_pipeline_worker_group",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        )
        self.producer = SentimentScoresProducer(bootstrap_servers=bootstrap_servers)
        self.classifier = SentimentClassifier()
        self.ticker_extractor = TickerExtractor()
        self._schema_cache: dict[int, dict] = {}

    def resolve_schema(self, schema_id: int) -> dict:
        schema = self._schema_cache.get(schema_id)
        if schema is None:
            schema = fetch_schema_by_global_id(schema_id)
            self._schema_cache[schema_id] = schema
            logger.info(f"Fetched and cached schema globalId={schema_id} from Apicurio Registry")
        return schema

    def deserialize(self, message_bytes: bytes) -> dict:
        magic_byte, schema_id = struct.unpack(APICURIO_HEADER_FORMAT, message_bytes[:APICURIO_HEADER_SIZE])
        if magic_byte != APICURIO_MAGIC_BYTE:
            raise ValueError(f"Unexpected message format (magic byte={magic_byte}); expected Apicurio wire format")
        schema = self.resolve_schema(schema_id)
        bytes_reader = io.BytesIO(message_bytes[APICURIO_HEADER_SIZE:])
        return fastavro.schemaless_reader(bytes_reader, schema)

    def build_score_records(self, source_topic: str, record: dict) -> list[dict]:
        """Classifies one news/community record and returns one SentimentScoreRecord per mentioned ticker."""
        title = record.get("title", "")
        content = record.get("content", "")
        text = f"{title}. {content}".strip()

        tickers = self.ticker_extractor.extract(text)
        if not tickers:
            return []

        result = self.classifier.classify(text)
        event_time = _parse_event_time(record.get("published_at"))
        now = datetime.now(timezone.utc)
        article_id = record.get("id") or hashlib.md5(text.encode("utf-8")).hexdigest()
        source = record.get("source") or source_topic

        score_records = []
        for ticker in tickers:
            score_records.append(
                {
                    "id": f"{article_id}:{ticker}",
                    "event_time": event_time,
                    "source": source,
                    "ticker": ticker,
                    "score": result["score"],
                    "label": result["label"],
                    "intensity": result["intensity"],
                    "headline": title,
                    "url": record.get("url"),
                    "created_at": now,
                }
            )
        return score_records

    def start_consuming(self):
        logger.info(f"Starting NLP worker consumer loop for topics {SOURCE_TOPICS}")
        try:
            while True:
                message_pack = self.consumer.poll(timeout_ms=1000)
                for tp, messages in message_pack.items():
                    for message in messages:
                        try:
                            record = self.deserialize(message.value)
                            for score_record in self.build_score_records(tp.topic, record):
                                self.producer.send(score_record)
                        except Exception as e:
                            logger.error(f"Error processing message at offset {message.offset}: {e}")
                if message_pack:
                    self.consumer.commit()
        except KeyboardInterrupt:
            logger.info("Consuming stopped by user.")
        finally:
            self.consumer.close()


if __name__ == "__main__":
    worker = NlpWorker()
    worker.start_consuming()
