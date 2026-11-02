"""Course hierarchy and duration: the facts the sources publish, kept intact.

Before this, `parser._mslearn` iterated ("modules", "learningPaths") into one
flat shape and dropped the payload's own `type`, so a 102-minute learning path
and the 65-minute module inside it became indistinguishable rows -- and any
"shortest first" ranking would put modules above the paths containing them.
The ordered `modules`/`units` uid lists, the uids themselves and
first-child urls were discarded with it, which threw away the only curriculum
sequence any source gives us.

Duration had a second problem: the schema claimed "MIT and freeCodeCamp do not
publish duration" and MIT plainly does, in weeks plus a weekly commitment. The
int-minutes field could not hold it, so all three values were dropped.

These tests pin both behaviours, plus the two properties that are easy to
regress into fabrication: an unresolved child must produce NO parent edge, and
weeks must never be silently converted into minutes.

The committed fixtures cannot exercise parent resolution -- the fixture's path
lists five `learn.fsharp-*` modules and the fixture's one module is an
unrelated Azure ML module -- so the resolution tests build small synthetic
catalogues in the real payload shape.
"""

import json
from pathlib import Path

import pytest

from api_crawler.jobs.courses.parser import CoursesParser

_FIXTURES = Path(__file__).parent / "fixtures"


def _parse(source: str, page) -> list[dict]:
    raw = json.dumps({"source": source, "page": json.dumps(page)})
    return list(CoursesParser().parse(raw))


def _fixture(source: str) -> list[dict]:
    page = (_FIXTURES / f"courses_{source}.json").read_text(encoding="utf-8")
    return list(CoursesParser().parse(json.dumps({"source": source, "page": page})))


def _ms(modules=(), paths=()) -> list[dict]:
    return _parse("mslearn", {"modules": list(modules), "learningPaths": list(paths)})


def _module(uid, *, units=(), title="M", n=None):
    return {"uid": uid, "type": "module", "title": title,
            "url": f"https://learn.microsoft.com/training/modules/{uid}/",
            "units": list(units), "number_of_children": len(units) if n is None else n,
            "firstUnitUrl": f"https://learn.microsoft.com/training/modules/{uid}/1-a/",
            "levels": ["beginner"], "duration_in_minutes": 30}


def _path(uid, *, modules=(), title="P", n=None):
    return {"uid": uid, "type": "learningPath", "title": title,
            "url": f"https://learn.microsoft.com/training/paths/{uid}/",
            "modules": list(modules), "number_of_children": len(modules) if n is None else n,
            "firstModuleUrl": f"https://learn.microsoft.com/training/modules/{uid}-first/",
            "levels": ["beginner"], "duration_in_minutes": 120}


# ── learning path vs module ───────────────────────────────────────────────


def test_fixture_keeps_path_and_module_distinct():
    by_kind = {r["content_kind"] for r in _fixture("mslearn")}
    assert by_kind == {"MODULE", "LEARNING_PATH"}


def test_content_kind_is_read_from_the_payload_not_guessed():
    """Microsoft publishes `type`; an unknown value must not become a guess."""
    recs = _ms(modules=[{"uid": "x", "type": "somethingNew", "title": "T",
                         "url": "https://learn.microsoft.com/training/modules/x/"}])
    assert recs[0]["content_kind"] == "UNKNOWN"


def test_standalone_sources_are_courses_not_containers():
    for source in ("mitlearn", "freecodecamp"):
        for r in _fixture(source):
            assert r["content_kind"] == "COURSE"
            assert r["child_uids"] == []
            assert r["parent_uids"] == []


# ── ordering and identity ─────────────────────────────────────────────────


def test_child_order_is_preserved_verbatim():
    """Position is the curriculum. Sorting would destroy the sequence."""
    order = ["b.second", "a.first", "c.third"]
    recs = _ms(paths=[_path("p", modules=order)])
    assert recs[0]["child_uids"] == order, "children must not be reordered"


def test_unit_order_is_preserved_on_modules():
    units = ["m.intro", "m.deep", "m.summary"]
    recs = _ms(modules=[_module("m", units=units)])
    assert recs[0]["child_uids"] == units


def test_source_uid_is_preserved():
    uids = {r["uid"] for r in _fixture("mslearn")}
    assert uids == {"learn.wwl.experiment-azure-machine-learning",
                    "learn.languages.fsharp-first-steps"}


def test_uid_is_null_where_the_source_publishes_none():
    for source in ("mitlearn", "freecodecamp"):
        assert all(r["uid"] is None for r in _fixture(source))


def test_first_child_url_is_captured_and_detracked():
    recs = _ms(modules=[dict(_module("m", units=["m.a"]),
                             firstUnitUrl="https://x/1-a/?WT.mc_id=api_CatalogApi")])
    assert recs[0]["first_child_url"] == "https://x/1-a/"


# ── parent resolution: real edges only ────────────────────────────────────


def test_parent_is_resolved_when_the_child_is_in_the_crawl():
    recs = _ms(modules=[_module("m.one"), _module("m.two")],
               paths=[_path("p.main", modules=["m.one", "m.two"])])
    by_uid = {r["uid"]: r for r in recs}
    assert by_uid["m.one"]["parent_uids"] == ["p.main"]
    assert by_uid["m.two"]["parent_uids"] == ["p.main"]
    assert by_uid["p.main"]["parent_uids"] == [], "a path has no parent here"


