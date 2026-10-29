"""Parse a saved TopCV detail page and assert the record is correct.

Offline only — the fixtures are one real listing page and one real detail page,
saved once with <script>/<style> stripped (the JSON-LD kept) and the oversized
tooltip `title=` payloads removed. No test touches the network.

Three groups:
  * contract/shape — the record validates against data-contracts/schemas/raw/
    topcv.avsc, so parser drift from the contract fails the build;
  * field correctness — Avro only checks types, so the real selectors are pinned
    with explicit values (location/salary/skills would pass validation while
    being wrong);
  * salary table — driven by tiny synthetic JSON-LD snippets, because TopCV
    serves several baseSalary shapes and only one of them is in the fixture.

Fixtures/helpers live in THIS module (not the shared tests/conftest.py) so the
per-source suites stay independent. conftest.py still puts the repo root and
data-contracts/src on sys.path.
"""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from api_crawler.jobs.topcv.parser import TopcvParser
from api_crawler.jobs.topcv.source import TopcvSource
from sentpul.validator import load_avro_schema, validate_record

_FIXTURES = Path(__file__).parent / "fixtures"
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "data-contracts"
    / "schemas"
    / "raw"
    / "topcv.avsc"
)

EXPECTED_TITLE = "Junior C/C++ Developer (Qt Framework)"
EXPECTED_COMPANY = "CÔNG TY TNHH ISB VIỆT NAM"
EXPECTED_URL = (
    "https://www.topcv.vn/viec-lam/junior-c-c-developer-qt-framework/2335678.html"
)
# The fixture's baseSalary: 10,000,000 - 25,000,000 VND / month.
VND_PER_USD = 25_000.0


@pytest.fixture
def detail_html() -> str:
    return (_FIXTURES / "topcv_detail.html").read_text(encoding="utf-8")


@pytest.fixture
def listing_html() -> str:
    return (_FIXTURES / "topcv_listing.html").read_text(encoding="utf-8")


@pytest.fixture
def record(detail_html) -> dict:
    return next(iter(TopcvParser(vnd_per_usd=VND_PER_USD).parse(detail_html)))


@pytest.fixture
def schema() -> dict:
    return load_avro_schema(str(_SCHEMA_PATH))


# ── contract / shape ──────────────────────────────────────────────────────
def test_record_matches_avro_schema(record, schema):
    validate_record(record, schema)  # raises on invalid shape


def test_source_is_topcv(record):
    assert record["source"] == "topcv"


def test_non_job_page_yields_nothing():
    assert list(TopcvParser().parse("<html><body><p>404</p></body></html>")) == []


def test_canonical_field_names_present(record):
    # The names the dbt silver models and the reverse ETL read. Keep in sync.
    expected = {
        "job_id", "job_url", "title", "salary_type", "salary_min_usd",
        "salary_max_usd", "currency", "location", "work_arrangement",
        "posted_days_ago", "min_years_experience", "description", "crawled_at",
        "source", "tech_stack", "skills", "benefits", "company_name",
        "company_url", "company_industry", "country",
    }
    assert expected <= set(record)


# ── field correctness ─────────────────────────────────────────────────────
def test_basic_fields(record):
    assert record["title"] == EXPECTED_TITLE
    assert record["company_name"] == EXPECTED_COMPANY
    assert record["company_url"].startswith("https://www.topcv.vn/cong-ty/")
    assert record["work_arrangement"] == "AT_OFFICE"


def test_location_is_the_province(record):
    # addressLocality is the ward ("Phường Tân Bình") — too fine. We want the
    # province, matching itviec's granularity.
    assert record["location"] == "Hồ Chí Minh"


def test_jsonld_fields(record):
    assert record["company_industry"].startswith("IT phần mềm")
    assert record["country"] == "VN"
    assert record["company_size"] == "100-499 nhân viên"
    assert record["min_years_experience"] == 1  # 12 months // 12
    assert isinstance(record["posted_days_ago"], int) and record["posted_days_ago"] >= 0


def test_tech_stack_from_skill_chips(record):
    assert "Qt Framework" in record["tech_stack"]
    assert "C/C++" in record["tech_stack"]
    # comma-split, so no element still carries a separator
    assert all("," not in t for t in record["tech_stack"])


