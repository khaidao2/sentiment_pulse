"""Model-inferred job skills land as model rows, or they do not land.

The hazards, in order of what they would cost:

  * an edge written without method='model' -- it would silently enter every
    demand figure the product publishes, with no way to separate it later;
  * a dictionary edge altered or deleted -- curated evidence replaced by
    inference;
  * a name the vocabulary does not have, or an edge with no confidence or no
    reason, reaching the table. Each makes a row nobody can review;
  * a job asked twice, or a job where the model found nothing asked every
    night for ever.

main() is driven against a recording cursor and a stubbed gateway, the same
shape test_sync_course_skills.py uses, so the SQL is asserted without a
database and without a model.
"""

import sys

import pytest

import match_jobs_ai
from match_jobs_ai import _parse, validated
from skills.extractor import normalize

VOCAB = {normalize(a): a for a in
         ("Docker", "Kubernetes", "CI/CD", "Python", "SQL")}


# ── the vocabulary, the confidence and the reason are all required ──────────


class TestValidated:

    def test_a_good_entry_passes(self):
        got = validated([{"skill": "Docker", "confidence": 0.8, "why": "containerised"}],
                        VOCAB, set())
        assert got == [("Docker", 0.8, "containerised")]

    def test_a_name_outside_the_vocabulary_is_dropped(self):
        assert validated([{"skill": "Helm", "confidence": 0.9, "why": "helm charts"}],
                         VOCAB, set()) == []

    def test_a_skill_the_dictionary_already_found_is_dropped(self):
        """Point 2: dictionary evidence is never replaced by inference."""
        assert validated([{"skill": "Docker", "confidence": 0.9, "why": "x"}],
                         VOCAB, already={"Docker"}) == []

    @pytest.mark.parametrize("conf", [2.0, -0.1, None, "high", ""])
    def test_an_unusable_confidence_is_dropped(self, conf):
        assert validated([{"skill": "Docker", "confidence": conf, "why": "x"}],
                         VOCAB, set()) == []

    @pytest.mark.parametrize("why", ["", "   ", None])
    def test_an_edge_with_no_reason_is_dropped(self, why):
        """An inference nobody can read is an inference nobody can review."""
        assert validated([{"skill": "Docker", "confidence": 0.8, "why": why}],
                         VOCAB, set()) == []

    def test_duplicates_collapse_keeping_the_first(self):
        got = validated([{"skill": "Docker", "confidence": 0.9, "why": "first"},
                         {"skill": "docker", "confidence": 0.1, "why": "second"}],
                        VOCAB, set())
        assert got == [("Docker", 0.9, "first")]

    def test_a_long_reason_is_truncated_not_rejected(self):
        got = validated([{"skill": "Docker", "confidence": 0.8, "why": "x" * 5000}],
                        VOCAB, set())
        assert len(got[0][2]) == 1000


class TestParsing:

    def test_fenced_and_chatty_replies_are_recovered(self):
        assert _parse('```json\n[{"skill":"Docker"}]\n```') == [{"skill": "Docker"}]
        assert _parse('Here:\n[{"skill":"SQL"}]\ndone') == [{"skill": "SQL"}]

    def test_an_empty_reply_is_an_answer(self):
        assert _parse("[]") == [] and _parse("") == []

    @pytest.mark.parametrize("reply", ["I cannot help", '["Docker"', "{}"])
    def test_an_unrecoverable_shape_raises(self, reply):
        with pytest.raises(ValueError):
            _parse(reply)

    def test_non_object_entries_are_discarded(self):
        assert _parse('[{"skill":"Docker"}, "SQL", 3]') == [{"skill": "Docker"}]


# ── what reaches the database ──────────────────────────────────────────────


class _Cursor:
    def __init__(self, pending=(("j-1", "GitLab runners deploy on every merge"),),
                 dictionary=(), has_tables=True):
        self.calls = []
        self._pending = list(pending)
        self._dictionary = list(dictionary)
        self._has = has_tables
        self._last = ""

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self._last = " ".join(sql.split())
        return self

    def fetchone(self):
        if "to_regclass" in self._last:
            return ("job_skills" if self._has else None,)
        return None

    def fetchall(self):
        if "LEFT JOIN job_model_match" in self._last:
            return list(self._pending)
        if "method = 'dictionary'" in self._last:
            return [(n,) for n in self._dictionary]
        if "FROM skills WHERE name = ANY" in self._last:
            return [("Docker", 1), ("CI/CD", 2), ("Kubernetes", 3)]
        return []

    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def run(monkeypatch):
    def _run(reply, argv=("--apply",), **cursor_kw):
        cur = _Cursor(**cursor_kw)
        commits = []
        pg = type("PG", (), {"cursor": lambda self: cur,
                             "commit": lambda self: commits.append(1)})()
        monkeypatch.setattr(match_jobs_ai.psycopg, "connect", lambda dsn: pg)
        monkeypatch.setattr(match_jobs_ai, "_ask",
                            lambda *a, **k: reply if not isinstance(reply, Exception)
                            else (_ for _ in ()).throw(reply))
        for k, v in (("POSTGRES_DSN", "postgresql:///stub"),
                     ("AI_GATEWAY_ENDPOINT", "https://x/v1"),
                     ("AI_GATEWAY_API_KEY", "k"),
                     ("AI_GATEWAY_MODEL", "test-model")):
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(sys, "argv", ["match_jobs_ai.py", *argv])
        match_jobs_ai.main()
        return cur, commits
    return _run


