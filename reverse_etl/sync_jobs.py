"""Reverse ETL: push silver_current (ClickHouse) -> web Postgres (normalized, 3NF).

Data activation, textbook way:
  * source = curated silver_current (one row per job), NOT raw bronze;
  * idempotent: companies/lookups UPSERT by natural key, jobs UPSERT by job_id,
    junction rows are rewritten per job — so re-runs never duplicate;
  * incremental via a watermark READ FROM THE TARGET itself (max crawled_at in
    jobs) — no extra state to manage.

The web OWNS the schema (see sentiment_pulse_web/db/schema.sql); this only writes.

Target shape:
  companies 1--* jobs ;  jobs *--* {technologies, skills, benefits}

Env:
  CLICKHOUSE_HOST / CLICKHOUSE_PASSWORD / CLICKHOUSE_USER / CLICKHOUSE_DB
  POSTGRES_DSN   (libpq keyword form or URL)
"""

from __future__ import annotations

import os

import clickhouse_connect
import psycopg

# Scalar job attributes (columns on the jobs table, minus company_id/updated_at).
JOB_COLS = [
    "job_id", "job_url", "title", "salary_type", "salary_min_usd",
    "salary_max_usd", "currency", "location", "work_arrangement",
    "posted_days_ago", "min_years_experience", "description",
    "crawled_at", "source",
]
# Company attributes, deduped into the companies table on `name`.
COMPANY_COLS = ["company_name", "company_url", "company_industry", "country"]
# Array attribute -> (lookup table, its id col, junction table, its fk col).
ARRAY_COLS = {
    "tech_stack": ("technologies", "tech_id", "job_technologies", "tech_id"),
    "skills": ("skills", "skill_id", "job_skills", "skill_id"),
    "benefits": ("benefits", "benefit_id", "job_benefits", "benefit_id"),
}
SELECT_COLS = JOB_COLS + COMPANY_COLS + list(ARRAY_COLS)


def _upsert_lookup(cur, table, col, id_col, value):
    """UPSERT a single lookup value, return its id (None for empty)."""
    if value is None or value == "":
        return None
    cur.execute(
        f"INSERT INTO {table} ({col}) VALUES (%s) "
        f"ON CONFLICT ({col}) DO UPDATE SET {col} = EXCLUDED.{col} "
        f"RETURNING {id_col}",
        (value,),
    )
    return cur.fetchone()[0]


def _upsert_company(cur, name, url, industry, country):
    if not name:
        return None
    cur.execute(
        "INSERT INTO companies (name, url, industry, country) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (name) DO UPDATE SET "
        "url = EXCLUDED.url, industry = EXCLUDED.industry, country = EXCLUDED.country "
        "RETURNING company_id",
        (name, url, industry, country),
    )
    return cur.fetchone()[0]


def _upsert_job(cur, row, company_id):
    cols = ["company_id"] + JOB_COLS
    vals = [company_id] + [row[c] for c in JOB_COLS]
    updatable = [c for c in cols if c != "job_id"]
    cur.execute(
        f"INSERT INTO jobs ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))}) "
        f"ON CONFLICT (job_id) DO UPDATE SET "
        + ", ".join(f"{c} = EXCLUDED.{c}" for c in updatable)
        + ", updated_at = now()",
        vals,
    )


def _sync_junction(cur, job_id, values, lookup_tbl, lookup_id, junction_tbl, junction_fk):
    """Rewrite a job's M:N links: clear then re-insert the current set."""
    cur.execute(f"DELETE FROM {junction_tbl} WHERE job_id = %s", (job_id,))
    for v in values or []:
        vid = _upsert_lookup(cur, lookup_tbl, "name", lookup_id, v)
        if vid is None:
            continue
        cur.execute(
            f"INSERT INTO {junction_tbl} (job_id, {junction_fk}) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (job_id, vid),
        )


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
    result = ch.query(
        f"SELECT {', '.join(SELECT_COLS)} FROM silver_current "
        "WHERE parseDateTimeBestEffort(crawled_at) > parseDateTimeBestEffort(%(wm)s)",
        parameters={"wm": watermark.isoformat()},
    )
    rows = [dict(zip(result.column_names, r)) for r in result.result_rows]

    if not rows:
        print(f"reverse-etl: nothing newer than {watermark.isoformat()}")
        return

    # 3. per-job UPSERT into the normalized target, all in one transaction.
    with pg.cursor() as cur:
        for row in rows:
            company_id = _upsert_company(
                cur, row["company_name"], row["company_url"],
                row["company_industry"], row["country"],
            )
            _upsert_job(cur, row, company_id)
            for arr_col, (ltbl, lid, jtbl, jfk) in ARRAY_COLS.items():
                _sync_junction(cur, row["job_id"], row[arr_col], ltbl, lid, jtbl, jfk)
    pg.commit()
    print(f"reverse-etl: upserted {len(rows)} job(s) (watermark {watermark.isoformat()})")


if __name__ == "__main__":
    main()
