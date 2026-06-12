"""CLI entrypoint: wire the generated producer and run the news crawl.

Usage::

    python -m api_crawler.news.src           # crawl news
    python api_crawler/news/src/__main__.py  # same, run as a script
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys

# Make sibling modules importable when run script-style.
sys.path.insert(0, os.path.dirname(__file__))

try:  # works both as a package (-m) and as flat scripts
    from .config import CrawlerConfig
    from .crawler import NewsCrawler
except ImportError:  # pragma: no cover
    from config import CrawlerConfig
    from crawler import NewsCrawler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("news_crawler.main")

_PRODUCER_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "producer.py")


def _load_producer():
    """Import NewsProducer from the generated sibling producer.py."""
    if not os.path.exists(_PRODUCER_PATH):
        raise SystemExit(
            "producer.py not found at "
            f"{os.path.abspath(_PRODUCER_PATH)}.\n"
            "Generate it first with:  sent-gen render"
        )
    spec = importlib.util.spec_from_file_location("news_producer", _PRODUCER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.NewsProducer


def main() -> int:
    config = CrawlerConfig()
    producer_cls = _load_producer()
    producer = producer_cls(bootstrap_servers=config.bootstrap_servers)
    crawler = NewsCrawler(producer=producer, config=config)
    total = crawler.crawl()
    logger.info("Done. %d records produced to Kafka.", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
