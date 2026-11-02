"""Reverse ETL: job -> role family, into the web Postgres.

    python sync_job_roles.py            # dry run: the plan and the report
    python sync_job_roles.py --apply    # write

Dry run by default, like sync_course_skills.py and backfill_skills.py, and for
the same reason: this writes a derivation, and the rules it derives from change.

WHERE THE TITLES COME FROM. Postgres `jobs`, not ClickHouse `silver_current`,
which is the one place this job departs from sync_course_skills.py. Three
reasons, in order of weight:

  * `jobs.job_id` is this table's FOREIGN KEY parent. Classifying straight from
    the parent means a row can never be written for a job the target does not
    have, so there is no "skipped, not landed yet" counter to reconcile.
  * `jobs` also holds app-posted rows (source = 'web') that never pass through
    ClickHouse. Reading silver_current would classify the crawled corpus and
    silently leave those out, and "all jobs accounted for" would stop being
    checkable.
  * the input is one column the crawl already landed verbatim. This is a pure
    function of `jobs.title`, closer to a materialised view than to an import;
    re-deriving it from the warehouse would add a hop that can disagree.

IDEMPOTENT, AND A FULL REBUILD — deliberately NOT watermarked, the same choice
sync_course_skills.py makes. These rows change when rules.yaml changes, and a
watermark cannot see that: a sharpened rule would never reach a job crawled last
week. Every run classifies every job and UPSERTs on the primary key, so a run is
reconciliation rather than accumulation. The whole corpus is 1,271 rows and
takes well under a second.

WHAT THIS DOES NOT DO, deliberately:

  * No forcing. UNKNOWN and AMBIGUOUS are written as themselves. A title that
    matches nothing or matches two families is an answer, and burying it in a
    family would turn 44.3% of the corpus into a confident guess.
  * No title rewriting. The raw crawled title is stored verbatim; the NFKC
    casefolding the classifier does is an implementation detail of matching and
    must not leak into storage.
  * No rules. This module decides nothing about what a family is -- roles/
    owns that, and a second opinion here is exactly the drift the skills
    dictionary exists to prevent.
  * No LLM. A classification is reproducible from the title, rules.yaml and the
    stored `method` alone.

Env:
  POSTGRES_DSN   (libpq keyword form or URL)
"""

from __future__ import annotations

import argparse
import collections
import os

import psycopg

from roles.classify import AMBIGUOUS, METHOD, UNKNOWN, classify

#: How `matched_rules` is encoded into text[]. One string per rule that fired,
#: including the rules of families that matched and LOST -- auditing "why
#: Fullstack and not Backend" needs to see that Backend matched as a qualifier.
RULE_SEP = "|"


def encode_rule(family: str, pattern: str, tier: str) -> str:
    return RULE_SEP.join((family, pattern, tier))


def decode_rule(encoded: str) -> tuple[str, str, str]:
    family, pattern, tier = encoded.split(RULE_SEP, 2)
    return family, pattern, tier


def _require_target(cur) -> None:
    """Fail now, with the fix, rather than half-way through a transaction."""
    cur.execute("SELECT to_regclass('public.job_role_family')")
    if cur.fetchone()[0] is None:
        raise SystemExit(
            "job_role_family does not exist in the target. The web app owns "
            "this schema and has to migrate it "
            "(20261102160000_add_job_role_family.sql). Nothing was written."
        )


def plan(rows: list[tuple[str, str]]) -> list[tuple]:
    """(job_id, family, seniority, method, matched_rules, title) for every job.

    EVERY job gets a row, including the ones that matched nothing. A table that
    held only the classified half would read as full coverage of a corpus it
    covers 54.6% of.
    """
    out = []
    for job_id, title in rows:
        c = classify(title)
        out.append((
            job_id,
            c.family,
            c.seniority,
            c.method,
            [encode_rule(f, p, t) for f, p, t in c.matched],
            title,                      # verbatim, never the normalised key
        ))
    return out


def report(planned: list[tuple]) -> str:
    by_family = collections.Counter(r[1] for r in planned)
    by_seniority = collections.Counter(r[2] for r in planned)
    total = len(planned) or 1
    usable = sum(n for f, n in by_family.items() if f not in (UNKNOWN, AMBIGUOUS))
    lines = [
        f"sync_job_roles: {len(planned)} job(s), method={METHOD}",
        f"  classified to a family {usable:5} ({100*usable/total:4.1f}%)",
        f"  AMBIGUOUS              {by_family[AMBIGUOUS]:5} ({100*by_family[AMBIGUOUS]/total:4.1f}%)",
        f"  UNKNOWN                {by_family[UNKNOWN]:5} ({100*by_family[UNKNOWN]/total:4.1f}%)",
        "",
        "  per family:",
    ]
    for fam, n in by_family.most_common():
        if fam in (UNKNOWN, AMBIGUOUS):
            continue
        lines.append(f"    {fam:22} {n:5}")
    lines.append("")
    lines.append("  seniority (independent of family):")
    for lvl, n in by_seniority.most_common():
        lines.append(f"    {str(lvl):22} {n:5}")
    return "\n".join(lines)


def _write(cur, planned: list[tuple]) -> int:
    """UPSERT on the primary key, so a re-run reconciles instead of appending.

    classified_at is refreshed on every write: it records when this row was last
    derived, which is what tells a reviewer whether a row predates a rules
    change. The row is rewritten even when nothing about it changed, because
    distinguishing those would cost a read and buy nothing for 1,271 rows.
    """
    written = 0
    for row in planned:
        cur.execute(
            "INSERT INTO job_role_family "
            "  (job_id, family, seniority, method, matched_rules, title) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (job_id) DO UPDATE SET "
            "  family = EXCLUDED.family, "
            "  seniority = EXCLUDED.seniority, "
            "  method = EXCLUDED.method, "
            "  matched_rules = EXCLUDED.matched_rules, "
            "  title = EXCLUDED.title, "
            "  classified_at = now()",
            row,
        )
        written += 1
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    pg = psycopg.connect(os.environ["POSTGRES_DSN"])
    with pg.cursor() as cur:
        _require_target(cur)
        cur.execute("SELECT job_id, title FROM jobs")
        rows = [(r[0], r[1]) for r in cur.fetchall()]
        if not rows:
            print("sync_job_roles: `jobs` is empty — has sync_jobs run? Nothing to do.")
            return

        planned = plan(rows)
        print(report(planned))

        if not args.apply:
            print("\nsync_job_roles: DRY RUN — nothing written. Re-run with --apply.")
            return

        written = _write(cur, planned)
        pg.commit()
        print(f"\nsync_job_roles: wrote {written} classification(s)")


if __name__ == "__main__":
    main()
