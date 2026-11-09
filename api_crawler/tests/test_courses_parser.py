"""Parse saved course-catalogue payloads and assert the records are correct.

Four dialects reach one table, so most of what can go wrong is a field landing
in the wrong shape rather than a selector missing. Avro validation catches the
shapes; the assertions below catch the things a valid record can still get
wrong — a tracking parameter left on a url, a level invented out of a title,
an identity that is not stable.
"""

import hashlib

import pytest
from conftest import COURSES_SOURCES as SOURCES

from sentpul.validator import validate_record


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
    """Only Microsoft publishes a level. The others must stay UNKNOWN — a wrong
    level puts a course at the wrong step of a path."""
    for source in ("mitlearn", "freecodecamp", "csvideo"):
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


# ── csvideo: a markdown bookmark list, not an API ─────────────────────────


def test_csvideo_skips_the_table_of_contents(courses_records):
    """Its bullets are shaped exactly like course bullets. They are excluded by
    pointing at #anchors, not by a rule naming the contents list."""
    for r in courses_records["csvideo"]:
        assert r["url"].startswith(("http://", "https://")), r["url"]
        assert "#" not in r["url"].split("://", 1)[1].split("/", 1)[0]
    titles = {r["title"] for r in courses_records["csvideo"]}
    assert "Data Structures and Algorithms" not in titles


def test_csvideo_takes_the_title_outside_the_link_when_there_is_one(courses_records):
    """`- <title> | [Video Playlist](url)` — the link text is a label, so the
    leading text is the course's identity."""
    by_url = {r["url"]: r for r in courses_records["csvideo"]}
    rec = by_url["https://www.youtube.com/playlist?list=PLGUxZeQTpJENZFOzGj-xUHOPuwpcCuIot"]
    assert rec["title"] == "COMP6991 - Rust Programming - UNSW (2022)"


def test_csvideo_keeps_a_title_that_holds_its_own_brackets(courses_records):
    """"[Python], UC Berkeley" must not be cut at the inner bracket."""
    titles = {r["title"] for r in courses_records["csvideo"]}
    assert any("[Python], UC Berkeley" in t for t in titles), titles


def test_csvideo_keeps_the_youtube_query_string(courses_records):
    """674 of these urls are youtube.com/playlist?list=..., where the parameter
    IS the resource — the opposite of mslearn's tracking parameter."""
    playlists = [r for r in courses_records["csvideo"] if "youtube.com/playlist" in r["url"]]
    assert playlists
    for r in playlists:
        assert "?list=" in r["url"]


def test_csvideo_takes_the_first_link_not_a_parenthesised_extra(courses_records):
    """`- [CS 50 - ...](https://cs50.harvard.edu/x/) ([cs50.tv](http://cs50.tv/...))`
    is one course at the first url, not at the aside."""
    urls = {r["url"] for r in courses_records["csvideo"]}
    assert "https://cs50.harvard.edu/x/" in urls
    assert "http://cs50.tv/2017/fall/" not in urls


def test_csvideo_yields_one_row_per_url_carrying_both_sections(courses_records):
    """The url is the identity, so a course listed under two headings is one
    course with two tags — not the same course twice under two ids."""
    dupes = [r for r in courses_records["csvideo"] if r["url"] == "https://cs61a.org/"]
    assert len(dupes) == 1
    assert dupes[0]["tags"] == [
        "Introduction to Computer Science",
        "Data Structures and Algorithms",
    ]


def test_csvideo_provider_is_the_host_not_the_list(courses_records):
    """A bookmark list is not a provider. The host is the one provider fact an
    entry carries; the university in the title is not parsed out."""
    providers = {r["provider"] for r in courses_records["csvideo"]}
    assert "csvideo" not in providers
    assert "cs-video-courses" not in providers
    assert "ocw.mit.edu" in providers
    for p in providers:
        assert not p.startswith("www."), p


def test_csvideo_records_no_hierarchy_and_no_duration(courses_records):
    """The list publishes neither, so neither is invented."""
    for r in courses_records["csvideo"]:
        assert r["content_kind"] == "COURSE"
        assert r["child_uids"] == [] and r["parent_uids"] == []
        assert r["duration_minutes"] is None and r["duration_text"] is None
        assert r["duration_is_derived"] is False
        assert r["summary"] is None


def test_csvideo_drops_a_bullet_with_no_usable_link(courses_records):
    """One fixture bullet has no link and one points at a relative path."""
    titles = {r["title"] for r in courses_records["csvideo"]}
    assert "A course whose link the curator has not added yet" not in titles
    assert "A course linked by a repository-relative path" not in titles
