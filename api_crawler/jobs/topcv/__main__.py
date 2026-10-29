"""Composition root for the topcv crawler.

Run with:  python -m api_crawler.jobs.topcv

The ONLY place that knows the concrete adapters: it builds them, injects them
into the generic CrawlService, and runs.
"""

from __future__ import annotations

import logging

from api_crawler.jobs.topcv import config
from api_crawler.jobs.topcv.parser import TopcvParser
from api_crawler.jobs.topcv.source import TopcvSource
from api_crawler.shared.adapters.kafka_publisher import AvroKafkaProducer
from api_crawler.shared.config import KAFKA_BOOTSTRAP_SERVERS, SCHEMA_REGISTRY_URL
from api_crawler.shared.pipeline.crawl_service import CrawlService


def _key(record: dict) -> str:
    """Kafka message key — dedup key for the record."""
    return record.get("job_id") or record["title"]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    source = TopcvSource(
        listing_url=config.LISTING_URL,
        max_pages=config.MAX_PAGES,
        delay=config.DELAY,
        max_retries=config.MAX_RETRIES,
    )
    parser = TopcvParser(vnd_per_usd=config.VND_PER_USD)
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
