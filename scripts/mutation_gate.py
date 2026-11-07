#!/usr/bin/env python3
"""Fail if the mutation score drops below a floor.

A green test suite says the tests ran; it does not say they would notice if the
code changed. Mutation testing answers that: mutmut edits the source (`>` for
`>=`, `+= 1` for `= 1`, a flipped `and`) and a mutant that no test objects to is
a line nothing actually checks. The score is `killed / (killed + survived)`.

The floor is a ratchet, not a target. It exists so the number cannot quietly
slide while coverage still reads green -- which is the failure mode a parser
suite has, because assertions on sample HTML pass whether or not the salary
branch they exercise is the one that matters.

`no_tests` mutants are excluded from the denominator: they are in code the
configured suites never import, so they measure mutmut's scope, not the tests.

Usage:
    python scripts/mutation_gate.py [--floor 60.0] [--stats PATH]

Run `mutmut run && mutmut export-cicd-stats` first; the stats land in
mutants/mutmut-cicd-stats.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_STATS = Path("mutants/mutmut-cicd-stats.json")

# Measured at 63.1% (849 killed / 496 survived) on 2026-11-07, over
# jobs/topcv/parser.py, jobs/careerviet/parser.py and shared/pipeline/
# crawl_service.py -- the scope set in setup.cfg. The floor sits a few points
# below that so an ordering or timing wobble is not a red build; raise it when
# the real score climbs, and do that in the same commit that earns it.
DEFAULT_FLOOR = 60.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--floor", type=float, default=DEFAULT_FLOOR)
    ap.add_argument("--stats", type=Path, default=DEFAULT_STATS)
    args = ap.parse_args(argv)

    if not args.stats.exists():
        print(
            f"error: {args.stats} not found -- did `mutmut run` and "
            "`mutmut export-cicd-stats` both run?",
            file=sys.stderr,
        )
        return 2

    stats = json.loads(args.stats.read_text(encoding="utf-8"))
    killed = stats.get("killed", 0)
    survived = stats.get("survived", 0)
    graded = killed + survived

    if graded == 0:
        print(
            "error: no mutant was graded. mutmut produced nothing to measure, "
            "which is a broken run and not a perfect score.",
            file=sys.stderr,
        )
        return 2

    score = 100.0 * killed / graded
    detail = (
        f"{score:.1f}% ({killed} killed / {survived} survived of {graded} graded; "
        f"{stats.get('no_tests', 0)} not covered by the configured suites, "
        f"{stats.get('total', graded)} generated)"
    )

    if score + 1e-9 < args.floor:
        print(f"FAIL mutation score {detail} is below the {args.floor:.1f}% floor", file=sys.stderr)
        print(
            "Kill the survivors that represent real gaps -- `mutmut browse` "
            "lists them -- or justify lowering the floor in the same commit.",
            file=sys.stderr,
        )
        return 1

    print(f"OK mutation score {detail} meets the {args.floor:.1f}% floor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
