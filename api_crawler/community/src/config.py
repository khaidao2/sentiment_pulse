"""Crawler configuration for the community source."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class CrawlerConfig:
    """Runtime configuration options for CommunityCrawler."""

    # API endpoint to fetch latest community posts from chungsy.vn
    api_url: str = os.environ.get("COMMUNITY_API_URL", "https://api.chungsy.vn/news-feed/latest")
    # Sleep time (seconds) between crawling pages or polling if needed
    request_delay: float = float(os.environ.get("COMMUNITY_REQUEST_DELAY", "1.0"))
    # Kafka bootstrap servers
    bootstrap_servers: str | None = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or None
