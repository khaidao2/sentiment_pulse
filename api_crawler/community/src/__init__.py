"""Community Crawler Module."""

from __future__ import annotations

try:
    from .config import CrawlerConfig
    from .crawler import CommunityCrawler
    from .mapper import to_record
except ImportError:
    from config import CrawlerConfig
    from crawler import CommunityCrawler
    from mapper import to_record

__all__ = ["CrawlerConfig", "CommunityCrawler", "to_record"]
