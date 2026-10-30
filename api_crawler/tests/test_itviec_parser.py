"""Parse a saved itviec detail page and assert the record is correct.

Two groups of tests:
  - shape/contract tests (schema-valid, basic fields) — should be GREEN.
  - bug-catchers (location, skills) — RED until the selectors are tuned.

Avro validation only checks TYPES/required fields, not correctness — "IT Jobs"
is a valid non-empty string, so the location bug slips past validation. The
explicit assertions below are what actually catch it.
"""

from sentpul.validator import validate_record

EXPECTED_TITLE = "Network Engineer (CDS) (Open For Freshers)"
EXPECTED_COMPANY = "LG CNS Việt Nam"


def test_record_matches_avro_schema(record, schema):
    validate_record(record, schema)  # raises on invalid shape


def test_basic_fields(record):
    assert record["title"] == EXPECTED_TITLE
    assert record["company_name"] == EXPECTED_COMPANY
    assert record["work_arrangement"] == "AT_OFFICE"


def test_tech_stack_present(record):
    assert len(record["tech_stack"]) >= 1


def test_description_present(record):
    assert record["description"]


# ── bug-catchers: RED now, GREEN after selector tuning ────────────────────
def test_location_is_real(record):
    assert record["location"] == "Ha Noi"


def test_skills_extracted(record):
    assert len(record["skills"]) >= 2
    assert record["skills"] != ["[Required]"]


# ── natural key / dedup metadata ──────────────────────────────────────────
def test_job_url_and_id(record):
    import hashlib

    assert record["job_url"] == (
        "https://itviec.com/it-jobs/"
        "network-engineer-cds-open-for-freshers-lg-cns-viet-nam-0956"
    )
    assert record["job_id"] == hashlib.sha256(record["job_url"].encode()).hexdigest()


def test_crawled_at_is_iso_utc(record):
    from datetime import datetime

    # parses as UTC ISO-8601 and round-trips
    dt = datetime.strptime(record["crawled_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert dt.year >= 2024


# ── JSON-LD enrichment ────────────────────────────────────────────────────
def test_jsonld_fields(record):
    assert record["company_industry"] == "Information Technology"
    assert record["country"] == "VN"
    assert record["min_years_experience"] == 0          # 10 months // 12
    assert isinstance(record["posted_days_ago"], int) and record["posted_days_ago"] >= 0


def test_description_covers_the_whole_section_not_just_the_first_block(record):
    """Regression: `description` used to read only h.find_next_sibling().

    itviec renders most descriptions as a bullet list, so the first sibling was
    an intro line or nothing and the body landed in `responsibilities` instead.
    Live index before the fix: median description 198 chars vs 868 of
    responsibilities, 231 jobs thin-description/fat-responsibilities.
    """
    desc = record["description"] or ""
    joined = "\n".join(record["responsibilities"])
    assert len(desc) > 400, "description truncated to the first block again"
    # Every bullet the section yields must appear in the section text.
    for item in record["responsibilities"]:
        assert item in desc, f"missing from description: {item[:60]!r}"
    assert len(desc) >= len(joined)
