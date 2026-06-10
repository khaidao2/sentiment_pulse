"""Map a vnstock OHLCV row to a ``VnStockRecord`` dict.

Target schema (data-contracts/schemas/vn_stock.avsc)::

    id, ticker, open_price, close_price, high_price, low_price,
    volume, trading_date, created_at   (all required, no nulls)

``trading_date`` is an Avro ``date`` (logicalType) and ``created_at`` is an
Avro ``timestamp-micros`` (logicalType) — fastavro encodes ``datetime.date``
and ``datetime.datetime`` objects directly for these.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping


def _to_date(value: Any) -> date:
    """Normalise vnstock's ``time`` column to a ``datetime.date``."""
    if value is None:
        return date(1970, 1, 1)
    # pandas.Timestamp / datetime expose .date(); fall back to ISO parsing.
    as_date = getattr(value, "date", None)
    if callable(as_date):
        return as_date()
    return date.fromisoformat(str(value)[:10])


def to_record(ticker: str, row: Mapping[str, Any]) -> dict:
    """Build one Avro-conformant record from a single OHLCV candle.

    ``row`` is one entry of the DataFrame returned by
    ``Market().equity(ticker).ohlcv(...)`` with the columns
    ``time, open, high, low, close, volume``.
    """
    trading_date = _to_date(row.get("time"))
    return {
        # ReplacingMergeTree de-dupes on `id`, so make it unique per candle.
        "id": f"{ticker}:{trading_date.isoformat()}",
        "ticker": ticker,
        "open_price": float(row["open"]),
        "high_price": float(row["high"]),
        "low_price": float(row["low"]),
        "close_price": float(row["close"]),
        "volume": int(row["volume"]),
        "trading_date": trading_date,
        "created_at": datetime.now(timezone.utc),
    }
