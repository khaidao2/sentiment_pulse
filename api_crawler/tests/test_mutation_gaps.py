"""Tests added from mutation testing (mutmut) of the topcv / careerviet parsers
and CrawlService: boundaries, defaults and branches the original suite left
unpinned. Each test names the mutant class it kills."""

from datetime import date, timedelta

import pytest
from bs4 import BeautifulSoup

from api_crawler.jobs.careerviet.parser import _INT32_MAX, CareerVietParser
from api_crawler.jobs.topcv.parser import TopcvParser
from api_crawler.shared.pipeline.crawl_service import CrawlService
from api_crawler.shared.ports.parser import IParser
from api_crawler.shared.ports.publisher import IPublisher
from api_crawler.shared.ports.source import ISource

_PARSERS = [TopcvParser(), CareerVietParser()]
_IDS = ["topcv", "careerviet"]


# ── CrawlService ───────────────────────────────────────────────────────────
def test_crawl_service_counts_every_record_across_pages():
    # `count += 1` -> `count = 1` would report 1 for any non-empty crawl.
    class Src(ISource):
        def fetch(self):
            yield from ["a b", "c"]

    class Words(IParser):
        def parse(self, raw):
            return ({"w": w} for w in raw.split())

    class Pub(IPublisher):
        def __init__(self):
            self.keys = []

        def produce(self, key, record):
            self.keys.append(key)

        def flush(self, timeout=30.0):
            pass

    pub = Pub()
    assert CrawlService(Src(), Words(), pub, key_fn=lambda r: r["w"]).run() == 3
    assert pub.keys == ["a", "b", "c"]


# ── _usd: strictly positive amounts only, divided by the rate ──────────────
@pytest.mark.parametrize("p", _PARSERS, ids=_IDS)
def test_usd_conversion_boundaries(p):
    assert p._usd(0, 1.0) is None        # `v >= 0`
    assert p._usd(-5, 1.0) is None       # `... or True`
    assert p._usd(1, 1.0) == 1           # `v > 1`
    assert p._usd(50_000_000, 25_000.0) == 2000
    assert p._usd("abc", 1.0) is None
    assert p._usd(None, 1.0) is None


# ── CareerViet._int32: (0, INT32_MAX] ──────────────────────────────────────
def test_careerviet_int32_bounds():
    p = CareerVietParser()
    assert p._int32(1) == 1                          # `1 < n`
    assert p._int32(0) is None                       # `0 <= n`
    assert p._int32(-3) is None                      # `... or True`
    assert p._int32(_INT32_MAX) == _INT32_MAX        # `n < MAX`
    assert p._int32(_INT32_MAX + 1) is None
    assert p._int32("x") is None


# ── posted_days_ago ────────────────────────────────────────────────────────
def test_careerviet_posted_days_ago():
    p = CareerVietParser()
    d10 = date.today() - timedelta(days=10)
    assert p._posted_days_ago({"datePosted": d10.isoformat()}) == 10        # len==10 accepted
    assert p._posted_days_ago({"datePosted": d10.isoformat() + "T09:09:18.141Z"}) == 10  # [:11]
    future = (date.today() + timedelta(days=5)).isoformat()
    assert p._posted_days_ago({"datePosted": future}) == 0                  # max(.., 0)
    assert p._posted_days_ago({"datePosted": "2026-1-1"}) is None           # len < 10
    assert p._posted_days_ago({"datePosted": 20261029}) is None             # not a str
    assert p._posted_days_ago({}) is None
    assert p._posted_days_ago({"datePosted": "not-a-date!"}) is None


def test_topcv_posted_days_ago():
    p = TopcvParser()
    assert p._posted_days_ago({"datePosted": (date.today() - timedelta(days=3)).isoformat()}) == 3
    assert p._posted_days_ago({"datePosted": ""}) is None     # `(ds) or True`
    assert p._posted_days_ago({}) is None
    assert p._posted_days_ago({"datePosted": "garbage"}) is None


# ── min years of experience: months // 12 ──────────────────────────────────
@pytest.mark.parametrize("p", _PARSERS, ids=_IDS)
def test_min_years_experience(p):
    mk = lambda m: {"experienceRequirements": {"monthsOfExperience": m}}  # noqa: E731
    assert p._min_years_experience(mk(24)) == 2      # `// 13`
    assert p._min_years_experience(mk(11)) == 0
    assert p._min_years_experience(mk("24")) is None  # non-int
    assert p._min_years_experience({"experienceRequirements": "2 years"}) is None  # `or`
    assert p._min_years_experience({}) is None


# ── TopCV._salary: currency, single value, type selection ──────────────────
def _topcv_ld(currency=None, **value):
    base = {"value": value}
    if currency is not None:
        base["currency"] = currency
    return {"baseSalary": base}


def test_topcv_salary_usd_is_not_converted_and_vnd_is():
    p = TopcvParser(vnd_per_usd=25_000.0)
    usd = p._salary(_topcv_ld("usd", minValue=1000, maxValue=2000))
    assert (usd["salary_min_usd"], usd["salary_max_usd"], usd["salary_type"]) == (1000, 2000, "RANGE")
    vnd = p._salary(_topcv_ld("VND", minValue=25_000_000, maxValue=50_000_000))
    assert (vnd["salary_min_usd"], vnd["salary_max_usd"]) == (1000, 2000)


def test_topcv_salary_missing_currency_is_treated_as_vnd():
    # `get("currency", None)` -> "NONE" and `"XXXX"` still != USD, but a
    # missing currency must convert, never pass through as USD.
    out = TopcvParser(vnd_per_usd=25_000.0)._salary(_topcv_ld(None, minValue=25_000_000, maxValue=25_000_000))
    assert out["salary_min_usd"] == 1000
    assert out["salary_type"] == "FIXED"


