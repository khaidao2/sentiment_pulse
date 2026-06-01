"""Crawler configuration for the vn_stock source."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

# Default basket of tickers to crawl when none are provided explicitly.
DEFAULT_TICKERS = ["FPT", "VCB", "HPG", "VNM", "VIC"]


def _tickers_from_env() -> List[str]:
    raw = os.environ.get("VN_STOCK_TICKERS", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    return tickers or list(DEFAULT_TICKERS)


@dataclass
class CrawlerConfig:
    """Runtime knobs for :class:`~src.crawler.VnStockCrawler`.

    Values fall back to environment variables so the same code runs unchanged
    from the Airflow DAG, a cron job or a local shell.
    """

    tickers: List[str] = field(default_factory=_tickers_from_env)
    # Candle interval accepted by vnstock: 1m, 5m, 15m, 30m, 1h, 1D, 1W.
    interval: str = os.environ.get("VN_STOCK_INTERVAL", "1D")
    # How many days back to fetch when start/end are not supplied.
    lookback_days: int = int(os.environ.get("VN_STOCK_LOOKBACK_DAYS", "7"))
    # Explicit window (YYYY-MM-DD). Override the rolling lookback when set.
    start: str | None = os.environ.get("VN_STOCK_START") or None
    end: str | None = os.environ.get("VN_STOCK_END") or None
    # Kafka bootstrap servers; ``None`` lets the producer use its baked-in default.
    bootstrap_servers: str | None = os.environ.get("KAFKA_BOOTSTRAP_SERVERS") or None
