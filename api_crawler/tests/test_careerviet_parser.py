"""Parse saved CareerViet detail pages and assert the records are correct.

Offline only — the fixtures are two real detail pages saved once (scripts and
images stripped, JSON-LD kept), so no test touches the network.

Two fixtures because the salary logic has two shapes CareerViet actually
serves:
  * careerviet_detail.html         -> salary hidden ("Cạnh tranh") -> NEGOTIABLE
  * careerviet_detail_salary.html  -> numeric VND range           -> RANGE

Fixtures/fixtures helpers are defined in THIS module (not in the shared
tests/conftest.py) so the itviec and careerviet suites stay independent.
conftest.py still puts the repo root and data-contracts/src on sys.path.
"""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from api_crawler.jobs.careerviet.parser import CareerVietParser
from api_crawler.jobs.careerviet.source import CareerVietSource
from sentpul.validator import load_avro_schema, validate_record

_FIXTURES = Path(__file__).parent / "fixtures"
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "data-contracts"
    / "schemas"
    / "raw"
    / "careerviet.avsc"
)

EXPECTED_TITLE = "Junior IT Service Specialist"
EXPECTED_COMPANY = "CÔNG TY TNHH HADDAD BRANDS VIETNAM REGIONAL"
EXPECTED_URL = (
    "https://careerviet.vn/vi/tim-viec-lam/junior-it-service-specialist.35C8C434.html"
)


def _parse(name: str) -> dict:
    html = (_FIXTURES / name).read_text(encoding="utf-8")
    return next(iter(CareerVietParser().parse(html)))


@pytest.fixture(scope="module")
def record() -> dict:
    """Posting whose salary is hidden ("Cạnh tranh")."""
    return _parse("careerviet_detail.html")


@pytest.fixture(scope="module")
def salary_record() -> dict:
    """Posting with a disclosed numeric VND range."""
    return _parse("careerviet_detail_salary.html")


@pytest.fixture(scope="module")
def schema() -> dict:
    return load_avro_schema(str(_SCHEMA_PATH))


# ── contract ──────────────────────────────────────────────────────────────
def test_record_matches_avro_schema(record, schema):
    validate_record(record, schema)  # raises on invalid shape


def test_salary_record_matches_avro_schema(salary_record, schema):
    validate_record(salary_record, schema)


def test_field_names_match_the_pipeline_contract(record, schema):
    """The record must carry exactly the fields dbt / reverse-ETL expect."""
    assert set(record) == {f["name"] for f in schema["fields"]}


# ── basic fields ──────────────────────────────────────────────────────────
def test_basic_fields(record):
    assert record["title"] == EXPECTED_TITLE
    assert record["company_name"] == EXPECTED_COMPANY
    assert record["company_url"].endswith(
        "/vi/nha-tuyen-dung/cong-ty-tnhh-haddad-brands-vietnam-regional.35A7FB90.html"
    )
    assert record["source"] == "careerviet"
    assert record["work_arrangement"] == "AT_OFFICE"


def test_location_is_the_city(record, salary_record):
    assert record["location"] == "Hồ Chí Minh"
    assert salary_record["location"] == "Hà Nội"


def test_description_and_requirements(record):
    assert "IT support" in record["description"]
    assert len(record["responsibilities"]) >= 5
    assert len(record["skills"]) >= 5
    # bullets stripped, heading not leaked into the items
    assert all(not s.startswith("•") for s in record["skills"])
    assert "Yêu Cầu Công Việc" not in record["skills"]


def test_benefits_and_tech_stack(record):
    assert "Chế độ bảo hiểm" in record["benefits"]
    assert len(record["tech_stack"]) >= 1
    assert record["nice_to_have"] == []


# ── salary / currency ─────────────────────────────────────────────────────
def test_hidden_salary_is_negotiable(record):
    assert record["salary_type"] == "NEGOTIABLE"
    assert record["salary_min_usd"] is None
    assert record["salary_max_usd"] is None
    # Always USD: every source converts, so silver_history's `select *` union
    # never mixes units in the salary_*_usd columns.
    assert record["currency"] == "USD"


