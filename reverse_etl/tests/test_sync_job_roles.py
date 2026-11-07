"""Role classifications land as rows, with the evidence that produced them.

The hazards this file guards:

  * UNKNOWN or AMBIGUOUS quietly becoming a family, or quietly not being
    written at all. Either turns 44.3% of the corpus into a confident number;
  * the stored title drifting from the crawled one. The classifier matches on an
    NFKC-casefolded key, and if that key reaches storage the audit trail is
    gone;
  * a re-run appending instead of reconciling, so a rules change leaves two
    generations of rows side by side;
  * the rule version or the matched rules going unstored, which makes a row
    impossible to tie back to the rules that produced it;
  * the rules drifting without anyone noticing, which silently moves every
    demand profile downstream.

main() is driven against a recording cursor, the same shape
test_sync_course_skills.py uses, so the SQL is asserted without a database.
"""

import sys

import pytest
import sync_job_roles
from sync_job_roles import decode_rule, encode_rule, plan

from roles.classify import AMBIGUOUS, METHOD, QUALIFIER, ROLE, UNKNOWN

# Real titles from the 2026-11-02 corpus, one per outcome.
JOBS = [
    ("j-data", "Senior Data Engineer"),                  # clean, English
    ("j-viet", "CHUYÊN VIÊN PHÂN TÍCH DỮ LIỆU"),         # clean, Vietnamese
    ("j-qual", "Java Developer"),                        # qualifier only
    ("j-amb",  "Fullstack Developer (Back-end)"),        # two role words
    ("j-unk",  "Chuyên viên kinh doanh"),                # matches nothing
]


