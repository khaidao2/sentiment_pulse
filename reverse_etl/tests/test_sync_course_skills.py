"""Course -> skill edges land as rows, and only where the title earned them.

The hazards this file guards, in order of how much they would cost:

  * a course whose title names no skill must contribute NOTHING — not even the
    skills its summary mentions. Guessing is how a catalogue of 7,205 courses
    starts claiming to teach everything;
  * `teaches` and `mentions` are different claims and must stay different rows.
    A path-builder reads `teaches`; nothing should be able to read a mention as
    an endorsement;
  * the run is a full rewrite, so it has to be idempotent AND has to delete the
    edges of a course that stopped matching;
  * the provider's `summary` is scanned and never republished.

main() is driven against a recording cursor and a stub ClickHouse read, the same
shape test_sync_jobs.py uses, so the SQL is asserted without a database.
"""

import sys

import pytest

import sync_course_skills
from courses import match, overrides as overrides_mod


# Three courses that cover the matrix: a clean title match, a mentions-only
# course that must be dropped, and a LEARNING_PATH whose own title says nothing
# while the MODULE under it teaches Kubernetes.
KUBE = {
    "id": "c-kube", "source": "mslearn", "content_kind": "MODULE",
    "title": "Introduction to Kubernetes",
    "url": "https://learn/modules/intro-kubernetes/",
    "summary": "The course also uses Docker.", "tags": ["Azure"],
}
CIRCUITS = {
    "id": "c-circuits", "source": "mitlearn", "content_kind": "COURSE",
    "title": "Circuits and Electronics",
    "url": "https://ocw/6-002/",
    "summary": "Problem sets are written in Python.", "tags": [],
}
PATH = {
    "id": "c-path", "source": "mslearn", "content_kind": "LEARNING_PATH",
    "title": "Become a data engineer",
    "url": "https://learn/paths/data-engineer/",
    "summary": "", "tags": [],
}
ROWS = [KUBE, CIRCUITS, PATH]


class _Cursor:
    """Records every statement; answers the three reads main() makes."""

    def __init__(self, known_courses=("c-kube", "c-circuits", "c-path"), has_table=True):
        self.calls = []
        self.known = list(known_courses)
        self.has_table = has_table
        self._next_id = 0
        self._last = ""

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self._last = sql

    def fetchone(self):
        if "to_regclass" in self._last:
            return ("course_skills" if self.has_table else None,)
        self._next_id += 1
        return (self._next_id,)

    def fetchall(self):
        return [(c,) for c in self.known]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def run(monkeypatch):
    """Run main() against the stubs; hand back the recorded statements."""

    def _run(rows=ROWS, argv=("--apply",), overrides=None, **cursor_kw):
        cur = _Cursor(**cursor_kw)
        commits = []
        pg = type("PG", (), {
            "cursor": lambda self: cur,
            "commit": lambda self: commits.append(1),
        })()
        monkeypatch.setattr(sync_course_skills.psycopg, "connect", lambda dsn: pg)
        monkeypatch.setattr(match, "client", lambda: object())
        monkeypatch.setattr(match, "fetch", lambda ch: list(rows))
        monkeypatch.setattr(
            overrides_mod, "load",
            lambda path=None: overrides or overrides_mod.Overrides({}, {}),
        )
        monkeypatch.setenv("POSTGRES_DSN", "postgresql:///stub")
        monkeypatch.setattr(sys, "argv", ["sync_course_skills.py", *argv])
        sync_course_skills.main()
        return cur, commits

    return _run


def _edges(calls):
    """(course_id, skill_id, relation) for every course_skills INSERT."""
    return [p for sql, p in calls if sql.startswith("INSERT INTO course_skills")]


def _skill_names(calls):
    return [p[0] for sql, p in calls if sql.startswith("INSERT INTO skills (name)")]


def _skill_id_by_name(calls):
    """Replay the RETURNING ids the stub cursor hands back, in order."""
    return {name: i + 1 for i, name in enumerate(_skill_names(calls))}


