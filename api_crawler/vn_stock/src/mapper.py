"""Map a vnstock OHLCV row to a ``VnStockRecord`` dict.

Target schema (data-contracts/schemas/vn_stock.avsc)::

    id, ticker, open_price, close_price, high_price, low_price,
    volume, trading_date, created_at   (all required, no nulls)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def _to_date_str(value: Any) -> str:
    """Normalise vnstock's ``time`` column to a ``YYYY-MM-DD`` string."""
    if value is None:
        return ""
    # pandas.Timestamp / datetime expose .strftime; fall back to str slicing.
    strftime = getattr(value, "strftime", None)
    if callable(strftime):
        return strftime("%Y-%m-%d")
    return str(value)[:10]


def to_record(ticker: str, row: Mapping[str, Any]) -> dict:
    """Build one Avro-conformant record from a single OHLCV candle.

    ``row`` is one entry of the DataFrame returned by
    ``Market().equity(ticker).ohlcv(...)`` with the columns
    ``time, open, high, low, close, volume``.
    """
    trading_date = _to_date_str(row.get("time"))
    return {
        # ReplacingMergeTree de-dupes on `id`, so make it unique per candle.
        "id": f"{ticker}:{trading_date}",
        "ticker": ticker,
        "open_price": float(row["open"]),
        "high_price": float(row["high"]),
        "low_price": float(row["low"]),
        "close_price": float(row["close"]),
        "volume": int(row["volume"]),
        "trading_date": trading_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
