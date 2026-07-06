"""Test setup: make api_crawler + sentpul importable, expose parsed fixtures."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]                 # repo root
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "data-contracts" / "src"))   # sentpul

import pytest  # noqa: E402

from api_crawler.jobs.itviec.parser import ItviecParser  # noqa: E402
from sentpul.validator import load_avro_schema  # noqa: E402

_FIXTURE = Path(__file__).parent / "fixtures" / "itviec_detail.html"
_SCHEMA = _ROOT / "data-contracts" / "schemas" / "raw" / "itviec.avsc"


@pytest.fixture
def detail_html() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def record(detail_html) -> dict:
    return next(iter(ItviecParser().parse(detail_html)))


@pytest.fixture
def schema() -> dict:
    return load_avro_schema(str(_SCHEMA))
