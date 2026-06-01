"""vn_stock OHLCV crawler.

Pulls daily (or intraday) candles from the vnstock ``Market`` API and feeds
each one, mapped to the ``VnStockRecord`` schema, into a Kafka producer.

The producer is injected so this module stays decoupled from the auto-generated
``api_crawler/vn_stock/producer.py`` and is unit-testable with a fake producer.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Iterable, List, Mapping, Optional, Protocol

try:  # works both as a package (-m) and as flat scripts
    from .config import CrawlerConfig
    from .mapper import to_record
except ImportError:  # pragma: no cover
    from config import CrawlerConfig
    from mapper import to_record

logger = logging.getLogger("vn_stock_crawler")


class Producer(Protocol):
    """Minimal contract the crawler needs from a producer."""

    def send(self, record: dict) -> Any: ...


def _rows(ohlcv: Any) -> List[Mapping[str, Any]]:
    """Normalise a vnstock ohlcv result (DataFrame or list) to row dicts."""
    if ohlcv is None:
        return []
    to_dict = getattr(ohlcv, "to_dict", None)
    if callable(to_dict):  # pandas DataFrame
        return ohlcv.to_dict(orient="records")
    return list(ohlcv)


class VnStockCrawler:
    def __init__(
        self,
        producer: Producer,
        market: Optional[Any] = None,
        config: Optional[CrawlerConfig] = None,
    ):
        self.producer = producer
        self.config = config or CrawlerConfig()
        self.market = market if market is not None else self._build_market()

    @staticmethod
    def _build_market() -> Any:
        # Imported lazily so importing this module never requires vnstock.
        from vnstock.ui import Market

        return Market()

    def _window(self) -> tuple[str, str]:
        """Resolve the (start, end) date window in 'YYYY-MM-DD' form."""
        cfg = self.config
        end = cfg.end or date.today().isoformat()
        if cfg.start:
            start = cfg.start
        else:
            end_date = date.fromisoformat(end)
            start = (end_date - timedelta(days=cfg.lookback_days)).isoformat()
        return start, end

    def crawl_ticker(self, ticker: str) -> int:
        """Fetch one ticker's candles and produce them. Returns count sent."""
        start, end = self._window()
        logger.info(
            "Fetching OHLCV for %s [%s..%s, interval=%s]",
            ticker, start, end, self.config.interval,
        )
        ohlcv = self.market.equity(ticker).ohlcv(
            start=start, end=end, interval=self.config.interval
        )

        sent = 0
        for row in _rows(ohlcv):
            record = to_record(ticker, row)
            try:
                self.producer.send(record)
                sent += 1
            except Exception:
                logger.exception("Failed to produce record for %s: %s", ticker, record)
        logger.info("Produced %d records for %s", sent, ticker)
        return sent

    def crawl(self, tickers: Optional[Iterable[str]] = None) -> int:
        """Crawl every configured ticker. Returns total records produced."""
        tickers = list(tickers) if tickers is not None else self.config.tickers
        total = 0
        for ticker in tickers:
            try:
                total += self.crawl_ticker(ticker)
            except Exception:
                logger.exception("Crawl failed for ticker %s; continuing", ticker)
        logger.info("vn_stock crawl finished: %d records across %d tickers",
                    total, len(tickers))
        return total