def test_unresolved_child_produces_no_parent_edge():
    """The catalogue is paged and capped, so paths routinely list modules that
    were never fetched. Inventing an edge would fabricate a curriculum."""
    recs = _ms(modules=[_module("m.present")],
               paths=[_path("p", modules=["m.present", "m.never-crawled"])])
    by_uid = {r["uid"]: r for r in recs}
    assert by_uid["m.present"]["parent_uids"] == ["p"]
    assert "m.never-crawled" not in by_uid, "absent child must not be invented as a row"


def test_fixture_hierarchy_is_incomplete_and_says_so():
    """The committed fixture's path names modules that are not in the crawl."""
    recs = _fixture("mslearn")
    path = next(r for r in recs if r["content_kind"] == "LEARNING_PATH")
    module = next(r for r in recs if r["content_kind"] == "MODULE")
    assert path["child_uids"], "path must still record the ordering it published"
    assert module["parent_uids"] == [], "no parent was crawled, so no edge"


def test_a_module_can_belong_to_several_paths():
    """Why step index is not a column: position differs per parent."""
    recs = _ms(modules=[_module("m.shared")],
               paths=[_path("p.one", modules=["m.shared"]),
                      _path("p.two", modules=["x.other", "m.shared"])])
    shared = next(r for r in recs if r["uid"] == "m.shared")
    assert sorted(shared["parent_uids"]) == ["p.one", "p.two"]
    # Its step differs between parents: index 0 in p.one, index 1 in p.two.
    by_uid = {r["uid"]: r for r in recs}
    assert by_uid["p.one"]["child_uids"].index("m.shared") == 0
    assert by_uid["p.two"]["child_uids"].index("m.shared") == 1


def test_child_count_exposes_a_truncated_hierarchy():
    """child_count vs len(child_uids) is the incompleteness signal."""
    recs = _ms(paths=[_path("p", modules=["a", "b"], n=7)])
    r = recs[0]
    assert r["child_count"] == 7 and len(r["child_uids"]) == 2
    assert r["child_count"] > len(r["child_uids"])


# ── duration ──────────────────────────────────────────────────────────────


def test_mit_weeks_and_weekly_commitment_are_kept():
    """The schema used to claim MIT publishes no duration. It does."""
    recs = _fixture("mitlearn")
    assert [(r["duration_text"], r["duration_weeks_min"], r["weekly_hours_min"])
            for r in recs] == [("6 weeks", 6, 5), ("9 weeks", 9, 10)]


def test_weeks_are_never_converted_to_minutes():
    """6 weeks x 5 h/week is only effort under a study rate MIT never states."""
    for r in _fixture("mitlearn"):
        assert r["duration_minutes"] is None


def test_microsoft_minutes_are_source_provided():
    mins = {r["content_kind"]: r["duration_minutes"] for r in _fixture("mslearn")}
    assert mins == {"MODULE": 65, "LEARNING_PATH": 102}


def test_missing_duration_stays_unknown():
    for r in _fixture("freecodecamp"):
        assert r["duration_minutes"] is None
        assert r["duration_text"] is None
        assert r["duration_weeks_min"] is None
        assert r["weekly_hours_min"] is None


def test_no_duration_is_derived_anywhere():
    """Nothing estimates today; the flag exists so a future estimate cannot be
    mistaken for a published value."""
    for source in ("mslearn", "mitlearn", "freecodecamp"):
        assert all(r["duration_is_derived"] is False for r in _fixture(source))


def test_duration_is_not_comparable_across_content_kinds():
    """A path CONTAINS its modules, so 102 < 65 is meaningless between kinds.

    This is a property of the data, not of a ranker: it is asserted here so
    that any future ranking code has a named reason to scope comparisons to a
    single content_kind.
    """
    recs = _fixture("mslearn")
    path = next(r for r in recs if r["content_kind"] == "LEARNING_PATH")
    module = next(r for r in recs if r["content_kind"] == "MODULE")
    assert path["duration_minutes"] > module["duration_minutes"]
    assert path["content_kind"] != module["content_kind"]


# ── the Avro contract still holds ─────────────────────────────────────────


def test_every_record_round_trips_through_the_avro_schema():
    """Nullable/default semantics, the enum symbols and array types all have to
    be right or the sink's Parquet write fails at runtime, not here."""
    fastavro = pytest.importorskip("fastavro")
    schema_path = (Path(__file__).resolve().parents[2]
                   / "data-contracts" / "schemas" / "learning" / "courses.avsc")
    schema = fastavro.parse_schema(json.loads(schema_path.read_text(encoding="utf-8")))
    import io
    for source in ("mslearn", "mitlearn", "freecodecamp"):
        recs = _fixture(source)
        buf = io.BytesIO()
        fastavro.writer(buf, schema, recs)          # raises on any bad field
        buf.seek(0)
        back = list(fastavro.reader(buf))
        assert len(back) == len(recs)
        for before, after in zip(recs, back):
            assert after["content_kind"] == before["content_kind"]
            assert after["child_uids"] == before["child_uids"], "order must survive"
            assert after["uid"] == before["uid"]
            assert after["duration_weeks_min"] == before["duration_weeks_min"]
