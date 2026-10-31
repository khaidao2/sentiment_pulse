"""Parse saved course-catalogue payloads and assert the records are correct.

Three dialects reach one table, so most of what can go wrong is a field landing
in the wrong shape rather than a selector missing. Avro validation catches the
shapes; the assertions below catch the things a valid record can still get
wrong — a tracking parameter left on a url, a level invented out of a title,
an identity that is not stable.
"""

import hashlib

import pytest

from sentpul.validator import validate_record

SOURCES = ("mslearn", "mitlearn", "freecodecamp")


def test_every_source_yields_records(courses_records):
    for source in SOURCES:
        assert courses_records[source], source


@pytest.mark.parametrize("source", SOURCES)
def test_records_match_avro_schema(courses_records, courses_schema, source):
    for record in courses_records[source]:
        validate_record(record, courses_schema)  # raises on invalid shape


def test_id_is_the_sha256_of_the_url(courses_records):
    """The url IS the identity: providers reuse titles across levels and
    locales, and ReplacingMergeTree orders by this id."""
    for records in courses_records.values():
        for r in records:
            assert r["id"] == hashlib.sha256(r["url"].encode()).hexdigest()


def test_ids_are_unique_within_a_page(courses_records):
    for source, records in courses_records.items():
        ids = [r["id"] for r in records]
        assert len(ids) == len(set(ids)), source


def test_microsoft_tracking_parameters_are_stripped(courses_records):
    """The catalogue appends ?WT.mc_id=api_CatalogApi — a tracking parameter,
    not part of the resource, and it would fork the identity of every row."""
    for r in courses_records["mslearn"]:
        assert "?" not in r["url"]
        assert "WT.mc_id" not in r["url"]


def test_microsoft_levels_map_to_the_schema_bands(courses_records):
    levels = {r["level"] for r in courses_records["mslearn"]}
    assert levels <= {"INTRO", "WORKING", "DEEP", "UNKNOWN"}
    assert levels != {"UNKNOWN"}, "the fixture has levels; none were mapped"


def test_level_is_never_guessed(courses_records):
    """Only Microsoft publishes a level. MIT and freeCodeCamp must stay
    UNKNOWN — a wrong level puts a course at the wrong step of a path."""
    for source in ("mitlearn", "freecodecamp"):
        assert {r["level"] for r in courses_records[source]} == {"UNKNOWN"}, source


def test_provider_comes_from_the_payload_not_the_source(courses_records):
    """MIT Learn's one API serves OCW, MITx and xPRO."""
    providers = {r["provider"] for r in courses_records["mitlearn"]}
    assert providers and "mitlearn" not in providers


def test_tags_are_words_not_slugs(courses_records):
    """The downstream extractor scans words, so 'azure-machine-learning' would
    never match anything."""
    for r in courses_records["mslearn"]:
        for tag in r["tags"]:
            assert "-" not in tag


def test_summary_is_carried_for_downstream_matching(courses_records):
    """The matcher needs text to scan; the public catalogue drops it later."""
    assert any(r["summary"] for r in courses_records["mslearn"])


def test_no_record_claims_a_skill(courses_records):
    """Skill matching belongs to reverse_etl, where the dictionary lives. A
    crawler that guessed would be a second place deciding what a skill is."""
    for records in courses_records.values():
        for r in records:
            assert "skills" not in r and "teaches" not in r


def test_freecodecamp_ignores_non_json_entries(courses_records):
    """The superblock listing also contains README.md."""
    for r in courses_records["freecodecamp"]:
        assert r["url"].startswith("https://www.freecodecamp.org/learn/")
        assert "readme" not in r["url"].lower()


def test_unknown_source_yields_nothing_instead_of_raising():
    """A new catalogue added to SOURCES without a parser must not kill the run."""
    import json

    from api_crawler.jobs.courses.parser import CoursesParser

    envelope = json.dumps({"source": "coursera", "page": "{}"})
    assert list(CoursesParser().parse(envelope)) == []
