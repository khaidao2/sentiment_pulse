"""Composition root for the courses crawler.

Run with:  python -m api_crawler.jobs.courses

The ONLY place that knows the concrete adapters: it builds them, injects them
into the generic CrawlService, and runs.
"""

from __future__ import annotations

import logging

from api_crawler.jobs.courses import config
from api_crawler.jobs.courses.parser import CoursesParser
from api_crawler.jobs.courses.source import CoursesSource
from api_crawler.shared.adapters.kafka_publisher import AvroKafkaProducer
from api_crawler.shared.config import KAFKA_BOOTSTRAP_SERVERS, SCHEMA_REGISTRY_URL
from api_crawler.shared.pipeline.crawl_service import CrawlService


def _key(record: dict) -> str:
    """Kafka message key — the course's identity, so re-crawls land on the
    same partition and compact onto the same row."""
    return record["id"]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    source = CoursesSource(
        sources=config.SOURCES,
        delay=config.DELAY,
        max_pages=config.MAX_PAGES,
    )
    parser = CoursesParser()
    publisher = AvroKafkaProducer(
        topic=config.KAFKA_TOPIC,
        schema_path=config.SCHEMA_PATH,
        subject=config.SCHEMA_SUBJECT,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        registry_url=SCHEMA_REGISTRY_URL,
    )
    service = CrawlService(source, parser, publisher, key_fn=_key)
    service.run()


if __name__ == "__main__":
    main()