# 1 — a course whose TITLE names no skill contributes no edges at all. Not its
#     mentions either: "written in Python" is not a Python course.
def test_course_with_no_title_match_is_dropped_not_guessed(run):
    cur, _ = run()
    ids = {e[0] for e in _edges(cur.calls)}
    assert "c-circuits" not in ids
    assert "Python" not in _skill_names(cur.calls)


# 2 — a LEARNING_PATH does not inherit the skills of what sits under it. The
#     path here is dropped while the Kubernetes module beside it is kept.
def test_learning_path_does_not_inherit_its_children(run):
    cur, _ = run()
    ids = {e[0] for e in _edges(cur.calls)}
    assert "c-path" not in ids
    assert "c-kube" in ids


# 3 — the two relations are different rows, and no skill is ever written under
#     both for one course.
def test_teaches_and_mentions_stay_distinct(run):
    cur, _ = run()
    by_id = _skill_id_by_name(cur.calls)
    edges = _edges(cur.calls)
    teaches = {(c, s) for c, s, rel in edges if rel == "teaches"}
    mentions = {(c, s) for c, s, rel in edges if rel == "mentions"}
    assert ("c-kube", by_id["Kubernetes"]) in teaches
    assert ("c-kube", by_id["Docker"]) in mentions
    assert ("c-kube", by_id["Azure"]) in mentions
    assert not teaches & mentions


# 4 — every processed course is cleared before anything is inserted, including
#     the ones that matched nothing. That DELETE is what removes the edges of a
#     course that stopped matching; an UPSERT alone would keep them for ever.
def test_every_processed_course_is_cleared_before_insert(run):
    cur, _ = run()
    sqls = [sql for sql, _ in cur.calls]
    delete_at = sqls.index("DELETE FROM course_skills WHERE course_id = ANY(%s)")
    first_insert = next(i for i, s in enumerate(sqls) if s.startswith("INSERT INTO course_skills"))
    assert delete_at < first_insert
    _, params = cur.calls[delete_at]
    assert set(params[0]) == {"c-kube", "c-circuits", "c-path"}


# 5 — re-running changes nothing: the same statements, in the same order, with
#     the same parameters. The result is a pure function of the catalogue, the
#     dictionary and the overrides.
def test_rerun_is_idempotent(run):
    first, _ = run()
    second, _ = run()
    assert first.calls == second.calls


# 6 — skills go into the lookup table sync_jobs.py already maintains, so a
#     course teaching Kubernetes and a posting asking for it share one row.
def test_skills_land_in_the_shared_lookup_table(run):
    cur, _ = run()
    assert "Kubernetes" in _skill_names(cur.calls)
    assert all(
        sql.startswith("INSERT INTO skills (name)")
        for sql, _ in cur.calls
        if "INSERT INTO skills" in sql
    )


# 7 — an edge is never written for a course the catalogue sync has not landed:
#     it would break the target's foreign key and abort the transaction.
def test_edges_are_skipped_for_courses_not_in_the_target(run):
    cur, _ = run(known_courses=("c-circuits",))
    assert _edges(cur.calls) == []


# 8 — the provider's prose is scanned and never leaves. Only skill NAMES do.
def test_summary_is_never_written_anywhere(run):
    cur, _ = run()
    blob = repr(cur.calls)
    assert "Problem sets" not in blob
    assert "also uses Docker" not in blob


# 9 — dry run is the default and writes nothing.
def test_dry_run_writes_nothing(run):
    cur, commits = run(argv=())
    assert _edges(cur.calls) == []
    assert _skill_names(cur.calls) == []
    assert commits == []


# 10 — without the target table the job stops before writing, loudly.
def test_missing_target_table_fails_loudly(run):
    with pytest.raises(SystemExit) as exc:
        run(has_table=False)
    assert "course_skills" in str(exc.value)


# ── the curation gate ────────────────────────────────────────────────────────