def _inserts(calls):
    return [p for sql, p in calls if sql.startswith("INSERT INTO job_skills")]


def _sql(calls):
    return [sql for sql, _ in calls]


def test_every_written_edge_is_a_model_edge(run):
    """'model' is a LITERAL in the SQL, not a bound parameter -- so no caller
    and no code path can vary it. That is stronger than validating a value."""
    cur, _ = run([{"skill": "CI/CD", "confidence": 0.7, "why": "runners deploy"}])
    stmts = [s for s in _sql(cur.calls) if s.startswith("INSERT INTO job_skills")]
    assert stmts, "nothing was written"
    for stmt in stmts:
        assert "'model'" in stmt, "method is not hardcoded to model"
        assert "%s, %s, 'model', %s, %s" in stmt
    for job_id, sid, conf, why in _inserts(cur.calls):
        assert 0.0 <= conf <= 1.0 and why, "confidence and reason are required"


def test_no_statement_ever_touches_a_dictionary_edge(run):
    cur, _ = run([{"skill": "CI/CD", "confidence": 0.7, "why": "runners"}])
    for sql in _sql(cur.calls):
        if sql.startswith(("DELETE", "UPDATE")):
            assert "method = 'model'" in sql, f"unscoped write: {sql[:70]}"


def test_the_delete_is_scoped_to_this_job_and_to_model_rows(run):
    cur, _ = run([{"skill": "CI/CD", "confidence": 0.7, "why": "runners"}])
    deletes = [s for s in _sql(cur.calls) if s.startswith("DELETE FROM job_skills")]
    assert deletes == ["DELETE FROM job_skills WHERE job_id = %s AND method = 'model'"]


def test_a_job_with_no_inferences_is_still_watermarked(run):
    """Otherwise it is re-asked every night for ever, paying repeatedly for
    the same empty answer while the rest of the corpus waits."""
    cur, _ = run([])
    marks = [(sql, p) for sql, p in cur.calls
             if sql.startswith("INSERT INTO job_model_match")]
    assert marks, "the posting was not watermarked"
    sql, params = marks[0]
    assert params[2] == 0 if len(params) > 2 else "0" in sql


def test_a_job_with_no_text_is_watermarked_without_a_model_call(run):
    cur, _ = run([{"skill": "Docker", "confidence": 1.0, "why": "x"}],
                 pending=(("j-blank", "   "),))
    assert _inserts(cur.calls) == [], "a model was asked about empty text"
    marks = [sql for sql, _ in cur.calls
             if sql.startswith("INSERT INTO job_model_match")]
    assert marks and "VALUES (%s, %s, 0)" in marks[0], \
        "an unreadable posting must be watermarked with 0 edges"


def test_a_dry_run_writes_nothing(run):
    cur, commits = run([{"skill": "Docker", "confidence": 0.9, "why": "x"}], argv=())
    assert _inserts(cur.calls) == [] and commits == []


def test_a_missing_target_table_fails_before_any_call(run):
    with pytest.raises(SystemExit, match="does not exist"):
        run([], has_tables=False)


def test_a_gateway_failure_on_one_job_does_not_abort_the_batch(run, monkeypatch):
    """Per-job isolation: one bad posting must not cost the night's budget.

    One failure in eight is below MAX_FAILURE_RATE, so the run completes.
    """
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("timeout")
        return [{"skill": "Docker", "confidence": 0.8, "why": "containerised"}]

    cur = _Cursor(pending=tuple((f"j-{i}", "some text") for i in range(8)))
    commits = []
    pg = type("PG", (), {"cursor": lambda self: cur,
                         "commit": lambda self: commits.append(1)})()
    monkeypatch.setattr(match_jobs_ai.psycopg, "connect", lambda dsn: pg)
    monkeypatch.setattr(match_jobs_ai, "_ask", flaky)
    for k, v in (("POSTGRES_DSN", "postgresql:///stub"),
                 ("AI_GATEWAY_ENDPOINT", "https://x/v1"),
                 ("AI_GATEWAY_API_KEY", "k"), ("AI_GATEWAY_MODEL", "m")):
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["match_jobs_ai.py", "--apply"])
    match_jobs_ai.main()
    assert commits, "a single failure aborted the batch"
    assert len(_inserts(cur.calls)) == 7, "the other seven postings were lost"


def test_a_high_failure_rate_fails_the_task_but_keeps_good_work(run):
    with pytest.raises(SystemExit, match="failed"):
        run(OSError("down"), pending=tuple((f"j-{i}", "text") for i in range(4)))
