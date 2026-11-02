"""Reverse ETL: course -> canonical skill edges, into the web Postgres.

    python sync_course_skills.py            # dry run: the plan and the report
    python sync_course_skills.py --apply    # write

Dry run by default, like backfill_skills.py and for the same reason: this writes
a junction derived from a dictionary, and the dictionary changes.

WHERE THIS SITS. The repo's path for landing data is crawler -> Kafka -> MinIO
-> dbt -> reverse-ETL -> Postgres. Course->skill is DERIVED data and takes the
same path's last hop: silver_courses (ClickHouse) is read, the canonical
extractor decides, and the edges are UPSERTed into the web's Postgres beside the
courses sync_courses.py already lands there. It is not a hand-curated file and
not a dbt model:

  * not a file, because a YAML catalogue promoted by hand would be a second
    source of truth beside the warehouse. courses/match.py used to promise one;
    see its docstring for what happened to it.
  * not SQL, because `reverse_etl/skills` is the single authority on what a
    skill is. Re-implementing the dictionary in a dbt model would be a second
    place deciding that, and the job pipeline would drift from the course one.
    The matching therefore happens in Python, here, and the warehouse is read
    only as a source.

The web OWNS the schema, exactly as sync_jobs.py says of `jobs`; this only
writes. The target is the `skills` lookup table that sync_jobs.py already
maintains — a skill is a skill whether a posting asks for it or a course teaches
it, and keying courses to the SAME rows is the whole point: it is what lets the
app answer "what should I learn for this job".

Target shape, owned by the web app (see WHAT CANNOT WORK YET below):

    course_skills (course_id -> courses.course_id,
                   skill_id  -> skills.skill_id,
                   relation  'teaches' | 'mentions')

IDEMPOTENT, AND A FULL REBUILD — deliberately NOT watermarked. sync_courses.py
reads max(crawled_at) from its target and pulls the newer slice, which is right
for facts that only change when the crawl changes them. These edges also change
when the DICTIONARY changes, and a watermark cannot see that: a new canonical
skill would never reach a course crawled last week. So every run matches the
whole catalogue (7,205 rows, seconds) and rewrites the edges of every course it
saw, including the ones that matched before and no longer do. That deletion is
the half a plain UPSERT would miss.

WHAT THIS DOES NOT DO, deliberately:

  * No `summary`, ever. It is scanned to produce `mentions` and the only thing
    that leaves this process is a canonical skill NAME. There is no summary
    column in the target and there must not be one.
  * No level, duration or language. Not read, not derived, not written. Weeks
    are not minutes and 'UNKNOWN' is not 'INTRO'; sync_courses.py copies those
    facts across as published and nothing here touches them.
  * No hierarchy walk. A LEARNING_PATH does not inherit the skills of the
    MODULEs under it, and a MODULE does not inherit its path's. Each course is
    matched on its own title. A consumer that wants "paths teaching Azure" joins
    course_hierarchy itself, on data it can see.
  * No skills for a course with no title match. Those courses contribute no
    edges at all, not even their `mentions`.
  * No invention of a course. An edge is only written for a course_id already in
    `courses`; the count skipped for that reason is reported, and a nonzero one
    means sync_courses.py has not run yet in this DAG run.

WHAT CANNOT WORK YET. The `course_skills` table does not exist: the web app's
20261102090000_add_courses_catalogue.sql migration deliberately left it out
because no skill data existed when it was written, and that migration is itself
unapplied. This job is correct and will fail loudly, not silently, against a
database without the table — see _require_target(). The web repo owns that
schema and has to add it; the shape is in this docstring.

Env:
  CLICKHOUSE_HOST / CLICKHOUSE_PASSWORD / CLICKHOUSE_USER / CLICKHOUSE_DB
  POSTGRES_DSN   (libpq keyword form or URL)
"""

from __future__ import annotations

import argparse
import collections
import os

import psycopg

from courses import match, overrides as overrides_mod

# Same guard, same number, same reason as sync_jobs.MAX_LOOKUP_BYTES: `skills`
# has a UNIQUE index on name and Postgres cannot index a btree row wider than
# 2704 bytes. A canonical skill is never near this — the dictionary's longest
# name is under 30 bytes — so tripping it means something that is not a skill
# reached this function, and skipping is better than minting the row.
MAX_LOOKUP_BYTES = 2000


