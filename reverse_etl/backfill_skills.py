"""Re-extract `skills` for every job already in Postgres. Dry-run by default.

Touches exactly two tables: `skills` and `job_skills`. Never writes `jobs`,
`companies`, `technologies`, `benefits` or their junctions — the original JD and
requirement text live on `jobs` and are left alone, which is the point: the
requirement bullets are not deleted, they simply stop being minted as lookup
rows.

Idempotent. The proposed set is a pure function of silver_current plus the
dictionary, and writes are UPSERT + full rewrite of each job's edges, so a
second run finds nothing to do.

    python backfill_skills.py              # dry run, prints the report
    python backfill_skills.py --apply      # snapshot, then write

Env: CLICKHOUSE_HOST / CLICKHOUSE_PASSWORD / CLICKHOUSE_USER / CLICKHOUSE_DB,
POSTGRES_DSN — the same ones sync_jobs.py uses.
"""

from __future__ import annotations

import argparse
import collections
import os
from datetime import datetime, timezone

import clickhouse_connect
import psycopg

from skills import extract_skills

SNAPSHOT_PREFIX = "backup_skills_"


def _snapshot(cur, stamp: str) -> tuple[str, str]:
    """CTAS copies of both tables. Plain tables, not temp: they must outlive the
    session so a bad result can be restored tomorrow, not just inside this run."""
    s_tbl, j_tbl = f"{SNAPSHOT_PREFIX}{stamp}", f"{SNAPSHOT_PREFIX}job_{stamp}"
    cur.execute(f'CREATE TABLE IF NOT EXISTS "{s_tbl}" AS TABLE skills')
    cur.execute(f'CREATE TABLE IF NOT EXISTS "{j_tbl}" AS TABLE job_skills')
    return s_tbl, j_tbl


def _proposed(ch) -> dict[str, list[str]]:
    """{job_id: [canonical skills]} straight from silver_current."""
    res = ch.query(
        "SELECT job_id, tech_stack, skills, description FROM silver_current WHERE job_id != ''"
    )
    out = {}
    for job_id, tech, reqs, desc in res.result_rows:
        out[job_id] = extract_skills(tech_stack=tech, description=desc, requirements=reqs)
    return out


def _current(cur) -> dict[str, set[str]]:
    cur.execute(
        "SELECT js.job_id, s.name FROM job_skills js JOIN skills s ON s.skill_id = js.skill_id"
    )
    out: dict[str, set[str]] = collections.defaultdict(set)
    for job_id, name in cur.fetchall():
        out[job_id].add(name)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default is dry run)")
    args = ap.parse_args()

    pg = psycopg.connect(os.environ["POSTGRES_DSN"])
    ch = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        username=os.environ.get("CLICKHOUSE_USER", "clickhouse"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "default"),
    )

    with pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM skills")
        n_skills_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM job_skills")
        n_edges_before = cur.fetchone()[0]
        current = _current(cur)
        cur.execute("SELECT job_id FROM jobs")
        known_jobs = {r[0] for r in cur.fetchall()}

    proposed = _proposed(ch)
    # Only jobs that exist in the target: a silver row with no Postgres job is
    # the reverse ETL's business, not this script's.
    proposed = {j: s for j, s in proposed.items() if j in known_jobs}

    additions = removals = 0
    unmatched = []
    for job_id in known_jobs:
        now, new = current.get(job_id, set()), set(proposed.get(job_id, []))
        additions += len(new - now)
        removals += len(now - new)
        if not new:
            unmatched.append(job_id)

    kept = sum(len(current.get(j, set()) & set(s)) for j, s in proposed.items())
    distinct_after = {s for v in proposed.values() for s in v}

    print(f"snapshot prefix      : {SNAPSHOT_PREFIX}<utc stamp>")
    print(f"jobs in target       : {len(known_jobs)}")
    print(f"skills   before/after: {n_skills_before} -> {len(distinct_after)}")
    print(f"edges    before/after: {n_edges_before} -> {sum(len(v) for v in proposed.values())}")
    print(f"edges kept unchanged : {kept}")
    print(f"edges added          : {additions}")
    print(f"edges removed        : {removals}")
    print(f"jobs with no skill   : {len(unmatched)}  (left with zero edges, flagged for review)")

    if not args.apply:
        print("\nDRY RUN — nothing written.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    with pg.cursor() as cur:
        s_tbl, j_tbl = _snapshot(cur, stamp)
        print(f"snapshot: {s_tbl}, {j_tbl}")

        for name in sorted(distinct_after):
            cur.execute(
                "INSERT INTO skills (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,)
            )
        cur.execute("SELECT name, skill_id FROM skills")
        ids = dict(cur.fetchall())

        for job_id, names in proposed.items():
            cur.execute("DELETE FROM job_skills WHERE job_id = %s", (job_id,))
            for name in names:
                cur.execute(
                    "INSERT INTO job_skills (job_id, skill_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (job_id, ids[name]),
                )

        # Orphans are the old polluted rows: nothing references them any more.
        cur.execute(
            "DELETE FROM skills s WHERE NOT EXISTS "
            "(SELECT 1 FROM job_skills js WHERE js.skill_id = s.skill_id)"
        )
        orphans = cur.rowcount
    pg.commit()
    print(f"applied. orphaned skill rows removed: {orphans}")


if __name__ == "__main__":
    main()
