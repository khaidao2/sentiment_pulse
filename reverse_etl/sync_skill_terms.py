"""Reverse ETL: the skill vocabulary's SPELLINGS -> the web Postgres.

    python sync_skill_terms.py            # dry run: the plan
    python sync_skill_terms.py --apply    # write

WHY THIS EXISTS. The web `skills` table holds 113 canonical names and nothing
else, so the application could only ever recognise a skill spelled exactly the
way the dictionary displays it. A CV that says "k8s", "postgres", "reactjs" or
"golang" named a real skill and the application could not see it -- it had one
model call standing between a learner and their own CV, and no way to read the
plainest possible evidence without it.

This lands every spelling the dictionary accepts, so the CV reader can do the
same exact and free-text matching the crawler does, and the model becomes a
SUPPLEMENT for implicit evidence rather than the only path.

SCANNABILITY TRAVELS WITH THE TERM. Whether a term may be hunted for inside
prose is a property of the term, and the dictionary is the only thing that
knows it: "C#" is safe at two characters, "go" is not safe at two. Re-deriving
that rule in the web repository would be a second copy of a judgement that has
to stay identical, and the two repositories deploy separately. So the answer is
computed HERE by skills.extractor.is_scannable -- the same predicate that
builds the crawler's own scanner -- and stored per row.

FULL RECONCILE, INCLUDING DELETES. An alias retracted from the dictionary
(because review decided it was a false friend) must stop matching. A plain
UPSERT would leave it recognising CVs for ever.

NO SKILL IS INVENTED. Every term resolves to a canonical name that must
already have a `skills` row. A name with no row means the job and course syncs
have not run yet; the term is skipped and NAMED, never minted -- the same rule
sync_skill_prerequisites.py follows, and for the same reason.

Env:
  POSTGRES_DSN   (libpq keyword form or URL)
"""

from __future__ import annotations

import argparse
import os

import psycopg

from skills.dictionary import ALL_SKILLS
from skills.extractor import is_scannable, normalize


def terms() -> list[tuple[str, str, bool]]:
    """(normalised term, canonical name, scannable) for the whole vocabulary.

    The canonical display name is included as a term in its own right -- the
    crawler's LOOKUP does the same -- so a consumer needs exactly one table to
    match against and never has to union this with `skills`.

    Stored NORMALISED (NFKC, folded, decoration stripped) because that is the
    form a consumer compares against, and normalising at read time would mean
    the web repository owning a copy of normalize(). The display name stays
    available: it is the canonical column.
    """
    out: dict[str, tuple[str, str, bool]] = {}
    for canonical, aliases in ALL_SKILLS.items():
        for term in (canonical, *aliases):
            key = normalize(term)
            if not key:
                continue
            # setdefault, matching LOOKUP: the first canonical to claim a
            # spelling keeps it, so an alias collision cannot silently
            # re-point an existing term at a different skill.
            out.setdefault(key, (key, canonical, is_scannable(term)))
    return sorted(out.values())


def _require_target(cur) -> None:
    cur.execute("SELECT to_regclass('public.skill_terms')")
    if cur.fetchone()[0] is None:
        raise SystemExit(
            "skill_terms does not exist in the target. The web app owns this "
            "schema and has to migrate it "
            "(20261103150000_add_skill_terms.sql). Nothing was written."
        )


def _skill_ids(cur, names: set[str]) -> dict[str, int]:
    """Resolve canonical names against the SHARED `skills` lookup. Never inserts."""
    if not names:
        return {}
    cur.execute("SELECT name, skill_id FROM skills WHERE name = ANY(%s)",
                (sorted(names),))
    return dict(cur.fetchall())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    vocabulary = terms()
    scannable = sum(1 for _, _, s in vocabulary if s)
    print(f"sync_skill_terms: {len(vocabulary)} term(s) over "
          f"{len(ALL_SKILLS)} skill(s); {scannable} scannable in free text, "
          f"{len(vocabulary) - scannable} exact-match only")

    pg = psycopg.connect(os.environ["POSTGRES_DSN"])
    with pg.cursor() as cur:
        _require_target(cur)
        ids = _skill_ids(cur, {c for _, c, _ in vocabulary})

        rows, missing = [], set()
        for term, canonical, scan in vocabulary:
            sid = ids.get(canonical)
            if sid is None:
                missing.add(canonical)
                continue
            rows.append((term, sid, scan))

        if missing:
            # Not fatal: the skill is real, it just has no row yet because no
            # posting and no course has mentioned it. Named so it is not silent.
            print(f"\n  {len(missing)} skill(s) not in the lookup yet, terms skipped:")
            for name in sorted(missing):
                print(f"    {name}")

        if not args.apply:
            print(f"\n  would write {len(rows)} term(s). Sample:")
            for term, sid, scan in rows[:10]:
                print(f"    {term!r} -> skill_id {sid}"
                      f"{'' if scan else '  [exact only]'}")
            print("\nsync_skill_terms: DRY RUN — nothing written.")
            return

        # Reconcile: a retracted alias must stop matching. Parallel arrays
        # rather than composites, for the reason spelled out in
        # sync_skill_prerequisites.py -- Postgres cannot take a row
        # constructor against an array of anonymous composite types.
        if rows:
            cur.execute(
                "DELETE FROM skill_terms t WHERE t.term <> ALL(%s)",
                ([r[0] for r in rows],),
            )
        else:
            cur.execute("DELETE FROM skill_terms")

        for term, sid, scan in rows:
            cur.execute(
                "INSERT INTO skill_terms (term, skill_id, scannable) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (term) DO UPDATE SET "
                "  skill_id = EXCLUDED.skill_id, scannable = EXCLUDED.scannable",
                (term, sid, scan),
            )
        pg.commit()
        print(f"\nsync_skill_terms: wrote {len(rows)} term(s)")


if __name__ == "__main__":
    main()