class _Cursor:
    """Records every statement; answers the two reads main() makes."""

    def __init__(self, has_table=True, jobs=JOBS):
        self.calls = []
        self.has_table = has_table
        self.jobs = list(jobs)
        self._last = ""

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self._last = sql
        return self

    def fetchone(self):
        if "to_regclass" in self._last:
            return ("job_role_family" if self.has_table else None,)
        return None

    def fetchall(self):
        return list(self.jobs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def run(monkeypatch):
    def _run(argv=("--apply",), **cursor_kw):
        cur = _Cursor(**cursor_kw)
        commits = []
        pg = type("PG", (), {
            "cursor": lambda self: cur,
            "commit": lambda self: commits.append(1),
        })()
        monkeypatch.setattr(sync_job_roles.psycopg, "connect", lambda dsn: pg)
        monkeypatch.setenv("POSTGRES_DSN", "postgresql:///stub")
        monkeypatch.setattr(sys, "argv", ["sync_job_roles.py", *argv])
        sync_job_roles.main()
        return cur, commits
    return _run


def _inserts(calls):
    return [p for sql, p in calls if sql.startswith("INSERT INTO job_role_family")]


def _by_job(calls):
    return {p[0]: p for p in _inserts(calls)}


# ── every job gets a row, including the ones that matched nothing ────────────


def test_every_job_is_written_not_just_the_classified_ones(run):
    cur, _ = run()
    assert len(_inserts(cur.calls)) == len(JOBS)


def test_unknown_is_stored_as_itself_with_no_evidence(run):
    cur, _ = run()
    row = _by_job(cur.calls)["j-unk"]
    assert row[1] == UNKNOWN
    assert row[4] == [], "a title that matched nothing cannot carry evidence"


def test_ambiguous_is_stored_as_itself_and_keeps_both_rules(run):
    cur, _ = run()
    row = _by_job(cur.calls)["j-amb"]
    assert row[1] == AMBIGUOUS
    assert len(row[4]) >= 2, "AMBIGUOUS means two families matched; keep both"


def test_no_planned_row_can_violate_the_check_constraints():
    """The DB enforces these too; this catches it before the transaction."""
    for _id, family, _sen, _m, rules, _t in plan(JOBS):
        if family == UNKNOWN:
            assert len(rules) == 0
        if family == AMBIGUOUS:
            assert len(rules) >= 2


# ── provenance: version and rules, including the losing family's ─────────────


def test_every_row_stores_the_rule_version(run):
    cur, _ = run()
    assert {r[3] for r in _inserts(cur.calls)} == {METHOD}


def test_matched_rules_round_trip_and_carry_their_tier(run):
    cur, _ = run()
    rules = [decode_rule(x) for x in _by_job(cur.calls)["j-qual"][4]]
    assert rules == [("Backend", "java developer", QUALIFIER)]


def test_the_losing_familys_rule_is_kept_for_audit():
    """"Why Fullstack and not Backend" is only answerable if Backend is stored."""
    (_id, family, _s, _m, rules, _t), = plan([("j", "Senior Fullstack PHP Developer")])
    assert family == "Fullstack"
    decoded = {decode_rule(r) for r in rules}
    assert ("Backend", "php developer", QUALIFIER) in decoded
    assert ("Fullstack", "fullstack", ROLE) in decoded


def test_encode_decode_is_lossless_for_patterns_with_punctuation():
    for f, p, t in [("Backend", "node.js developer", QUALIFIER),
                    ("Backend", "back-end", ROLE)]:
        assert decode_rule(encode_rule(f, p, t)) == (f, p, t)


# ── the stored title is the crawled one ──────────────────────────────────────


def test_the_title_is_stored_exactly_as_crawled(run):
    cur, _ = run()
    stored = {p[0]: p[5] for p in _inserts(cur.calls)}
    assert stored["j-viet"] == "CHUYÊN VIÊN PHÂN TÍCH DỮ LIỆU", \
        "the normalised matching key must never reach storage"
    assert stored == dict(JOBS)


# ── seniority stays independent ──────────────────────────────────────────────


def test_seniority_is_stored_separately_and_may_be_null(run):
    cur, _ = run()
    by = _by_job(cur.calls)
    assert by["j-data"][2] == "senior"
    assert by["j-unk"][2] is None, "no marker must stay distinguishable from a level"
    assert "senior" not in by["j-data"][1], "seniority must not leak into family"


# ── idempotence ──────────────────────────────────────────────────────────────


def test_the_write_upserts_on_the_primary_key(run):
    cur, _ = run()
    sql = [s for s, _ in cur.calls if s.startswith("INSERT INTO job_role_family")][0]
    assert "ON CONFLICT (job_id) DO UPDATE SET" in sql
    for col in ("family", "seniority", "method", "matched_rules", "title"):
        assert f"{col} = EXCLUDED.{col}" in sql, f"{col} would go stale on a re-run"
    assert "classified_at = now()" in sql


def test_two_runs_plan_identical_rows():
    assert plan(JOBS) == plan(JOBS)


def test_a_rerun_writes_the_same_statements(run):
    first, _ = run()
    second, _ = run()
    assert _inserts(first.calls) == _inserts(second.calls)


# ── guards ───────────────────────────────────────────────────────────────────


def test_a_missing_target_table_fails_loudly_before_writing(run):
    with pytest.raises(SystemExit) as exc:
        run(has_table=False)
    assert "job_role_family does not exist" in str(exc.value)


def test_a_dry_run_writes_nothing(run):
    cur, commits = run(argv=())
    assert _inserts(cur.calls) == []
    assert commits == []


def test_an_empty_jobs_table_is_a_no_op_not_a_crash(run):
    cur, commits = run(jobs=[])
    assert _inserts(cur.calls) == []
    assert commits == []


# ── regression against the approved rules:v2 report ──────────────────────────


def test_the_rule_version_is_the_approved_one():
    assert METHOD == "rules:v2", \
        "rules changed without bumping METHOD; stored rows would be untraceable"


@pytest.mark.parametrize("title,family", [
    # One per qualifying family, taken from the approved rules:v2 corpus. If a
    # rule edit moves any of these, the demand profiles move with it.
    ("Senior DevOps Engineer",              "DevOps"),
    ("Data Engineer",                       "Data"),
    ("Senior IT Business Analyst",          "Business Analysis"),
    ("Java Developer",                      "Backend"),
    ("Software Development Engineer in Test (SDET)", "QA"),
    ("AI Engineer",                         "ML/AI"),
    ("Senior Fullstack Engineer",           "Fullstack"),
    ("Product Owner",                       "Product/Project"),
])
def test_approved_family_assignments_do_not_drift(title, family):
    (_id, got, _s, _m, _r, _t), = plan([("j", title)])
    assert got == family
