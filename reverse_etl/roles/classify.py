"""Classify a free-text job title into a role family, auditably.

Job titles are free text: 1,192 of 1,271 are distinct, so nothing can be keyed
on the title itself. A role family is the unit a demand profile is computed
over -- "what do Data postings actually ask for" -- so a wrong assignment
silently corrupts every number downstream.

THREE OUTCOMES, AND TWO OF THEM ARE NOT FAMILIES:

  <family>   exactly one family's patterns matched
  AMBIGUOUS  two or more did; the title genuinely reads as both
  UNKNOWN    none did

AMBIGUOUS and UNKNOWN are answers, not failures, and they are never collapsed
into a family. A demand profile built over a forced assignment would be a
confident number computed from a guess, which is worse than no number.

EVERY RULE IS EVALUATED. There is no first-match anywhere in this module: the
earlier prototype took the first matching rule and so reported a single tidy
family for titles that named two, hiding the ambiguity instead of surfacing it.
`matched` carries every (family, pattern) pair that fired, so any assignment can
be reproduced and argued with.

SENIORITY IS PARSED SEPARATELY and never contributes to the family. Treating
"Manager" as a family is what produced an earlier "Mgmt" bucket holding both
engineering and product managers -- two different demand profiles averaged into
one meaningless one.

NO LLM. The rules are a reviewable file. A model may propose additions to
rules.yaml for a human to merge; nothing here calls one, so a classification is
always reproducible from the title plus the file.

Run `python -m roles.classify` for the coverage report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from skills.extractor import normalize

PATH = Path(__file__).parent / "rules.yaml"

UNKNOWN = "UNKNOWN"
AMBIGUOUS = "AMBIGUOUS"

#: Evidence tiers. ROLE names the job; QUALIFIER names the stack it is built on.
ROLE = "role"
QUALIFIER = "qualifier"

#: Bumped when rules.yaml changes in a way that moves assignments. Stored
#: beside each classification so a row can be tied to the rules that produced it.
#: v2 introduced the role/qualifier tiers.
METHOD = "rules:v2"


class RoleRulesError(RuntimeError):
    """rules.yaml is missing or malformed."""


@dataclass(frozen=True)
class Classification:
    """One title's verdict, with everything needed to reproduce it."""

    family: str                                   # a family, UNKNOWN or AMBIGUOUS
    matched: tuple[tuple[str, str, str], ...]     # every (family, pattern, tier)
    seniority: str | None                         # None when the title says nothing
    #: The title EXACTLY as the crawler saw it. Kept so an assignment can be
    #: audited against its real input: `normalize` casefolds and NFKC-folds, and
    #: a reviewer checking a surprising row needs the original, not the key.
    title: str | None = None
    method: str = METHOD

    @property
    def is_usable(self) -> bool:
        """True only for a real family: what a demand profile may be built on."""
        return self.family not in (UNKNOWN, AMBIGUOUS)


def _load(path: Path | None = None) -> dict:
    p = Path(path or PATH)
    if not p.exists():
        raise RoleRulesError(f"{p} is missing")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise RoleRulesError(f"{p}: top level must be a mapping")

    unknown = set(doc) - {"families", "seniority"}
    if unknown:
        raise RoleRulesError(f"{p}: unknown section(s) {sorted(unknown)}")

    families: dict[str, dict[str, list[str]]] = {}
    seen: dict[str, str] = {}
    for fam, spec in (doc.get("families") or {}).items():
        if fam in (UNKNOWN, AMBIGUOUS):
            raise RoleRulesError(f"{p}: {fam!r} is a reserved outcome, not a family")
        # A bare list is shorthand for "all of these are role patterns", which
        # is what most families are; only the ones whose postings are named by a
        # language need the two-tier form.
        if isinstance(spec, list):
            spec = {ROLE: spec}
        if not isinstance(spec, dict):
            raise RoleRulesError(f"{p}: family {fam!r} must be a list or a mapping")
        bad = set(spec) - {ROLE, QUALIFIER}
        if bad:
            raise RoleRulesError(f"{p}: family {fam!r} has unknown tier(s) {sorted(bad)}")
        tiers: dict[str, list[str]] = {ROLE: [], QUALIFIER: []}
        for tier in (ROLE, QUALIFIER):
            for pat in (spec.get(tier) or []):
                key = normalize(pat)
                if not key:
                    raise RoleRulesError(f"{p}: family {fam!r} has an empty pattern")
                if key in seen:
                    # Otherwise the pattern makes every title carrying it
                    # AMBIGUOUS between two families, which is a rule bug rather
                    # than real ambiguity in the title.
                    raise RoleRulesError(
                        f"{p}: pattern {pat!r} is in both {seen[key]!r} and {fam!r}"
                    )
                seen[key] = fam
                tiers[tier].append(key)
        if not tiers[ROLE] and not tiers[QUALIFIER]:
            raise RoleRulesError(f"{p}: family {fam!r} has no patterns")
        families[fam] = tiers

    seniority: dict[str, list[str]] = {}
    for level, pats in (doc.get("seniority") or {}).items():
        seniority[level] = [normalize(x) for x in (pats or []) if normalize(x)]
    return {"families": families, "seniority": seniority}


_RULES: dict | None = None


def _rules() -> dict:
    global _RULES
    if _RULES is None:
        _RULES = _load()
    return _RULES


#: Seniority is reported at the HIGHEST marker present, so "Senior Engineering
#: Manager" is manager, not senior. Order is most senior first.
_SENIORITY_ORDER = ("manager", "lead", "senior", "middle", "junior", "intern")


