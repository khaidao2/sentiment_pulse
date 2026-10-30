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
from datetime import datetime, timedelta, timezone

import clickhouse_connect
import psycopg

from skills import extract_skills

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


# Postgres cannot index a btree row wider than 2704 bytes, and every lookup
# table has a UNIQUE index on its name. Parsers sometimes emit a whole
# requirements paragraph as one "skill" (careerviet maps job_requirement
# straight across), and one 6107-byte value aborted the entire sync with
#   ProgramLimitExceeded: index row size 3016 exceeds btree version 4 maximum 2704
# Headroom under the hard limit because the index row carries overhead on top of
# the value itself.
MAX_LOOKUP_BYTES = 2000
_skipped_lookups = []
# [raw requirement bullets seen, canonical skills kept]
_extraction_stats = [0, 0]


def _upsert_lookup(cur, table, col, id_col, value):
    """UPSERT a single lookup value, return its id (None for empty/oversized)."""
    if value is None or value == "":
        return None
    # Guard, not truncation: a truncated paragraph is not a skill, and cutting it
    # would mint a bogus lookup row that later exact values never match. Skipping
    # loses the junction edge for that one value and keeps the job itself.
    size = len(str(value).encode("utf-8"))
    if size > MAX_LOOKUP_BYTES:
        _skipped_lookups.append((table, size))
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

    # 1-day look-back: rows that reach silver after a later-stamped row was
    # already synced are still picked up; the upsert makes the overlap free.
    #
    # The subtraction happens HERE, not in ClickHouse, and is floored at the
    # epoch. ClickHouse DateTime is an unsigned 32-bit epoch and cannot hold a
    # pre-1970 value, so `parseDateTimeBestEffort('1970-01-01Z') - INTERVAL 1
    # DAY` does not clamp — it WRAPS to 2106-02-06. On an empty target the
    # watermark is exactly that epoch sentinel, so the predicate became
    # `crawled_at > 2106` and matched nothing: the job printed "nothing newer",
    # exited 0, and Airflow recorded success while writing zero rows. A cold
    # first sync could never happen. Verified against the live table: with the
    # look-back 0 rows, without it 1046.
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
        f"SELECT {', '.join(SELECT_COLS)} FROM silver_current "
        "WHERE parseDateTimeBestEffort(crawled_at) > parseDateTimeBestEffort(%(wm)s)",
        parameters={"wm": lower_bound.isoformat()},
    )
    rows = [dict(zip(result.column_names, r)) for r in result.result_rows]

    if not rows:
        print(f"reverse-etl: nothing newer than {lower_bound.isoformat()}")
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
                values = row[arr_col]
                if arr_col == "skills":
                    # `skills` off the crawler is NOT a skills list — every
                    # parser fills it with the requirements section's bullets,
                    # so its values are sentences, section headers and even
                    # recruitment copy. Storing them as lookup rows is what put
                    # 13495 rows averaging 104 bytes in this table. Read them as
                    # text to scan instead, and keep only dictionary matches.
                    values = extract_skills(
                        tech_stack=row.get("tech_stack"),
                        description=row.get("description"),
                        requirements=row.get("skills"),
                    )
                    _extraction_stats[0] += len(row.get("skills") or [])
                    _extraction_stats[1] += len(values)
                _sync_junction(cur, row["job_id"], values, ltbl, lid, jtbl, jfk)
    pg.commit()
    print(f"reverse-etl: upserted {len(rows)} job(s) (watermark {watermark.isoformat()})")
    print(
        f"reverse-etl: skills — {_extraction_stats[0]} requirement bullet(s) read, "
        f"{_extraction_stats[1]} canonical skill(s) stored (dictionary matches only)"
    )
    if _skipped_lookups:
        worst = max(size for _, size in _skipped_lookups)
        tables = sorted({t for t, _ in _skipped_lookups})
        print(
            f"reverse-etl: skipped {len(_skipped_lookups)} lookup value(s) over "
            f"{MAX_LOOKUP_BYTES} bytes (largest {worst}) in {', '.join(tables)}. "
            "These are parser output that is prose, not a label — the junction "
            "edge is dropped, the job row is not."
        )


if __name__ == "__main__":
    main()
