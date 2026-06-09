"""Tests for VnStockCrawler using fake market/producer (no network/Kafka)."""

import datetime

from config import CrawlerConfig
from crawler import VnStockCrawler, _rows, _symbols

ROW = {"time": datetime.date(2024, 1, 2), "open": 55.05, "high": 55.52,
       "low": 54.59, "close": 55.45, "volume": 1785800}


class FakeOHLCV:
    """Mimics a pandas DataFrame's .to_dict(orient="records")."""
    def __init__(self, rows):
        self._rows = rows

    def to_dict(self, orient=None):
        return self._rows


class FakeEquity:
    def __init__(self, calls):
        self.calls = calls

    def ohlcv(self, start, end, interval):
        self.calls.append((start, end, interval))
        return FakeOHLCV([ROW, {**ROW, "time": "2024-01-03"}])


class FakeMarket:
    def __init__(self):
        self.calls = []

    def equity(self, ticker):
        return FakeEquity(self.calls)


class FakeSymbolFrame:
    """Mimics a pandas DataFrame with a 'symbol' column for all_symbols()."""
    def __init__(self, symbols):
        self.columns = ["symbol", "organ_name"]
        self._symbols = symbols

    def __getitem__(self, key):
        assert key == "symbol"
        return self

    def tolist(self):
        return list(self._symbols)


class FakeListing:
    def __init__(self, symbols):
        self._symbols = symbols
        self.calls = 0

    def all_symbols(self):
        self.calls += 1
        return FakeSymbolFrame(self._symbols)


class FakeProducer:
    def __init__(self):
        self.sent = []

    def send(self, record):
        self.sent.append(record)


def _crawler(producer, market=None, listing=None, **cfg):
    return VnStockCrawler(
        producer=producer,
        market=market or FakeMarket(),
        listing=listing,
        config=CrawlerConfig(**cfg),
    )


def test_crawl_produces_every_row_for_every_ticker():
    producer = FakeProducer()
    market = FakeMarket()
    crawler = _crawler(producer, market, tickers=["FPT", "VCB"], interval="1D")

    total = crawler.crawl(request_delay=0)

    assert total == 4                      # 2 tickers x 2 rows
    assert len(producer.sent) == 4
    assert {r["ticker"] for r in producer.sent} == {"FPT", "VCB"}
    assert all(call[2] == "1D" for call in market.calls)


def test_window_uses_explicit_dates():
    crawler = _crawler(FakeProducer(), start="2024-01-01", end="2024-01-31")
    assert crawler._window() == ("2024-01-01", "2024-01-31")


def test_window_falls_back_to_lookback():
    crawler = _crawler(FakeProducer(), start=None, end="2024-01-31", lookback_days=7)
    assert crawler._window() == ("2024-01-24", "2024-01-31")


def test_send_errors_are_isolated_per_record():
    class FlakyProducer:
        def __init__(self):
            self.sent = []
            self.calls = 0

        def send(self, record):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient kafka error")
            self.sent.append(record)

    producer = FlakyProducer()
    crawler = _crawler(producer, tickers=["FPT"])

    total = crawler.crawl(request_delay=0)      # 2 rows; first send raises, second succeeds

    assert total == 1
    assert len(producer.sent) == 1


def test_rows_normalises_dataframe_list_and_none():
    assert _rows(None) == []
    assert _rows([{"a": 1}]) == [{"a": 1}]
    assert _rows(FakeOHLCV([{"a": 1}, {"b": 2}])) == [{"a": 1}, {"b": 2}]


def test_symbols_normalises_dataframe_list_and_none():
    assert _symbols(None) == []
    assert _symbols(["fpt", " vcb ", ""]) == ["FPT", "VCB"]
    assert _symbols(FakeSymbolFrame(["fpt", "hpg"])) == ["FPT", "HPG"]


def test_crawl_all_resolves_tickers_from_listing_and_ignores_configured_basket():
    producer = FakeProducer()
    market = FakeMarket()
    listing = FakeListing(["fpt", "vcb", "hpg"])
    crawler = _crawler(
        producer, market, listing,
        tickers=["IGNORED"], crawl_all=True, interval="1D",
    )

    total = crawler.crawl(request_delay=0)

    assert listing.calls == 1
    assert total == 6                      # 3 tickers x 2 rows
    assert {r["ticker"] for r in producer.sent} == {"FPT", "VCB", "HPG"}


def test_crawl_all_disabled_uses_configured_tickers_without_building_listing():
    crawler = _crawler(FakeProducer(), tickers=["FPT"], crawl_all=False)

    assert crawler._resolve_tickers() == ["FPT"]
    assert crawler._listing is None        # never built — no vnstock import needed