def _require_target(cur) -> None:
    """Fail now, with the fix, rather than half-way through a transaction."""
    cur.execute("SELECT to_regclass('public.course_skills')")
    if cur.fetchone()[0] is None:
        raise SystemExit(
            "course_skills does not exist in the target. The web app owns this "
            "schema and has to migrate it; the shape is in this module's "
            "docstring. Nothing was written."
        )


def _skill_ids(cur, names: set[str]) -> dict[str, int]:
    """UPSERT each canonical name into the shared `skills` lookup, keep the ids.

    The same table sync_jobs.py fills. A course teaching Kubernetes and a
    posting asking for Kubernetes must point at ONE row, or the join that makes
    this feature work compares two different ids for one word.
    """
    out: dict[str, int] = {}
    for name in sorted(names):
        if len(name.encode("utf-8")) > MAX_LOOKUP_BYTES:
            continue
        cur.execute(
            "INSERT INTO skills (name) VALUES (%s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
            "RETURNING skill_id",
            (name,),
        )
        out[name] = cur.fetchone()[0]
    return out


def _known_courses(cur) -> set[str]:
    cur.execute("SELECT course_id FROM courses")
    return {row[0] for row in cur.fetchall()}


def _rewrite(cur, course_ids: list[str], edges, skill_ids) -> int:
    """Clear every processed course's edges, then insert the current set.

    Delete-then-insert over the WHOLE set of courses seen this run, not just the
    ones that matched: a course that loses its last title match — because the
    dictionary changed, or a provider retitled it — has to lose its edges, and
    an UPSERT alone would leave them behind for ever.
    """
    if course_ids:
        cur.execute("DELETE FROM course_skills WHERE course_id = ANY(%s)", (course_ids,))
    written = 0
    for course_id, skill, relation in edges:
        skill_id = skill_ids.get(skill)
        if skill_id is None:
            continue
        cur.execute(
            "INSERT INTO course_skills (course_id, skill_id, relation) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (course_id, skill_id, relation),
        )
        written += 1
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    ov = overrides_mod.load()
    rows = match.fetch(match.client())
    if not rows:
        print("sync_course_skills: silver_courses is empty — has crawl_courses "
              "run, and dbt after it? Nothing to do.")
        return

    edges, report = match.build_edges(rows, ov)
    match.print_report(report, edges)

    pg = psycopg.connect(os.environ["POSTGRES_DSN"])
    with pg.cursor() as cur:
        _require_target(cur)
        known = _known_courses(cur)

        # An edge for a course the catalogue sync has not landed yet would
        # violate the target's foreign key and abort the whole transaction.
        # Dropped and counted instead: in the DAG, sync_courses runs first and
        # this number is 0.
        kept_edges = [e for e in edges if e[0] in known]
        missing = {e[0] for e in edges if e[0] not in known}
        course_ids = sorted({row["id"] for row in rows} & known)

        by_relation = collections.Counter(rel for _, _, rel in kept_edges)
        print(
            f"\nsync_course_skills: {len(kept_edges)} edge(s) over "
            f"{len({e[0] for e in kept_edges})} course(s) — "
            f"{by_relation[match.TEACHES]} teaches, {by_relation[match.MENTIONS]} mentions"
        )
        if missing:
            print(
                f"sync_course_skills: {len(missing)} matched course(s) are not in "
                "`courses` yet — their edges are skipped, not invented. Run "
                "sync_courses.py first (the DAG does)."
            )

        if not args.apply:
            print("\nDRY RUN — nothing written. Pass --apply to write.")
            return

        skill_ids = _skill_ids(cur, {skill for _, skill, _ in kept_edges})
        written = _rewrite(cur, course_ids, kept_edges, skill_ids)
    pg.commit()
    print(
        f"sync_course_skills: rewrote the edges of {len(course_ids)} course(s), "
        f"wrote {written} edge(s) across {len(skill_ids)} canonical skill(s)"
    )


if __name__ == "__main__":
    main()