def test_disclosed_salary_is_converted_to_usd(salary_record):
    assert salary_record["salary_type"] == "RANGE"
    # "Lương: 18 - 25 Triệu VND" -> baseSalary 18_000_000 .. 25_000_000 VND,
    # converted at the contract's crawler.vnd_per_usd = 25_000.
    assert salary_record["salary_min_usd"] == 720
    assert salary_record["salary_max_usd"] == 1_000
    assert salary_record["currency"] == "USD"


def test_salary_conversion_uses_the_injected_rate():
    """The rate is contract-fed, not hardcoded in the parser."""
    html = (_FIXTURES / "careerviet_detail_salary.html").read_text(encoding="utf-8")
    rec = next(iter(CareerVietParser(vnd_per_usd=20_000).parse(html)))
    assert rec["salary_min_usd"] == 900          # 18_000_000 / 20_000
    assert rec["salary_max_usd"] == 1_250        # 25_000_000 / 20_000


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Cạnh tranh", (None, None, None)),
        ("Lương: 18 - 25 Triệu VND", (18_000_000, 25_000_000, "VND")),
        ("15 Tr - 20 Tr VND", (15_000_000, 20_000_000, "VND")),
        ("Trên 20 Triệu VND", (20_000_000, None, "VND")),
        ("$1,000 - $2,000", (1_000, 2_000, "USD")),
    ],
)
def test_salary_text_fallback(text, expected):
    assert CareerVietParser()._salary_from_text(text) == expected


# ── natural key / dedup metadata ──────────────────────────────────────────
def test_job_url_and_id(record):
    assert record["job_url"] == EXPECTED_URL
    assert record["job_id"] == hashlib.sha256(EXPECTED_URL.encode()).hexdigest()


def test_crawled_at_is_iso_utc(record):
    dt = datetime.strptime(record["crawled_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert dt.year >= 2024


# ── JSON-LD enrichment ────────────────────────────────────────────────────
def test_jsonld_fields(record, salary_record):
    assert "CNTT - Phần mềm" in record["company_industry"]
    assert record["country"] == "VN"
    assert record["min_years_experience"] == 1          # 12 months // 12
    assert salary_record["min_years_experience"] == 3   # 36 months // 12
    assert isinstance(record["posted_days_ago"], int) and record["posted_days_ago"] >= 0


# ── source navigation (pure functions, no network) ────────────────────────
def test_listing_paging_url():
    src = CareerVietSource()
    base = "https://careerviet.vn/viec-lam/cntt-phan-mem-c1-vi.html"
    assert src._page_url(1) == base
    assert src._page_url(3) == (
        "https://careerviet.vn/viec-lam/cntt-phan-mem-c1-trang-3-vi.html"
    )


def test_detail_urls_from_listing_cards():
    listing = """
    <div class="job-item" id="job-item-35C8C434">
      <div class="title"><h2>
        <a class="job_link" href="/vi/tim-viec-lam/junior-it-service-specialist.35C8C434.html">x</a>
      </h2></div>
      <a class="job_link" href="/vi/tim-viec-lam/junior-it-service-specialist.35C8C434.html">dup</a>
    </div>
    <div class="job-item">
      <div class="title"><h2><a href="/vi/nha-tuyen-dung/some-company.35A7FB90.html">employer</a></h2></div>
    </div>
    """
    assert CareerVietSource()._detail_urls(listing) == [EXPECTED_URL]


@pytest.mark.parametrize(
    "text, expected",
    [
        # "remote setup of devices" in an onsite support role must NOT read as remote
        ("Conduct remote setup and device enrollment", "AT_OFFICE"),
        ("Fully remote position, WFH friendly", "REMOTE"),
        ("Làm việc từ xa 2 ngày/tuần", "REMOTE"),
        ("Hybrid working model", "HYBRID"),
        ("Làm việc tại văn phòng Hồ Chí Minh", "AT_OFFICE"),
    ],
)
def test_work_arrangement_needs_a_phrase_not_a_bare_word(text, expected):
    assert CareerVietParser()._work_arrangement("Engineer", text) == expected
