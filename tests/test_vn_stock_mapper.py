"""Tests for the vn_stock OHLCV -> VnStockRecord mapper."""

import datetime
import json
import os

import pytest

import mapper

SCHEMA_FIELDS = {
    "id", "ticker", "open_price", "high_price", "low_price",
    "close_price", "volume", "trading_date", "created_at",
}


def test_to_record_shape_and_values():
    row = {
        "time": datetime.datetime(2024, 1, 2, 7, 0, 0),
        "open": 55.05, "high": 55.52, "low": 54.59, "close": 55.45,
        "volume": 1785800,
    }
    rec = mapper.to_record("FPT", row)

    assert set(rec) == SCHEMA_FIELDS
    assert rec["id"] == "FPT:2024-01-02"   # unique per ticker+day for ReplacingMergeTree
    assert rec["ticker"] == "FPT"
    assert rec["open_price"] == 55.05
    assert rec["high_price"] == 55.52
    assert rec["low_price"] == 54.59
    assert rec["close_price"] == 55.45
    assert rec["volume"] == 1785800
    assert rec["trading_date"] == "2024-01-02"
    assert isinstance(rec["created_at"], str) and rec["created_at"]


def test_to_record_coerces_types():
    row = {"time": "2024-01-03", "open": "55.45", "high": "56.12",
           "low": "54.99", "close": "56.12", "volume": "1373000"}
    rec = mapper.to_record("VCB", row)

    assert isinstance(rec["open_price"], float)
    assert isinstance(rec["volume"], int)
    assert rec["trading_date"] == "2024-01-03"


def test_to_record_handles_missing_time():
    rec = mapper.to_record("HPG", {"time": None, "open": 1, "high": 1,
                                   "low": 1, "close": 1, "volume": 1})
    assert rec["trading_date"] == ""
    assert rec["id"] == "HPG:"


def test_record_validates_against_avro_schema():
    """The mapped record must conform to the real VnStockRecord schema."""
    fastavro = pytest.importorskip("fastavro")
    schema_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data-contracts", "schemas", "vn_stock.avsc",
    )
    with open(schema_path, encoding="utf-8") as f:
        schema = json.load(f)

    row = {"time": datetime.date(2024, 1, 2), "open": 55.05, "high": 55.52,
           "low": 54.59, "close": 55.45, "volume": 1785800}
    rec = mapper.to_record("FPT", row)

    assert fastavro.validate(rec, schema) is True
