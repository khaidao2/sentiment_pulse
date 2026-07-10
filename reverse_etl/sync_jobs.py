"""Reverse ETL: push silver_current (ClickHouse) -> web Postgres `jobs` table.

Data activation, done the textbook way:
  * source = the curated silver_current (one row per job), NOT raw bronze;
  * idempotent UPSERT keyed on job_id, so re-runs never duplicate;
  * incremental via a watermark READ FROM THE TARGET itself (max crawled_at
    already in Postgres) — no extra state to manage.

The web OWNS the jobs table + its migrations; this only writes into it.

Env:
  CLICKHOUSE_HOST / CLICKHOUSE_PASSWORD / CLICKHOUSE_USER / CLICKHOUSE_DB
  POSTGRES_DSN   e.g. postgresql://user:pass@host:5432/webdb
"""

from __future__ import annotations

import os

import clickhouse_connect
import psycopg

# Columns pulled from silver_current and upserted into jobs — must match
# reverse_etl/jobs_schema.sql (minus updated_at, which Postgres sets).
COLS = [
    "job_id", "job_url", "title", "company_name", "company_url",
    "salary_type", "salary_min_usd", "salary_max_usd", "currency",
    "location", "work_arrangement", "posted_days_ago", "company_industry",
    "country", "min_years_experience", "tech_stack", "description",
    "skills", "benefits", "crawled_at", "source",
]


def main() -> None:
    pg = psycopg.connect(os.environ["POSTGRES_DSN"])

    # 1. watermark = newest crawled_at already in the target (self-tracking).
    with pg.cursor() as cur:
        cur.execute("SELECT coalesce(max(crawled_at), '1970-01-01Z') FROM jobs")
        watermark = cur.fetchone()[0]

    # 2. pull only the changed slice from ClickHouse. crawled_at is a String in
    #    ClickHouse; parse both sides so format never bites us.
    ch = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        username=os.environ.get("CLICKHOUSE_USER", "clickhouse"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "default"),
    )
    rows = ch.query(
        f"SELECT {', '.join(COLS)} FROM silver_current "
        "WHERE parseDateTimeBestEffort(crawled_at) > parseDateTimeBestEffort(%(wm)s)",
        parameters={"wm": watermark.isoformat()},
    ).result_rows

    if not rows:
        print(f"reverse-etl: nothing newer than {watermark.isoformat()}")
        return

    # 3. idempotent upsert.
    updatable = [c for c in COLS if c != "job_id"]
    sql = (
        f"INSERT INTO jobs ({', '.join(COLS)}) "
        f"VALUES ({', '.join(['%s'] * len(COLS))}) "
        f"ON CONFLICT (job_id) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
        + ", updated_at = now()"
    )
    with pg.cursor() as cur:
        cur.executemany(sql, rows)
    pg.commit()
    print(f"reverse-etl: upserted {len(rows)} row(s) (watermark {watermark.isoformat()})")


if __name__ == "__main__":
    main()
