"""Deterministic skill extraction: dictionary match, nothing invented.

Two passes, in order of trust:

  1. `tech_stack` tags — the site's own labels. 96% are already under 40 bytes
     and look like "Python", "AWS", "CI/CD". Matched EXACTLY (after
     normalisation), so a tag is either a known skill or dropped.
  2. free text — the description and the requirement bullets, scanned for
     dictionary terms with explicit boundaries.

Anything not in the dictionary is discarded. That is the point: the previous
behaviour stored whatever the parser put in `skills`, which is how recruitment
copy like "Ai Yêu Miền Bắc hơn MBers?" became a skill.
"""

from __future__ import annotations

import re
import unicodedata

from .dictionary import CANONICAL, EXCLUDED

# Terms too short or too common to scan for inside prose. "Go" matches the verb,
# "C" matches an initial, "ML" matches a unit. They are still recognised when a
# tech_stack tag says exactly that, where the site has already vouched for them.
EXACT_MATCH_ONLY = {"c", "go", "r", "ml", "bi", "cv", "js", "ts", "py", "k8", "sap", "erp", "crm"}

# Leading list markers and trailing punctuation the parsers carry over.
_LEAD = re.compile(r"^[\s\-–—*•·+>\d.)\]]+")
_TRAIL = re.compile(r"[\s:;,.…]+$")
_WS = re.compile(r"\s+")

# A term may contain or end in +, # or . (C++, C#, .NET, Node.js), so the usual
# \b is wrong: \b after "+" never matches. Hand-built edges instead.
#   LEFT  not mid-token, and not the tail of a dotted name  -> ".NET" must not
#         match inside "ASP.NET"
#   RIGHT not mid-token, and not followed by ".<word>"      -> "Node" must not
#         match inside "Node.js", while "CI/CD." at the end of a sentence still
#         matches, because there the dot is followed by a space or nothing.
_LEFT = r"(?<![A-Za-z0-9+#])(?<!\w\.)"
_RIGHT = r"(?![A-Za-z0-9+#])(?!\.\w)"


def normalize(value: str | None) -> str:
    """Casefold and strip decoration so 'PostgreSql' and ' postgres. ' agree."""
    if not value:
        return ""
    # NFKC first: the crawlers see full-width and composed forms in Vietnamese
    # pages, and 'ReactJS' must not depend on which form the page used.
    text = unicodedata.normalize("NFKC", str(value))
    text = _WS.sub(" ", text).strip()
    text = _LEAD.sub("", text)
    text = _TRAIL.sub("", text)
    return text.casefold().strip()


def _build_lookup() -> dict[str, str]:
    """{normalised term: canonical display name} for every name and alias."""
    table: dict[str, str] = {}
    for canonical, aliases in CANONICAL.items():
        for term in (canonical, *aliases):
            key = normalize(term)
            if key:
                table.setdefault(key, canonical)
    return table


LOOKUP = _build_lookup()


def _build_scanner() -> list[tuple[re.Pattern[str], str]]:
    """Patterns for the free-text pass, longest term first.

    Longest-first matters: without it "Spring" would claim the text of
    "Spring Boot" and the more specific skill would be lost.
    """
    terms = [(t, c) for t, c in LOOKUP.items() if len(t) >= 3 and t not in EXACT_MATCH_ONLY]
    terms.sort(key=lambda tc: len(tc[0]), reverse=True)
    return [
        (re.compile(rf"{_LEFT}{re.escape(term)}{_RIGHT}", re.IGNORECASE), canonical)
        for term, canonical in terms
    ]


SCANNER = _build_scanner()


def match_exact(value: str | None) -> str | None:
    """Canonical name for one tag, or None when unknown or deliberately excluded."""
    key = normalize(value)
    if not key or key in EXCLUDED:
        return None
    return LOOKUP.get(key)


def extract_skills(tech_stack=None, description=None, requirements=None) -> list[str]:
    """Canonical technical skills for one job, in a stable order.

    `requirements` is the old `skills` array — requirement bullets. It is read
    as TEXT to scan, never trusted as a list of skill names.
    """
    found: dict[str, None] = {}  # ordered set

    for tag in tech_stack or []:
        canonical = match_exact(tag)
        if canonical:
            found[canonical] = None

    blob = "\n".join(str(p) for p in [description, *(requirements or [])] if p)
    if blob:
        # Consume matched spans so a shorter term cannot re-claim text a longer
        # one already took: without this, "Spring Boot" also yields "Spring",
        # and the job gains a skill nobody wrote.
        consumed: list[tuple[int, int]] = []
        for pattern, canonical in SCANNER:
            for m in pattern.finditer(blob):
                if any(m.start() < end and m.end() > start for start, end in consumed):
                    continue
                consumed.append((m.start(), m.end()))
                found.setdefault(canonical, None)
                break

    return list(found)
