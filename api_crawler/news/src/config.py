"""Crawler configuration for the news source."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict

# Default feeds corresponding to P1 & P2 categories of VnExpress
DEFAULT_FEEDS = {
    # P1 (High priority)
    "thoi-su": "https://vnexpress.net/rss/thoi-su.rss",
    "so-hoa": "https://vnexpress.net/rss/so-hoa.rss",  # Khoa học công nghệ
    "tin-moi-nhat": "https://vnexpress.net/rss/tin-moi-nhat.rss",
    # P2 (Medium priority)
    "the-gioi": "https://vnexpress.net/rss/the-gioi.rss",
    "y-kien": "https://vnexpress.net/rss/y-kien.rss"
}


def _feeds_from_env() -> Dict[str, str]:
    """Parse custom feeds if defined in the env, else return DEFAULT_FEEDS."""
    # Feeds can be provided in env as: NEWS_FEEDS="thoi-su=url,so-hoa=url"
    raw = os.environ.get("NEWS_FEEDS", "")
    if not raw:
        return dict(DEFAULT_FEEDS)
    
    feeds = {}
    for item in raw.split(","):
        if "=" in item:
            name, url = item.split("=", 1)
            name = name.strip()
            url = url.strip()
            if name and url:
                feeds[name] = url
    return feeds or dict(DEFAULT_FEEDS)


@dataclass
class CrawlerConfig:
    """Runtime configuration options for NewsCrawler."""

    feeds: Dict[str, str] = field(default_factory=_feeds_from_env)
    # Maximum number of articles to crawl per feed per run (default 20)
    max_articles_per_feed: int = int(os.environ.get("NEWS_MAX_ARTICLES_PER_FEED", "20"))
    # Sleep time (seconds) between scraping full HTML articles to avoid rate-limiting
    request_delay: float = float(os.environ.get("NEWS_REQUEST_DELAY", "1.0"))
    # Kafka bootstrap servers
    bootstrap_servers: str | None = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or None
