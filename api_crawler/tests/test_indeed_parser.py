"""Parse a saved Indeed search page and assert the records are correct.

Offline only — the fixture is one real `vn.indeed.com/jobs` page reduced to the
job-card JSON the parser reads, so no test touches the network.

The three cards in it are the shapes Indeed actually serves, and the salary
logic is why they were chosen:
  * a card whose period Indeed mislabels (10–20M VND tagged YEARLY) -> the
    implausible monthly figure is refused, not published
  * a card with no salary at all                                    -> NEGOTIABLE
  * a card with a real monthly VND range                            -> RANGE

Fixture helpers live in THIS module (not the shared conftest.py) so the
per-source suites stay independent. conftest.py still puts the repo root and
data-contracts/src on sys.path.
"""

import hashlib
from pathlib import Path

import pytest

from api_crawler.jobs.indeed.parser import IndeedParser
from api_crawler.jobs.indeed.source import IndeedSource
from sentpul.validator import load_avro_schema, validate_record

_FIXTURES = Path(__file__).parent / "fixtures"
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "data-contracts"
    / "schemas"
    / "raw"
    / "indeed.avsc"
)


@pytest.fixture(scope="module")
def records() -> list:
    html = (_FIXTURES / "indeed_search.html").read_text(encoding="utf-8")
    return list(IndeedParser().parse(html))


@pytest.fixture(scope="module")
def schema() -> dict:
    return load_avro_schema(str(_SCHEMA_PATH))


# ── contract ──────────────────────────────────────────────────────────────
def test_every_record_matches_the_avro_schema(records, schema):
    assert records
    for record in records:
        validate_record(record, schema)  # raises on invalid shape


def test_field_names_match_the_pipeline_contract(records, schema):
    """The records must carry exactly the fields dbt / reverse-ETL expect."""
    expected = {f["name"] for f in schema["fields"]}
    for record in records:
        assert set(record) == expected


# ── one page, many postings ───────────────────────────────────────────────
def test_a_search_page_yields_one_record_per_card(records):
    # Unlike every other source here, one fetched page is many postings --
    # there is no detail request to make.
    assert len(records) == 3
    assert {r["source"] for r in records} == {"indeed"}


def test_basic_fields(records):
    first = records[0]
    assert first["title"] == "Data Engineer (Junior/ Fresher)"
    assert first["company_name"] == "CMC Telecom"
    assert first["company_url"].startswith("https://vn.indeed.com/cmp/")
    assert first["location"] == "Hà Nội"
    assert first["work_arrangement"] == "AT_OFFICE"


def test_job_url_is_the_canonical_viewjob_link_and_the_id_hashes_it(records):
    for record in records:
        assert record["job_url"].startswith("https://vn.indeed.com/viewjob?jk=")
        assert record["job_id"] == hashlib.sha256(
            record["job_url"].encode("utf-8")
        ).hexdigest()
    # Distinct postings must not collide on the dedup key.
    assert len({r["job_id"] for r in records}) == len(records)


def test_the_detail_only_fields_are_empty_not_invented(records):
    # robots.txt Disallows /viewjob, so these have no source. A parser that
    # guessed them from the title would be inventing the product's data.
    for record in records:
        assert record["responsibilities"] == []
        assert record["skills"] == []
        assert record["nice_to_have"] == []
        assert record["tech_stack"] == []
        assert record["min_years_experience"] is None


def test_a_truncated_snippet_says_it_was_truncated(records):
    clipped = [r for r in records if "truncated by Indeed" in (r["description"] or "")]
    assert clipped, "Indeed clips its snippets; the record must not hide that"
    # The bare ellipsis is replaced by the marker, not left dangling.
    assert clipped[0]["description"].endswith("full text is on the posting]")
    assert "hỗ trợ…" not in clipped[0]["description"]
    assert "<" not in clipped[0]["description"]  # card markup stripped


def test_posted_days_ago_comes_from_the_epoch(records):
    assert all(
        r["posted_days_ago"] is None or r["posted_days_ago"] >= 0 for r in records
    )
    assert any(r["posted_days_ago"] is not None for r in records)


# ── salary ────────────────────────────────────────────────────────────────
def test_a_real_monthly_range_is_kept_in_vnd(records):
    ranged = [r for r in records if r["salary_type"] == "RANGE"]
    assert len(ranged) == 1
    record = ranged[0]
    assert record["currency"] == "VND"       # no FX rate is invented here
    assert record["salary_min_usd"] == 17_000_000
    assert record["salary_max_usd"] == 18_000_000


