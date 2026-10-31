"""Populate jobs.responsibilities / requirements / nice_to_have. Dry run by default.

The reverse ETL only revisits a job when its crawled_at passes the watermark, so
rows already in Postgres would keep NULL in the new columns indefinitely. This
fills them once from silver_current.

Writes THREE columns on `jobs` and nothing else — no junctions, no company, no
app-owned fields (is_active, expires_at, owner_user_id). Idempotent: the values
are a pure function of silver_current, and a second run writes the same thing.

    python backfill_requirements.py            # dry run + report
    python backfill_requirements.py --apply    # snapshot, then write

Env: CLICKHOUSE_HOST / CLICKHOUSE_PASSWORD / CLICKHOUSE_USER / CLICKHOUSE_DB,
POSTGRES_DSN.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

import clickhouse_connect
import psycopg

# Postgres column -> ClickHouse column. `requirements` is called `skills` there.
FIELDS = {"responsibilities": "responsibilities", "requirements": "skills",
          "nice_to_have": "nice_to_have"}
SNAPSHOT_PREFIX = "backup_jobreq_"


def _fetch(ch):
    cols = ", ".join(dict.fromkeys(["job_id", *FIELDS.values()]))
    res = ch.query(f"SELECT {cols} FROM silver_current WHERE job_id != ''")
    names = res.column_names
    out = {}
    for row in res.result_rows:
        rec = dict(zip(names, row))
        out[rec["job_id"]] = {pg: list(rec.get(ch_col) or []) for pg, ch_col in FIELDS.items()}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--samples", type=int, default=3)
    args = ap.parse_args()

    pg = psycopg.connect(os.environ["POSTGRES_DSN"])
    ch = clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        username=os.environ.get("CLICKHOUSE_USER", "clickhouse"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "default"),
    )

    source = _fetch(ch)

    with pg.cursor() as cur:
        cur.execute("SELECT job_id, source, responsibilities, requirements, nice_to_have FROM jobs")
        current = {r[0]: {"source": r[1], "responsibilities": r[2],
                          "requirements": r[3], "nice_to_have": r[4]} for r in cur.fetchall()}

    present = [j for j in current if j in source]
    missing = [j for j in current if j not in source]

    print(f"jobs in postgres          : {len(current)}")
    print(f"  matched in silver_current: {len(present)}")
    print(f"  absent from silver       : {len(missing)}  (left untouched)")

    print("\n-- source coverage (of the matched jobs) --")
    per_source: dict[str, dict[str, int]] = {}
    for col in FIELDS:
        nonempty = sum(1 for j in present if source[j][col])
        empty = len(present) - nonempty
        print(f"  {col:<17} non-empty {nonempty:>5}   empty in source {empty:>5}")
    for j in present:
        s = current[j]["source"] or "?"
        d = per_source.setdefault(s, {c: 0 for c in FIELDS})
        for col in FIELDS:
            if source[j][col]:
                d[col] += 1
    print("  by crawler source:")
    for s, d in sorted(per_source.items()):
        total = sum(1 for j in present if current[j]["source"] == s)
        print(f"    {s:<11} n={total:<5} " + "  ".join(f"{c}={d[c]}" for c in FIELDS))

    print("\n-- current postgres state (NULL = never synced, {} = synced but none) --")
    for col in FIELDS:
        nulls = sum(1 for j in current.values() if j[col] is None)
        empties = sum(1 for j in current.values() if j[col] == [])
        filled = len(current) - nulls - empties
        print(f"  {col:<17} NULL {nulls:>5}   empty[] {empties:>5}   filled {filled:>5}")

    changed = [j for j in present
               if any(current[j][c] != source[j][c] for c in FIELDS)]
    print(f"\njobs eligible for backfill : {len(changed)} of {len(present)}")

    print("\n-- discrepancy checks --")
    longest = max((len(v) for j in present for col in FIELDS for v in source[j][col]), default=0)
    total_items = sum(len(source[j][col]) for j in present for col in FIELDS)
    nul_bytes = sum(1 for j in present for col in FIELDS for v in source[j][col] if "\x00" in v)
    blanks = sum(1 for j in present for col in FIELDS for v in source[j][col] if not v.strip())
    print(f"  total array items         : {total_items}")
    print(f"  longest single item       : {longest} bytes  (text[] has no length limit — no truncation)")
    print(f"  items containing NUL      : {nul_bytes}  (would be rejected by Postgres)")
    print(f"  blank/whitespace items    : {blanks}")

    print(f"\n-- sample comparisons (first {args.samples}) --")
    for j in [x for x in present if source[x]["requirements"]][: args.samples]:
        print(f"  job {j[:12]}…  source={current[j]['source']}")
        for col in FIELDS:
            ch_v, pg_v = source[j][col], current[j][col]
            shown = (ch_v[0][:70] + "…") if ch_v else "—"
            print(f"    {col:<17} CH n={len(ch_v):<3} PG={'NULL' if pg_v is None else f'n={len(pg_v)}'}  first: {shown}")

    if not args.apply:
        print("\nDRY RUN — nothing written.")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    tbl = f"{SNAPSHOT_PREFIX}{stamp}"
    with pg.cursor() as cur:
        cur.execute(
            f'CREATE TABLE IF NOT EXISTS "{tbl}" AS '
            "SELECT job_id, responsibilities, requirements, nice_to_have FROM jobs"
        )
        for j in changed:
            v = source[j]
            cur.execute(
                "UPDATE jobs SET responsibilities = %s, requirements = %s, nice_to_have = %s "
                "WHERE job_id = %s",
                (v["responsibilities"], v["requirements"], v["nice_to_have"], j),
            )
    pg.commit()
    print(f"applied to {len(changed)} job(s). snapshot: {tbl}")


if __name__ == "__main__":
    main()