def test_description_and_responsibilities(record):
    assert record["description"]
    assert "C/C++ code" in record["description"]
    assert len(record["responsibilities"]) == 6


def test_skills_are_the_requirement_bullets(record):
    assert len(record["skills"]) == 4
    assert record["skills"][0].startswith("At least 1 year")


def test_nice_to_have_split_off_the_requirements(record):
    # The section carries a "Nice-to-Have" marker then a second <ul>; those
    # bullets must NOT land in `skills`.
    assert len(record["nice_to_have"]) == 3
    assert any("CMake" in s for s in record["nice_to_have"])
    assert not any("CMake" in s for s in record["skills"])


def test_benefits_extracted(record):
    assert len(record["benefits"]) >= 5
    assert any("13th month salary" in b for b in record["benefits"])
    # the "- " bullet prefixes TopCV writes are stripped
    assert not any(b.startswith("-") for b in record["benefits"])


# ── natural key / dedup metadata ──────────────────────────────────────────
def test_job_url_and_id(record):
    assert record["job_url"] == EXPECTED_URL
    assert record["job_id"] == hashlib.sha256(EXPECTED_URL.encode()).hexdigest()


def test_crawled_at_is_iso_utc(record):
    dt = datetime.strptime(record["crawled_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert dt.year >= 2024


# ── salary: VND -> USD, and every disclosure shape ────────────────────────
def test_salary_converted_from_vnd(record):
    assert record["salary_type"] == "RANGE"
    assert record["currency"] == "USD"
    assert record["salary_min_usd"] == 400       # 10,000,000 VND / 25,000
    assert record["salary_max_usd"] == 1_000     # 25,000,000 VND / 25,000


def _page(base_salary_json: str) -> str:
    """Minimal detail page: just the JSON-LD the salary logic reads."""
    return (
        '<html><head><link rel="canonical" href="https://www.topcv.vn/viec-lam/x/1.html">'
        '<script type="application/ld+json">'
        '{"@context":"http://schema.org/","@type":"JobPosting","title":"X"'
        + (f',"baseSalary":{base_salary_json}' if base_salary_json else "")
        + "}</script></head><body><h1>X</h1></body></html>"
    )


@pytest.mark.parametrize(
    "base_salary, expected",
    [
        # "Thoả thuận" — TopCV omits baseSalary entirely.
        ("", ("NEGOTIABLE", None, None)),
        # min + max -> RANGE
        (
            '{"currency":"VND","value":{"minValue":10000000,"maxValue":25000000}}',
            ("RANGE", 400, 1000),
        ),
        # equal bounds -> FIXED
        (
            '{"currency":"VND","value":{"minValue":20000000,"maxValue":20000000}}',
            ("FIXED", 800, 800),
        ),
        # min only -> FROM
        ('{"currency":"VND","value":{"minValue":30000000}}', ("FROM", 1200, None)),
        # max only -> UP_TO
        ('{"currency":"VND","value":{"maxValue":50000000}}', ("UP_TO", None, 2000)),
        # already USD -> no conversion
        (
            '{"currency":"USD","value":{"minValue":1500,"maxValue":2500}}',
            ("RANGE", 1500, 2500),
        ),
        # unusable payload -> NEGOTIABLE, never a bogus 0
        ('{"currency":"VND","value":{}}', ("NEGOTIABLE", None, None)),
    ],
)
def test_salary_shapes(base_salary, expected):
    rec = next(iter(TopcvParser(vnd_per_usd=VND_PER_USD).parse(_page(base_salary))))
    assert (rec["salary_type"], rec["salary_min_usd"], rec["salary_max_usd"]) == expected
    assert rec["currency"] == "USD"


# ── listing -> detail URLs (pure, no network) ─────────────────────────────
def test_detail_urls_from_listing(listing_html):
    urls = TopcvSource()._detail_urls(listing_html)
    assert len(urls) == 10                      # the fixture keeps 10 job cards
    assert EXPECTED_URL in urls
    # tracking query (?ta_source=...&u_sr_id=...) is stripped so the fetched URL
    # equals the page's canonical URL — the natural key.
    assert all("?" not in u for u in urls)
    assert all(u.startswith("https://www.topcv.vn/viec-lam/") for u in urls)
    assert len(set(urls)) == len(urls)          # deduped