def test_topcv_salary_single_value_fallback():
    out = TopcvParser()._salary(_topcv_ld("USD", value=1500))
    assert out["salary_type"] == "FIXED"
    assert (out["salary_min_usd"], out["salary_max_usd"]) == (1500, 1500)  # lo = hi = value


def test_topcv_salary_one_sided_and_empty():
    p = TopcvParser()
    assert p._salary(_topcv_ld("USD", minValue=900))["salary_type"] == "FROM"
    assert p._salary(_topcv_ld("USD", maxValue=900))["salary_type"] == "UP_TO"
    assert p._salary(_topcv_ld("USD", minValue=0, maxValue=0))["salary_type"] == "NEGOTIABLE"
    assert p._salary({})["salary_type"] == "NEGOTIABLE"
    assert p._salary({"baseSalary": {"value": "Thoả thuận"}})["salary_type"] == "NEGOTIABLE"


# ── TopCV._work_arrangement: detail box first, <h1> fallback ───────────────
def _soup(html):
    return BeautifulSoup(html, "html.parser")


def test_topcv_work_arrangement_sources():
    p = TopcvParser()
    box = '<div class="box-job-information-detail-item">Làm việc  Remote</div>'
    assert p._work_arrangement(_soup(box)) == "REMOTE"
    # box text wins over a hybrid title
    assert p._work_arrangement(_soup("<h1>Hybrid Dev</h1>" + box)) == "REMOTE"
    # no box -> falls back to <h1>, case-insensitively
    assert p._work_arrangement(_soup("<h1>Senior Dev (HYBRID)</h1>")) == "HYBRID"
    assert p._work_arrangement(_soup("<h1>Dev</h1>")) == "AT_OFFICE"
    assert p._work_arrangement(_soup("<p>nothing</p>")) == "AT_OFFICE"
    # multi-word needle must survive the " ".join of separate blocks
    two = ('<div class="box-job-information-detail-item">làm việc</div>'
           '<div class="box-job-information-detail-item">từ xa</div>')
    assert p._work_arrangement(_soup(two)) == "REMOTE"


def test_careerviet_work_arrangement_missing_description():
    p = CareerVietParser()
    assert p._work_arrangement("Backend Dev", None) == "AT_OFFICE"   # None description must not crash
    assert p._work_arrangement("Dev", "fully remote role") == "REMOTE"
    assert p._work_arrangement("Hybrid Dev", "fully remote role") == "HYBRID"


# ── CareerViet._salary / _salary_from_text ─────────────────────────────────
def _cv_ld(currency=None, **value):
    base = {"value": value}
    if currency is not None:
        base["currency"] = currency
    return {"baseSalary": base}


_EMPTY = _soup("<html></html>")


def test_careerviet_salary_ld_branches():
    p = CareerVietParser(vnd_per_usd=25_000.0)
    assert p._salary(_EMPTY, _cv_ld("USD", minValue=1000, maxValue=2000)) == ("RANGE", 1000, 2000, "USD")
    assert p._salary(_EMPTY, _cv_ld("VND", minValue=25_000_000, maxValue=50_000_000)) == ("RANGE", 1000, 2000, "USD")
    assert p._salary(_EMPTY, _cv_ld(None, minValue=25_000_000)) == ("FROM", 1000, None, "USD")  # default VND
    assert p._salary(_EMPTY, _cv_ld("USD", maxValue=700)) == ("UP_TO", None, 700, "USD")
    assert p._salary(_EMPTY, _cv_ld("USD", value=1500)) == ("FIXED", 1500, 1500, "USD")
    assert p._salary(_EMPTY, {}) == ("NEGOTIABLE", None, None, "USD")


def _detail_html(salary_text):
    return _soup(
        f'<div class="job-detail-content"><ul><li><strong>Lương</strong><p>{salary_text}</p></li></ul></div>'
    )


@pytest.mark.parametrize(
    "text, expected",
    [
        ("18 - 25 Triệu VND", (18_000_000, 25_000_000, "VND")),
        ("25 - 18 Triệu", (18_000_000, 25_000_000, "VND")),            # min/max ordering
        ("Trên 20 Triệu", (20_000_000, None, "VND")),
        ("Từ 20 Triệu", (20_000_000, None, "VND")),
        ("Đến 30 Triệu", (None, 30_000_000, "VND")),
        ("Tới 30 Triệu", (None, 30_000_000, "VND")),
        ("Dưới 30 Triệu", (None, 30_000_000, "VND")),
        ("20 Triệu", (20_000_000, 20_000_000, "VND")),
        ("1,000 - 2,000 USD", (1000, 2000, "USD")),
        ("$1500", (1500, 1500, "USD")),
        ("Cạnh tranh", (None, None, None)),
        ("", (None, None, None)),
    ],
)
def test_careerviet_salary_from_text(text, expected):
    assert CareerVietParser()._salary_from_text(text) == expected


def test_careerviet_salary_falls_back_to_detail_row():
    p = CareerVietParser(vnd_per_usd=25_000.0)
    soup = _detail_html("25 - 50 Triệu VND")
    assert p._detail_item(soup, "Lương") == "25 - 50 Triệu VND"
    assert p._salary(soup, {}) == ("RANGE", 1000, 2000, "USD")
    usd = _detail_html("1,000 - 2,000 USD")
    assert p._salary(usd, {}) == ("RANGE", 1000, 2000, "USD")
