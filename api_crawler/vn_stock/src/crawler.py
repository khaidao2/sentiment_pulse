"""vn_stock OHLCV crawler.

Pulls daily (or intraday) candles from the vnstock ``Market`` API and feeds
each one, mapped to the ``VnStockRecord`` schema, into a Kafka producer.

The producer is injected so this module stays decoupled from the auto-generated
``api_crawler/vn_stock/producer.py`` and is unit-testable with a fake producer.
"""

from __future__ import annotations

import logging
import time
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


def _symbols(all_symbols_result: Any) -> List[str]:
    """Normalise a vnstock Listing.all_symbols() result (DataFrame or list) to an upper-cased ticker list."""
    if all_symbols_result is None:
        return []
    columns = getattr(all_symbols_result, "columns", None)
    if columns is not None and "symbol" in columns:  # pandas DataFrame
        all_symbols_result = all_symbols_result["symbol"].tolist()
    return [str(s).strip().upper() for s in all_symbols_result if str(s).strip()]


class VnStockCrawler:
    def __init__(
        self,
        producer: Producer,
        market: Optional[Any] = None,
        listing: Optional[Any] = None,
        config: Optional[CrawlerConfig] = None,
    ):
        self.producer = producer
        self.config = config or CrawlerConfig()
        self.market = market if market is not None else self._build_market()
        # Built lazily on first use (only needed when config.crawl_all is set)
        # so importing/running the crawler never requires vnstock unless asked.
        self._listing = listing

    @staticmethod
    def _build_market() -> Any:
        # Imported lazily so importing this module never requires vnstock.
        from vnstock.ui import Market

        return Market()

    @staticmethod
    def _build_listing() -> Any:
        from vnstock import Listing

        return Listing()

    def _resolve_tickers(self) -> List[str]:
        """Resolve which tickers to crawl: every market symbol when `crawl_all`
        is set, otherwise the fixed basket from :class:`CrawlerConfig`."""
        if not self.config.crawl_all:
            return list(self.config.tickers)
        if self._listing is None:
            self._listing = self._build_listing()
        symbols = _symbols(self._listing.all_symbols())
        logger.info("crawl_all enabled: resolved %d symbols from the market listing", len(symbols))
        # all_symbols() consumes ~17 of the 20-request burst quota; wait for the
        # quota window to reset before starting per-ticker requests.
        logger.info("Sleeping 65s after all_symbols() to reset vnstock burst quota")
        time.sleep(65)
        return symbols

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

    def crawl(self, tickers: Optional[Iterable[str]] = None, request_delay: float = 1.1) -> int:
        """Crawl every configured ticker (or every market symbol when `crawl_all`
        is set). Returns total records produced.

        request_delay: seconds to sleep between tickers to stay within
        vnstock's 60 req/min community rate limit (default 1.1s ≈ 54 req/min).
        """
        tickers = list(tickers) if tickers is not None else self._resolve_tickers()
        total = 0
        for i, ticker in enumerate(tickers):
            try:
                total += self.crawl_ticker(ticker)
            except SystemExit:
                # vnstock calls sys.exit() when the burst quota (20 req/window) is
                # exhausted — SystemExit bypasses `except Exception`. Sleep until the
                # quota window resets (~52s per vnstock message) then continue.
                logger.warning(
                    "vnstock called sys.exit() for ticker %s — burst quota hit; sleeping 65s",
                    ticker,
                )
                time.sleep(65)
            except Exception:
                logger.exception("Crawl failed for ticker %s; continuing", ticker)
            if i < len(tickers) - 1:
                time.sleep(request_delay)
        logger.info("vn_stock crawl finished: %d records across %d tickers",
                    total, len(tickers))
        return total
