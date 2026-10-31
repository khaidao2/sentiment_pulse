"""Test setup: make api_crawler + sentpul importable, expose parsed fixtures."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]                 # repo root
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "data-contracts" / "src"))   # sentpul

import pytest  # noqa: E402

from api_crawler.jobs.itviec.parser import ItviecParser  # noqa: E402
from sentpul.validator import load_avro_schema  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"
_FIXTURE = _FIXTURES / "itviec_detail.html"
_SCHEMA = _ROOT / "data-contracts" / "schemas" / "raw" / "itviec.avsc"
_COURSES_SCHEMA = _ROOT / "data-contracts" / "schemas" / "learning" / "courses.avsc"


@pytest.fixture
def detail_html() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def record(detail_html) -> dict:
    return next(iter(ItviecParser().parse(detail_html)))


@pytest.fixture
def schema() -> dict:
    return load_avro_schema(str(_SCHEMA))


# ── courses crawler ───────────────────────────────────────────────────────
# The courses job reads three JSON catalogues rather than one HTML page, so its
# fixtures are per-source payloads and the parser is handed an envelope (see
# jobs/courses/source.py for why the source name travels with the page).


@pytest.fixture
def courses_schema() -> dict:
    return load_avro_schema(str(_COURSES_SCHEMA))


@pytest.fixture
def courses_page():
    """Build the envelope the parser expects from a saved catalogue payload."""
    import json

    def _build(source: str) -> str:
        page = (_FIXTURES / f"courses_{source}.json").read_text(encoding="utf-8")
        return json.dumps({"source": source, "page": page})

    return _build


@pytest.fixture
def courses_records(courses_page):
    """{source: [record]} parsed from every saved catalogue fixture."""
    from api_crawler.jobs.courses.parser import CoursesParser

    parser = CoursesParser()
    return {
        source: list(parser.parse(courses_page(source)))
        for source in ("mslearn", "mitlearn", "freecodecamp")
    }
