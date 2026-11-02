"""Reverse ETL: push silver_courses (ClickHouse) -> web Postgres `courses`.

Same conventions as sync_jobs.py, for the same reasons:
  * source = curated silver_courses (one row per course id), NOT bronze_courses
    — the crawl re-reads every catalogue weekly, so bronze holds one copy of a
    course per run and upserting from there would do the same work N times;
  * idempotent: courses UPSERT on their natural key (course_id = sha256(url)),
    and the hierarchy junction is DELETEd and rewritten per parent, so re-runs
    never duplicate an edge or leave a stale one behind;
  * incremental via a watermark READ FROM THE TARGET itself (max crawled_at in
    courses) — no extra state to manage.

The web OWNS the schema (backend/db/migrations/20261102090000_add_courses_catalogue.sql);
this only writes.

Target shape:
  courses  ;  course_hierarchy (parent_uid, child_uid, position) as ROWS

WHAT THIS DOES NOT DO, deliberately:

  * No `summary`. The contract carries it into the raw zone only so the
    downstream skill matcher has text to scan (see its field doc in
    data-contracts/schemas/learning/courses.avsc, and match.py's COLUMNS list,
    which reads it and never writes it). The public catalogue holds facts and a
    link, and there is no summary column in Postgres to write it to.

  * No course -> skill rows. They are derived data with their own stage:
    sync_course_skills.py matches silver_courses against the canonical
    dictionary and writes the `course_skills` junction, after this script has
    landed the courses those edges point at. The DAG runs the two in that
    order. Nothing here matches or writes a skill.

  * No duration arithmetic. duration_minutes is Microsoft-only; MIT publishes
    duration_weeks_min plus weekly_hours_min, which is a calendar span and an
    effort rate, not minutes. Every duration column is copied across as the
    source published it.

  * No level inference. 'UNKNOWN' is copied verbatim.

  * No sorting of child_uids. Their order IS the curriculum sequence, and the
    position written to each edge is the index in that array.

  * No reconciling child_count with len(child_uids). When they disagree the
    hierarchy is partial, and that disagreement is the signal the web app reads
    to answer hierarchy_complete=false. Both land as they came.

STATE OF THE WORLD (re-verified 2026-11-02, later the same day): the first
crawl HAS now run. bronze_courses holds 18,410 rows and silver_courses 7,205
deduped courses — 4,297 mslearn (833 LEARNING_PATH + 3,464 MODULE), 2,807
mitlearn COURSE, 101 freecodecamp COURSE. The earlier note here said all three
were empty; that was true when it was written and is not any more. So the first
run of this script IS the cold start the watermark note below describes, and
max(..., _EPOCH) is what makes it write rather than report "nothing newer".

Env:
  CLICKHOUSE_HOST / CLICKHOUSE_PASSWORD / CLICKHOUSE_USER / CLICKHOUSE_DB
  POSTGRES_DSN   (libpq keyword form or URL)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import clickhouse_connect
import psycopg

# Scalar course attributes: the Postgres columns the sync owns, minus updated_at
# (the UPSERT stamps it with now()). All of them are pipeline-owned — the
# courses table has no app-owned column to keep out of this list, unlike jobs,
# where is_active / expires_at / owner_user_id must stay out of JOB_COLS.
#
# `summary` is absent on purpose and must stay absent. See the module docstring.
COURSE_COLS = [
    "course_id", "title", "source", "provider", "url", "uid",
    "content_kind", "level", "language", "is_free", "license_cc", "tags",
    # four source-provided duration facts, never combined into one
    "duration_minutes", "duration_text", "duration_weeks_min",
    "weekly_hours_min", "duration_is_derived",
    "child_count", "first_child_url", "crawled_at",
]

# Postgres column -> ClickHouse column, where the two disagree. Only one does:
# the contract's `id` is a sha256 of the url, and the web schema names it
# `course_id` to match jobs.job_id. Mapped explicitly here rather than renaming a
# contract field that the crawler, bronze, silver and match.py all already read.
SOURCE_COL = {"course_id": "id"}

# Read but never written to a scalar column: `child_uids` becomes rows in
# course_hierarchy, and `parent_uids` is the exact inverse of those rows, so the
# target derives parents from the edges instead of storing a second copy that
# could disagree with them.
HIERARCHY_COLS = ["child_uids", "parent_uids"]

# What to ask ClickHouse for: course columns under their SOURCE names, plus the
# hierarchy arrays. dict.fromkeys dedupes while keeping order, matching
# sync_jobs.py — there is no overlap today, but a future mapping that introduced
# one would otherwise be a syntax error in the SELECT.
SELECT_COLS = list(
    dict.fromkeys([SOURCE_COL.get(c, c) for c in COURSE_COLS] + HIERARCHY_COLS)
)


def _upsert_course(cur, row) -> None:
    """UPSERT one course on its natural key. Re-crawling replaces, never adds."""
    cols = COURSE_COLS
    vals = [row[SOURCE_COL.get(c, c)] for c in cols]
    updatable = [c for c in cols if c != "course_id"]
    cur.execute(
        f"INSERT INTO courses ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT (course_id) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
        + ", updated_at = now()",
        vals,
    )


def _sync_hierarchy(cur, parent_uid, child_uids) -> int:
    """Rewrite one parent's curriculum edges: clear, then re-insert in order.

    Delete-and-rewrite per PARENT, the same shape as sync_jobs._sync_junction:
    a module removed from a learning path between crawls has to lose its edge,
    and an UPSERT alone would leave it there for ever.

    ENUMERATE, DO NOT SORT. The source's array order is the curriculum sequence
    and the only ordering any catalogue gives us, so position = index: index 0 is
    step 1. Sorting here — by uid, title or anything — would silently reorder a
    learning path.

    A course whose source publishes no uid (freeCodeCamp rows are synthesised
    from filenames) can hold no edges, so it is skipped rather than keyed on
    None.

    The child uid is written whether or not we hold that course. A referenced
    child the crawl never fetched has no row in `courses`, and there is no
    foreign key precisely so the edge survives: losing it would lose the
    evidence that the hierarchy is partial. The API answers such an edge with a
    null course id.
    """
    if not parent_uid:
        return 0
    cur.execute("DELETE FROM course_hierarchy WHERE parent_uid = %s", (parent_uid,))
    written = 0
    for position, child_uid in enumerate(child_uids or []):
        if not child_uid:
            continue
        cur.execute(
            "INSERT INTO course_hierarchy (parent_uid, child_uid, position) "
            "VALUES (%s, %s, %s) ON CONFLICT (parent_uid, position) DO UPDATE "
            "SET child_uid = EXCLUDED.child_uid",
            (parent_uid, child_uid, position),
        )
        written += 1
    return written


def main() -> None:
    pg = psycopg.connect(os.environ["POSTGRES_DSN"])

    # 1. watermark = newest crawled_at already in the target (self-tracking).
    with pg.cursor() as cur:
        cur.execute("SELECT coalesce(max(crawled_at), '1970-01-01Z') FROM courses")
        watermark = cur.fetchone()[0]

    # 1-day look-back: rows that reach silver after a later-stamped row was
    # already synced are still picked up; the upsert makes the overlap free.
    #
    # The subtraction happens HERE, in Python, and is FLOORED AT THE EPOCH. This
    # is the bug sync_jobs.py documents, not reproduced: ClickHouse DateTime is
    # an unsigned 32-bit epoch and cannot hold a pre-1970 value, so
    # `parseDateTimeBestEffort('1970-01-01Z') - INTERVAL 1 DAY` does not clamp —
    # it WRAPS to 2106-02-06. On an empty target the watermark is exactly that
    # epoch sentinel, so the predicate would become `crawled_at > 2106`, match
    # nothing, print "nothing newer", exit 0, and let Airflow record success
    # while writing zero rows. A cold first sync could never happen.
    #
    # That failure mode matters more here than it did for jobs: the courses
    # target IS empty and the first run of this script WILL be a cold start, so
    # the wrap would be the first thing it did. max(..., _EPOCH) is what makes
    # the cold start work, and the empty-target case is the one to re-check if
    # this predicate is ever moved into ClickHouse.
    _EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
    wm = watermark if watermark.tzinfo else watermark.replace(tzinfo=timezone.utc)
    lower_bound = max(wm - timedelta(days=1), _EPOCH)

    # 2. pull only the changed slice from ClickHouse. crawled_at is a String in
    #    ClickHouse; parse both sides so format never bites us.
    ch = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        username=os.environ.get("CLICKHOUSE_USER", "clickhouse"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "default"),
    )
    result = ch.query(
        f"SELECT {', '.join(SELECT_COLS)} FROM silver_courses "
        "WHERE parseDateTimeBestEffort(crawled_at) > parseDateTimeBestEffort(%(wm)s)",
        parameters={"wm": lower_bound.isoformat()},
    )
    rows = [dict(zip(result.column_names, r)) for r in result.result_rows]

    if not rows:
        # Expected today: silver_courses holds 0 rows because crawl_courses has
        # never run. Said plainly so an empty run is not mistaken for the
        # watermark bug above.
        print(
            f"reverse-etl/courses: nothing newer than {lower_bound.isoformat()} "
            "(silver_courses empty? crawl_courses is paused and dbt must run after it)"
        )
        return

    # 3. per-course UPSERT into the target, all in one transaction.
    edges = 0
    partial = 0
    for row in rows:
        if row["child_count"] is not None and row["child_count"] > len(row["child_uids"] or []):
            partial += 1
    with pg.cursor() as cur:
        for row in rows:
            _upsert_course(cur, row)
            edges += _sync_hierarchy(cur, row["uid"], row["child_uids"])
    pg.commit()

    print(
        f"reverse-etl/courses: upserted {len(rows)} course(s), "
        f"{edges} hierarchy edge(s) (watermark {watermark.isoformat()})"
    )
    if partial:
        # Reported, not repaired. A course whose source claims more children
        # than the crawl saw is a PARTIAL hierarchy; the web app reads the same
        # disagreement and answers hierarchy_complete=false. A jump in this
        # number means paging or max_pages is cutting catalogues short.
        print(
            f"reverse-etl/courses: {partial} course(s) report more children than "
            "were crawled — partial hierarchy, surfaced to the app as "
            "hierarchy_complete=false, never reconciled here"
        )


if __name__ == "__main__":
    main()
