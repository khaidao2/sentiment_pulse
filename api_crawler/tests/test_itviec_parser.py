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


# ── JSON-LD enrichment ────────────────────────────────────────────────────
def test_jsonld_fields(record):
    assert record["company_industry"] == "Information Technology"
    assert record["country"] == "VN"
    assert record["min_years_experience"] == 0          # 10 months // 12
    assert isinstance(record["posted_days_ago"], int) and record["posted_days_ago"] >= 0
