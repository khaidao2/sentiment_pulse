"""News Crawler Module."""

from __future__ import annotations

try:
    from .config import CrawlerConfig
    from .crawler import NewsCrawler
    from .mapper import to_record
except ImportError:
    from config import CrawlerConfig
    from crawler import NewsCrawler
    from mapper import to_record

__all__ = ["CrawlerConfig", "NewsCrawler", "to_record"]