# 11 — deny removes one course's bad edge and nothing else.
def test_deny_removes_the_edge_for_that_course_only(run):
    ov = overrides_mod.Overrides({KUBE["url"]: {"Kubernetes"}}, {})
    cur, _ = run(overrides=ov)
    by_id = _skill_id_by_name(cur.calls)
    edges = _edges(cur.calls)
    assert ("c-kube", by_id.get("Kubernetes"), "teaches") not in edges
    # Kubernetes was this course's ONLY title match, so denying it leaves no
    # `teaches` and the course is dropped whole — its mentions go with it,
    # exactly as for a course that never matched.
    assert {e[0] for e in edges} == set()
    assert not {"Kubernetes", "Docker", "Azure"} & set(_skill_names(cur.calls))


# 12 — a curated `teaches` rescues a dropped course and takes the skill out of
#      mentions, so the two relations stay exclusive.
def test_curated_teaches_rescues_a_dropped_course(run):
    ov = overrides_mod.Overrides({}, {CIRCUITS["url"]: {"Python"}})
    cur, _ = run(overrides=ov)
    by_id = _skill_id_by_name(cur.calls)
    edges = _edges(cur.calls)
    assert ("c-circuits", by_id["Python"], "teaches") in edges
    assert ("c-circuits", by_id["Python"], "mentions") not in edges


# 13 — the curation file cannot grow a vocabulary the dictionary has not got.
def test_override_naming_an_unknown_skill_is_an_error(tmp_path):
    path = tmp_path / "overrides.yaml"
    path.write_text("teaches:\n  - url: https://x\n    skills: [Totally Made Up Thing]\n")
    with pytest.raises(ValueError) as exc:
        overrides_mod.load(path)
    assert "Totally Made Up Thing" in str(exc.value)


# 14 — the file that actually ships parses, and every skill in it is canonical.
def test_shipped_overrides_file_is_valid():
    ov = overrides_mod.load()
    from skills.dictionary import ALL_SKILLS
    for mapping in (ov.deny, ov.teaches):
        for skills in mapping.values():
            assert not set(skills) - set(ALL_SKILLS)


# ── tag_map cannot smuggle a course past the title gate ──────────────────────

# 14 — a course whose ONLY skill evidence comes from the provider's tags still
#      contributes nothing. tag_map (added 2026-11-02) resolves tags that the
#      exact-match extractor never could, which means it can hand `mentions` to
#      a course whose title names no skill at all. Those courses must stay
#      dropped: the whole point of the title gate is that a tag is not a claim
#      about what a course TEACHES.
#
#      This is not hypothetical. Reading tag_map's effect through match_course()
#      instead of build_edges() suggests it adds 615 edges over 311 extra
#      courses; through the real pipeline it adds 127, over zero extra courses,
#      because every one of those 311 is dropped right here. The gate is what
#      makes those two numbers differ, so it gets its own test.
def test_a_course_matched_only_by_its_tags_is_still_dropped(run):
    tagged_only = {
        "id": "c-tagged", "source": "mslearn", "content_kind": "MODULE",
        # Title names no canonical skill; `azure virtual machines` is a tag
        # tag_map maps to Azure, and it must not rescue the course.
        "title": "Plan and prepare your deployment",
        "url": "https://learn/modules/plan-prepare/",
        "summary": "", "tags": ["azure virtual machines"],
    }
    cur, _ = run(rows=[tagged_only], known_courses=("c-tagged",))
    edges = _edges(cur.calls)
    assert edges == [], f"a tag-only course must contribute no edges, got {edges}"


# 15 — and the same tag DOES reach `mentions` once the title earns a `teaches`.
#      Together with 14 this pins the rule exactly: tags enrich a kept course,
#      they never keep one.
def test_a_tag_enriches_a_course_the_title_already_kept(run):
    kept = {
        "id": "c-kept", "source": "mslearn", "content_kind": "MODULE",
        "title": "Introduction to Kubernetes",
        "url": "https://learn/modules/intro-k8s/",
        "summary": "", "tags": ["azure virtual machines"],
    }
    cur, _ = run(rows=[kept], known_courses=("c-kept",))
    by_id = _skill_id_by_name(cur.calls)
    edges = _edges(cur.calls)
    assert ("c-kept", by_id["Kubernetes"], "teaches") in edges
    assert ("c-kept", by_id["Azure"], "mentions") in edges
