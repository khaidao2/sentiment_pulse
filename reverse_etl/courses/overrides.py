"""The curation gate: a hand-maintained override/deny-list over the matcher.

This is the ONLY hand-written input to course -> skill. It is not a catalogue
and it is not a dictionary. It holds two kinds of correction, both keyed by a
course's url (its identity: `course_id` is sha256(url), and a provider reuses
titles across levels and locales):

  deny    — the title match is wrong for THIS course. "Introduction to R
            Programming for Azure" is not a Go course because "go" appears in
            "Programming"; a curator kills that one edge and the rest stand.
  teaches — the title match is missing for THIS course. MIT publishes
            "6.0001 Introduction to Computer Science and Programming", which
            teaches Python and says so nowhere in the title. A curator asserts
            it, and the course stops being dropped.

Both lists may only name skills already in `skills.dictionary`. An unknown name
is a hard error, not a warning: the point of the gate is to correct a verdict
about a COURSE, never to grow a second vocabulary beside the dictionary. A skill
this market needs belongs in the dictionary, where the job pipeline sees it too.

Overrides are applied AFTER matching and deny wins over teaches, so adding the
same skill to both lists removes it. A url that no longer exists in the
catalogue is reported by `python -m courses.match`, not silently kept.

The file is YAML because a human edits it in review. match.py still emits no
YAML — the earlier "no pyyaml dependency" note there was about machine-writing
a 1200-row catalogue, which this is not.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from skills.dictionary import ALL_SKILLS

PATH = Path(__file__).parent / "overrides.yaml"


class Overrides:
    """{url: {skill}} for each list, plus the urls seen, for staleness reports."""

    def __init__(self, deny: dict[str, set[str]], teaches: dict[str, set[str]]):
        self.deny = deny
        self.teaches = teaches

    @property
    def urls(self) -> set[str]:
        return set(self.deny) | set(self.teaches)

    def apply(self, url: str, teaches: list[str], mentions: list[str]) -> tuple[list[str], list[str]]:
        """Correct one course's verdict. Order is stable and deny is last."""
        added = [s for s in sorted(self.teaches.get(url, ())) if s not in teaches]
        teaches = teaches + added
        # A skill promoted to `teaches` must not also sit in `mentions`: the two
        # relations are exclusive, and the downstream junction keys on both.
        mentions = [s for s in mentions if s not in teaches]
        denied = self.deny.get(url, set())
        return ([s for s in teaches if s not in denied],
                [s for s in mentions if s not in denied])


def _parse_entries(raw, key: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i, entry in enumerate(raw.get(key) or []):
        if not isinstance(entry, dict) or "url" not in entry:
            raise ValueError(f"{PATH.name}: {key}[{i}] needs a `url:`")
        skills = entry.get("skills") or []
        unknown = [s for s in skills if s not in ALL_SKILLS]
        if unknown:
            # Loud, because the alternative is a curation file that quietly
            # becomes a second dictionary nothing else in the repo can see.
            raise ValueError(
                f"{PATH.name}: {key}[{i}] names skill(s) not in skills.dictionary: "
                f"{', '.join(unknown)}. Add them to the dictionary (the job "
                f"pipeline reads it too) or correct the spelling."
            )
        out.setdefault(entry["url"], set()).update(skills)
    return out


def load(path: Path | None = None) -> Overrides:
    """Read the curation file. A missing file is an empty gate, not an error."""
    path = path or PATH
    if not path.exists():
        return Overrides({}, {})
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: top level must be a mapping")
    return Overrides(_parse_entries(raw, "deny"), _parse_entries(raw, "teaches"))