def test_an_implausible_monthly_figure_is_refused(records):
    # The fixture's first card is "10.000.000 đ - 20.000.000 đ / năm" tagged
    # YEARLY, which converts to 833,333 VND/month -- below Vietnam's minimum
    # wage, so Indeed's period is wrong. Publishing it would feed a fiction
    # into every salary average downstream.
    assert records[0]["salary_type"] == "NEGOTIABLE"
    assert records[0]["salary_min_usd"] is None
    assert records[0]["salary_max_usd"] is None


def test_a_card_without_a_salary_is_negotiable(records):
    assert any(
        r["salary_type"] == "NEGOTIABLE"
        and r["salary_min_usd"] is None
        and r["salary_max_usd"] is None
        for r in records
    )


def test_a_page_with_no_cards_yields_nothing():
    assert list(IndeedParser().parse("<html><body>no cards here</body></html>")) == []


# ── the robots guard ──────────────────────────────────────────────────────
def test_the_source_refuses_a_disallowed_url():
    # The URL comes from a contract, so a typo there must fail loudly rather
    # than quietly crawl a path robots.txt forbids.
    for url in (
        "https://vn.indeed.com/viewjob",
        "https://vn.indeed.com/m/viewjob",
        "https://vn.indeed.com/jobs?q=data&rt=nc",
        "https://vn.indeed.com/jobs?q=data&serpstart=10",
    ):
        with pytest.raises(ValueError):
            IndeedSource(search_url=url)


def test_the_sign_in_page_is_recognised_as_the_end_of_pagination():
    # Indeed answers an anonymous request for page two with this, not results.
    # Read as HTML it would parse to zero jobs and look like an exhausted
    # query, which is a different fact about the world.
    from api_crawler.jobs.indeed.source import _is_sign_in_page

    assert _is_sign_in_page(
        "<html><head><title>\n Đăng nhập | Tài khoản Indeed</title></head></html>"
    )
    assert not _is_sign_in_page(
        '<html><head><title>500+ Backend Developer Việc làm</title></head>'
        '<body><script>"jobkey":"abc"</script></body></html>'
    )


def test_the_search_url_is_accepted():
    IndeedSource(search_url="https://vn.indeed.com/jobs", queries=["data engineer"])


# ── backoff ───────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, text: str, fail: bool = False):
        self.text = text
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            raise RuntimeError("HTTP Error 403: ")


class _Session:
    """Refuses `refusals` times, then serves the page."""

    def __init__(self, refusals: int, page: str):
        self.refusals = refusals
        self.page = page
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        if self.calls <= self.refusals:
            return _Resp("", fail=True)
        return _Resp(self.page)


def test_a_refusal_is_waited_out_rather_than_losing_the_query(monkeypatch):
    # Indeed 403s a crawler that goes too fast. Abandoning the query on the
    # first refusal is how a crawl silently returns a fraction of the postings.
    slept = []
    monkeypatch.setattr("api_crawler.jobs.indeed.source.time.sleep", slept.append)
    source = IndeedSource(queries=["x"], backoff=60.0, attempts=3)
    session = _Session(refusals=2, page='{"jobkey":"ok"}')

    page = source._get(session, "https://vn.indeed.com/jobs")

    assert page == '{"jobkey":"ok"}'
    assert session.calls == 3
    assert slept == [60.0, 120.0]   # linear, and bounded by `attempts`


def test_a_persistent_refusal_gives_up_without_raising(monkeypatch):
    monkeypatch.setattr("api_crawler.jobs.indeed.source.time.sleep", lambda s: None)
    source = IndeedSource(queries=["x"], attempts=2)

    assert source._get(_Session(refusals=9, page="never"), "https://vn.indeed.com/jobs") is None


def test_a_challenge_page_is_not_mistaken_for_results(monkeypatch):
    # The edge serves its challenge with a 200, so status is not the test.
    monkeypatch.setattr("api_crawler.jobs.indeed.source.time.sleep", lambda s: None)
    source = IndeedSource(queries=["x"], attempts=1)
    session = _Session(refusals=0, page="<html><title>Security Check - Indeed.com</title>")

    assert source._get(session, "https://vn.indeed.com/jobs") is None
