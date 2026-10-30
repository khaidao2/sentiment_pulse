"""The requirement bullets go to `jobs.requirements` as prose and nowhere else.

ClickHouse calls them `skills`, Postgres calls them `requirements`, and the
`skills` LOOKUP table must still receive only dictionary matches. The three
names colliding is the whole hazard this file guards.

The cases that exercise main() drive it against a recording cursor and a stub
ClickHouse client, so the SQL is asserted without a database.
"""

from datetime import datetime, timezone

import pytest

import sync_jobs


# A real posting shape: the parser dumped the requirements section into
# `skills`, one bullet of which happens to mention a dictionary term.
BULLETS = [
    "At least 3 years building data pipelines in Python",
    "Yêu cầu: tốt nghiệp đại học chuyên ngành CNTT",
]
ROW = {
    "job_id": "j-1", "job_url": "https://x/1", "title": "Data Engineer",
    "salary_type": "range", "salary_min_usd": 1000, "salary_max_usd": 2000,
    "currency": "USD", "location": "Hanoi", "work_arrangement": "hybrid",
    "posted_days_ago": 2, "min_years_experience": 3, "description": "Build ETL.",
    "crawled_at": "2026-10-30 00:00:00", "source": "itviec",
    "responsibilities": ["Design and run the pipelines"],
    "nice_to_have": ["Kafka exposure"],
    "skills": BULLETS,
    "company_name": "ACME", "company_url": "https://acme",
    "company_industry": "IT", "country": "VN",
    "tech_stack": ["Python", "Totally Made Up Tag"],
    "benefits": ["13th month salary"],
}


# 1 — the SELECT is valid SQL: `skills` is both a JOB_COLS source and an
#     ARRAY_COLS key, and naming it twice is a syntax error.
def test_select_cols_has_no_duplicates():
    assert len(sync_jobs.SELECT_COLS) == len(set(sync_jobs.SELECT_COLS))
    assert sync_jobs.SELECT_COLS.count("skills") == 1


# 2 — `requirements` is a Postgres name only; asking ClickHouse for it would
#     fail, because there the column does not exist.
def test_select_cols_asks_for_source_names_only():
    assert "requirements" not in sync_jobs.SELECT_COLS
    for col in sync_jobs.JOB_COLS:
        assert sync_jobs.SOURCE_COL.get(col, col) in sync_jobs.SELECT_COLS


class _Cursor:
    """Records every statement; answers the two queries main() reads back."""

    def __init__(self):
        self.calls = []
        self._next_id = 0
        self._last = ""

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        self._last = sql

    def fetchone(self):
        if "max(crawled_at)" in self._last:
            return (datetime(2026, 10, 1, tzinfo=timezone.utc),)
        self._next_id += 1
        return (self._next_id,)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Result:
    def __init__(self, row):
        self.column_names = list(row)
        self.result_rows = [tuple(row.values())]


@pytest.fixture
def run_sync(monkeypatch):
    """Run main() against the stubs and hand back the recorded statements."""

    cur = _Cursor()
    pg = type("PG", (), {"cursor": lambda self: cur, "commit": lambda self: None})()
    ch = type("CH", (), {"query": lambda self, sql, parameters=None: _Result(ROW)})()

    monkeypatch.setattr(sync_jobs.psycopg, "connect", lambda dsn: pg)
    monkeypatch.setattr(sync_jobs.clickhouse_connect, "get_client", lambda **kw: ch)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql:///stub")
    monkeypatch.setenv("CLICKHOUSE_HOST", "stub")
    monkeypatch.setattr(sync_jobs, "_skipped_lookups", [])
    monkeypatch.setattr(sync_jobs, "_extraction_stats", [0, 0])

    sync_jobs.main()
    return cur.calls


def _job_insert(calls):
    """The INSERT INTO jobs, as a {column: value} dict."""
    sql, params = next(c for c in calls if c[0].startswith("INSERT INTO jobs ("))
    cols = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
    return dict(zip(cols, params))


def _lookup_values(calls, table):
    return [p[0] for sql, p in calls if sql.startswith(f"INSERT INTO {table} (name)")]


# 3 — the bullets land in the Postgres column named `requirements`, read from
#     the ClickHouse column named `skills`.
def test_requirements_column_is_written_from_the_clickhouse_skills_column(run_sync):
    job = _job_insert(run_sync)
    assert job["requirements"] == BULLETS
    assert job["responsibilities"] == ROW["responsibilities"]
    assert job["nice_to_have"] == ROW["nice_to_have"]


# 4 — and nowhere else: the lookup table keeps dictionary matches only, so the
#     prose that polluted it does not come back in through the new column.
def test_requirement_prose_never_becomes_a_skill_lookup_row(run_sync):
    stored = _lookup_values(run_sync, "skills")
    assert "Python" in stored
    assert not set(stored) & set(BULLETS)
    assert "Totally Made Up Tag" not in stored


# 5 — every scalar column the target expects is written, so a new JOB_COLS
#     entry without a source cannot silently sync as NULL.
def test_every_job_column_is_written(run_sync):
    job = _job_insert(run_sync)
    assert set(job) == {"company_id", *sync_jobs.JOB_COLS}
