"""Reverse ETL: curated skill prerequisites -> the web Postgres.

    python sync_skill_prerequisites.py            # dry run: the plan
    python sync_skill_prerequisites.py --apply    # write

Dry run by default, like the other syncs, and for a sharper reason here: these
rows order a learner's months.

SOURCE IS A FILE IN GIT, NOT THE WAREHOUSE. Unlike every other sync in this
directory, nothing is derived from crawled data -- there is no measurement that
can tell you Kubernetes depends on Docker. The source is
skills/prerequisites.yaml, reviewed in git by a named curator, and this job
only lands it. That is also why it ships inside the image rather than mounted:
the row's `curated_by` must correspond to a file someone approved.

FULL RECONCILE, INCLUDING DELETES. An edge removed from the YAML -- because
review decided it was never a dependence -- must disappear from the target.
A plain UPSERT would leave a retracted claim ordering learners for ever, so
every run DELETEs the edges that are no longer declared.

NO CYCLES REACH THE DATABASE. prerequisites.load() refuses a cyclic table
outright, so the sync fails before it opens a transaction. Postgres cannot
express that constraint, which makes this the only place it can be enforced.

Env:
  POSTGRES_DSN   (libpq keyword form or URL)
"""

from __future__ import annotations

import argparse
import os

import psycopg

from skills.prerequisites import all_edges


def _require_target(cur) -> None:
    cur.execute("SELECT to_regclass('public.skill_prerequisites')")
    if cur.fetchone()[0] is None:
        raise SystemExit(
            "skill_prerequisites does not exist in the target. The web app "
            "owns this schema and has to migrate it "
            "(20261102180000_add_skill_prerequisites.sql). Nothing was written."
        )


def _skill_ids(cur, names: set[str]) -> dict[str, int]:
    """Resolve names against the SHARED `skills` lookup. Never inserts.

    sync_jobs and sync_course_skills mint skill rows because they discover
    skills in crawled text. This file cannot discover anything: every name in
    it was validated against skills.dictionary at load. A name missing from the
    lookup means the job and course syncs have not run yet, and inventing the
    row here would create a skill no posting and no course points at.
    """
    out: dict[str, int] = {}
    for name in sorted(names):
        cur.execute("SELECT skill_id FROM skills WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            out[name] = row[0]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    edges = all_edges()          # raises on unknown skills, bad kinds or cycles
    print(f"sync_skill_prerequisites: {len(edges)} curated edge(s)")

    pg = psycopg.connect(os.environ["POSTGRES_DSN"])
    with pg.cursor() as cur:
        _require_target(cur)
        ids = _skill_ids(cur, {n for e in edges for n in (e.skill, e.requires)})

        rows, missing = [], []
        for e in edges:
            a, b = ids.get(e.skill), ids.get(e.requires)
            if a is None or b is None:
                missing.append(f"{e.skill} <- {e.requires}")
                continue
            rows.append((a, b, e.evidence_kind, e.why, e.curated_by))

        for line in (f"  {e.skill} <- {e.requires}  [{e.evidence_kind}] {e.why}"
                     for e in edges):
            print(line)
        if missing:
            # Not fatal: the skill is real, it just has no row yet because no
            # posting or course has mentioned it. Named so it is not silent.
            print(f"\n  {len(missing)} edge(s) skipped, skill not in the lookup yet:")
            for m in missing:
                print(f"    {m}")

        if not args.apply:
            print("\nsync_skill_prerequisites: DRY RUN — nothing written.")
            return

        # Reconcile: drop what is no longer declared, then upsert the rest.
        keep = [(r[0], r[1]) for r in rows]
        if keep:
            cur.execute(
                "DELETE FROM skill_prerequisites "
                "WHERE (skill_id, requires_skill_id) <> ALL(%s)",
                (keep,),
            )
        else:
            cur.execute("DELETE FROM skill_prerequisites")

        for row in rows:
            cur.execute(
                "INSERT INTO skill_prerequisites "
                "  (skill_id, requires_skill_id, evidence_kind, why, curated_by) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (skill_id, requires_skill_id) DO UPDATE SET "
                "  evidence_kind = EXCLUDED.evidence_kind, "
                "  why = EXCLUDED.why, "
                "  curated_by = EXCLUDED.curated_by",
                row,
            )
        pg.commit()
        print(f"\nsync_skill_prerequisites: wrote {len(rows)} edge(s)")


if __name__ == "__main__":
    main()
