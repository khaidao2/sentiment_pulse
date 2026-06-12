"""chungsy.vn Community API crawler.

Fetches latest social posts from the chungsy.vn API, maps them to the
CommunityRecord schema, and publishes them to Kafka.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional, Protocol

from curl_cffi import requests

try:  # works both as a package (-m) and as flat scripts
    from .config import CrawlerConfig
    from .mapper import to_record
except ImportError:  # pragma: no cover
    from config import CrawlerConfig
    from mapper import to_record

logger = logging.getLogger("community_crawler")


class Producer(Protocol):
    """Minimal contract the crawler needs from a producer."""

    def send(self, record: dict) -> Any: ...


class CommunityCrawler:
    def __init__(
        self,
        producer: Producer,
        config: Optional[CrawlerConfig] = None,
    ):
        self.producer = producer
        self.config = config or CrawlerConfig()

    def fetch_posts(self) -> List[dict]:
        """Fetch latest posts from chungsy.vn API."""
        logger.info("Fetching community posts from %s", self.config.api_url)
        try:
            response = requests.get(self.config.api_url, impersonate="chrome110", timeout=15)
            if response.status_code != 200:
                logger.error("Failed to fetch community posts: HTTP %d", response.status_code)
                return []

            posts = response.json()
            if not isinstance(posts, list):
                logger.error("Unexpected response format: expected a list, got %s", type(posts))
                return []

            logger.info("Successfully fetched %d posts from chungsy.vn", len(posts))
            return posts
        except Exception as e:
            logger.error("Error fetching or parsing community posts: %s", e)
            return []

    def crawl(self) -> int:
        """Fetch posts, map them, and produce to Kafka."""
        posts = self.fetch_posts()
        if not posts:
            logger.info("No posts found to crawl.")
            return 0

        sent = 0
        for post in posts:
            # Skip empty content posts
            if not post.get("textContent"):
                continue

            record = to_record(post)
            try:
                self.producer.send(record)
                sent += 1
            except Exception as e:
                logger.error("Failed to produce community record: %s", e)

            # Politeness delay
            if self.config.request_delay > 0:
                time.sleep(self.config.request_delay)

        logger.info("Community crawl finished: produced %d/ %d records to Kafka.", sent, len(posts))
        return sent