def classify(title: str | None) -> Classification:
    """The family a title names, plus every rule that fired and its seniority."""
    key = normalize(title)
    if not key:
        return Classification(UNKNOWN, (), None)

    rules = _rules()
    matched: list[tuple[str, str, str]] = []
    for fam, tiers in rules["families"].items():
        for tier in (ROLE, QUALIFIER):
            for pat in tiers[tier]:
                if pat in key:
                    matched.append((fam, pat, tier))

    # Role patterns decide. Qualifiers are consulted ONLY when no role pattern
    # matched anywhere -- a language names the stack, not the job, so it must
    # never outrank a title that says what the job is. Both tiers are always
    # evaluated and both are always reported in `matched`; what the tier changes
    # is which set of hits gets to pick, never whether a rule was checked.
    deciding = {fam for fam, _, tier in matched if tier == ROLE}
    if not deciding:
        deciding = {fam for fam, _, _ in matched}

    if not deciding:
        family = UNKNOWN
    elif len(deciding) == 1:
        family = next(iter(deciding))
    else:
        family = AMBIGUOUS

    seniority = None
    for level in _SENIORITY_ORDER:
        if any(pat in key for pat in rules["seniority"].get(level, ())):
            seniority = level
            break

    return Classification(family, tuple(sorted(matched)), seniority, title)


#: A family below this many postings does not get a demand profile. Demand
#: share is a proportion, and a proportion over a handful of postings is noise
#: dressed as a number: at N=20 a single posting moves any skill's share by 5
#: points. 50 is a convention, not a validated constant -- it is the point where
#: one posting moves a share by 2 points, which is below the resolution anyone
#: should read into these figures. Override per call site; do not hardcode.
MIN_FAMILY_POSTINGS = 50


def report(titles, min_postings: int = MIN_FAMILY_POSTINGS, skill_counts=None) -> str:
    """Coverage over real titles, plus what still needs a rule.

    `titles` is any iterable of raw title strings. `skill_counts`, when given,
    is a parallel iterable of how many canonical skills each posting carries.

    Posting count and SKILL coverage are reported as two separate columns on
    purpose. A family can clear the posting threshold while most of its postings
    name no skill at all -- IT Support is the live example, 32 postings of which
    only 19 carry any skill -- and a demand profile built on the posting count
    alone would not show that. Qualifying needs BOTH.
    """
    import collections

    by_family = collections.Counter()
    with_skills = collections.Counter()
    skill_edges = collections.Counter()
    by_seniority = collections.Counter()
    unknown = collections.Counter()
    ambiguous = collections.Counter()
    total = 0

    counts = list(skill_counts) if skill_counts is not None else None
    for i, t in enumerate(titles):
        total += 1
        c = classify(t)
        by_family[c.family] += 1
        by_seniority[c.seniority] += 1
        if counts is not None and c.is_usable:
            n = counts[i] if i < len(counts) else 0
            if n:
                with_skills[c.family] += 1
                skill_edges[c.family] += n
        if c.family == UNKNOWN:
            unknown[normalize(t)] += 1
        elif c.family == AMBIGUOUS:
            ambiguous[tuple(sorted({f for f, _, _ in c.matched}))] += 1

    total = total or 1
    usable = sum(n for f, n in by_family.items() if f not in (UNKNOWN, AMBIGUOUS))
    lines = [
        f"role classification ({METHOD}): {total} titles",
        f"  classified to a family   {usable:5}  ({100*usable/total:4.1f}%)",
        f"  AMBIGUOUS                {by_family[AMBIGUOUS]:5}  ({100*by_family[AMBIGUOUS]/total:4.1f}%)",
        f"  UNKNOWN                  {by_family[UNKNOWN]:5}  ({100*by_family[UNKNOWN]/total:4.1f}%)",
        "",
        f"  per family (>= {min_postings} postings qualifies for a demand profile):",
    ]
    if counts is None:
        for fam, n in by_family.most_common():
            if fam in (UNKNOWN, AMBIGUOUS):
                continue
            flag = "ok" if n >= min_postings else "TOO SMALL"
            lines.append(f"    {fam:20} {n:5}  {flag}")
    else:
        lines.append(f"    {'family':20} {'postings':>8} {'w/skills':>9} {'cover':>6} {'edges':>7}  verdict")
        for fam, n in by_family.most_common():
            if fam in (UNKNOWN, AMBIGUOUS):
                continue
            w = with_skills[fam]
            # BOTH counts must clear the bar: postings say the family exists,
            # postings-with-skills say a demand profile can be computed from it.
            ok = n >= min_postings and w >= min_postings
            lines.append(
                f"    {fam:20} {n:8} {w:9} {100*w/n:5.0f}% {skill_edges[fam]:7}  "
                f"{'usable' if ok else 'TOO SMALL'}"
            )

    lines.append("")
    lines.append("  seniority (parsed independently of family):")
    for level, n in by_seniority.most_common():
        lines.append(f"    {str(level):20} {n:5}")

    if ambiguous:
        lines.append("")
        lines.append("  AMBIGUOUS by family pair — each is a rule to sharpen or a real overlap:")
        for combo, n in ambiguous.most_common(10):
            lines.append(f"    {n:5}  {' + '.join(combo)}")

    if unknown:
        lines.append("")
        lines.append("  UNKNOWN titles by frequency — the curation queue:")
        for t, n in unknown.most_common(20):
            lines.append(f"    {n:5}  {t[:70]}")
    return "\n".join(lines)


def main() -> None:
    """Coverage against the live job corpus."""
    import os

    import psycopg

    with psycopg.connect(os.environ["POSTGRES_DSN"]) as pg, pg.cursor() as cur:
        cur.execute("SELECT title FROM jobs")
        titles = [r[0] for r in cur.fetchall()]
    print(report(titles))


if __name__ == "__main__":
    main()
