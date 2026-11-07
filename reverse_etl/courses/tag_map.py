"""Classify a catalogue tag: skill, role, subject, or not yet classified.

Courses carry the provider's own labels in `tags` -- Microsoft publishes
`products` + `roles`, MIT publishes `topics`. They are NOT skills (courses.avsc
says so in the field doc), and the extractor's tag path is exact-match-only, so
multi-word labels like "azure virtual machines" never matched anything. On the
2026-11-02 crawl that left 497 of 521 distinct tags -- 83% of all course-tag
pairs -- doing nothing at all, silently.

This module gives every frequent tag a declared KIND, so the leftovers are a
short reviewable queue rather than undifferentiated noise:

  skill    -> a canonical skill the dictionary already knows
  role     -> a job role ("administrator"); who you are, not what you know
  subject  -> an academic domain ("Mathematics"); a field of study
  None     -> not yet classified; shows up in the coverage report

The hard rule, same as overrides.yaml: a mapped skill must already exist in
`skills.dictionary`. An unknown name raises. The dictionary was seeded from
Vietnamese JOB postings while this catalogue is Microsoft-shaped, so most
product tags have no canonical skill -- and the right answer for those is to
leave them unmapped, not to grow a second vocabulary here. A skill this market
hires for belongs in the dictionary, where the job pipeline sees it too.

Mapped tags are worth `mentions`, never `teaches`. A tag is weaker evidence
than a title: a course tagged `azure` may be about something else that merely
runs on Azure.

Run `python -m courses.tag_map` for coverage and the unclassified queue.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from skills.dictionary import ALL_SKILLS
from skills.extractor import match_exact, normalize

PATH = Path(__file__).parent / "tag_map.yaml"

SKILL = "skill"
ROLE = "role"
SUBJECT = "subject"


class TagMapError(RuntimeError):
    """The table is malformed or names a skill the dictionary does not have."""


def _load(path: Path | None = None) -> dict:
    p = Path(path or PATH)
    if not p.exists():
        raise TagMapError(f"{p} is missing")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise TagMapError(f"{p}: top level must be a mapping")

    unknown = set(doc) - {SKILL, ROLE, SUBJECT}
    if unknown:
        raise TagMapError(f"{p}: unknown section(s) {sorted(unknown)}")

    skills: dict[str, str] = {}
    for tag, name in (doc.get(SKILL) or {}).items():
        if name not in ALL_SKILLS:
            # The whole point of the rule: this file classifies tags, it does
            # not get to invent skills the job side has never heard of.
            raise TagMapError(
                f"{p}: tag {tag!r} maps to {name!r}, which is not in "
                f"skills.dictionary. Add it there first, or leave the tag "
                f"unmapped."
            )
        skills[normalize(tag)] = name

    kinds: dict[str, str] = {}
    for section in (ROLE, SUBJECT):
        for tag in (doc.get(section) or []):
            key = normalize(tag)
            if key in skills:
                raise TagMapError(f"{p}: tag {tag!r} is in both skill and {section}")
            if key in kinds:
                raise TagMapError(f"{p}: tag {tag!r} listed twice")
            if match_exact(tag):
                # Otherwise the table and the dictionary would disagree about
                # the same tag, and classify() would silently prefer one.
                raise TagMapError(
                    f"{p}: tag {tag!r} is listed under {section} but the "
                    f"dictionary already resolves it to {match_exact(tag)!r}"
                )
            kinds[key] = section
    return {"skills": skills, "kinds": kinds}


_TABLE: dict | None = None


def _table() -> dict:
    global _TABLE
    if _TABLE is None:
        _TABLE = _load()
    return _TABLE


def classify(tag: str) -> str | None:
    """SKILL / ROLE / SUBJECT, or None when the tag is not classified.

    The dictionary is consulted first, so this table stays ADDITIVE over it: a
    tag the dictionary already resolves ("azure", "power bi") needs no entry
    here, and adding one would be a second place to maintain the same fact.
    """
    key = normalize(tag)
    if not key:
        return None
    if key in _table()["skills"] or match_exact(tag):
        return SKILL
    return _table()["kinds"].get(key)


def skill_for(tag: str) -> str | None:
    """The canonical skill a tag maps to, or None.

    Falls back to the dictionary, for the same reason classify() does.

    None covers three different situations on purpose -- unmapped, a role, and
    a subject all mean "no skill here". Callers that need to tell them apart
    use classify().
    """
    return _table()["skills"].get(normalize(tag)) or match_exact(tag)


def skills_for(tags) -> list[str]:
    """Canonical skills for a course's tags, in first-seen order, deduped."""
    out: list[str] = []
    for t in tags or []:
        name = skill_for(str(t))
        if name and name not in out:
            out.append(name)
    return out


def report(tag_counts: dict[str, int]) -> str:
    """Coverage over real tag frequencies, plus the unclassified queue.

    `tag_counts` is {tag: number of courses carrying it}, so the queue is
    ordered by how much signal each unclassified tag is actually worth.
    """
    buckets: dict[str | None, list[tuple[str, int]]] = {}
    for tag, n in tag_counts.items():
        buckets.setdefault(classify(tag), []).append((tag, n))
    total = sum(tag_counts.values()) or 1

    lines = [f"tag_map: {len(tag_counts)} distinct tags, {total} course-tag pairs"]
    for kind in (SKILL, ROLE, SUBJECT, None):
        rows = buckets.get(kind, [])
        pairs = sum(n for _, n in rows)
        label = kind or "UNCLASSIFIED"
        lines.append(f"  {label:13} {len(rows):4} tags  {pairs:6} pairs  "
                     f"({100 * pairs // total}%)")

    todo = sorted(buckets.get(None, []), key=lambda r: -r[1])[:25]
    if todo:
        lines.append("")
        lines.append("  unclassified, by how much signal they carry:")
        for tag, n in todo:
            lines.append(f"    {n:5}  {tag}")
    return "\n".join(lines)


def main() -> None:
    """Coverage report against the live catalogue."""
    import os

    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        database=os.environ.get("CLICKHOUSE_DB", "default"),
    )
    rows = client.query(
        "SELECT arrayJoin(tags) AS t, count() FROM silver_courses GROUP BY t"
    ).result_rows
    print(report(dict(rows)))


if __name__ == "__main__":
    main()
