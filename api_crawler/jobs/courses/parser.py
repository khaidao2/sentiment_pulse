"""Courses parser: one catalogue page -> canonical Course records.

Shapes the three dialects into data-contracts/schemas/learning/courses.avsc.
Every field is a FACT or a LINK — title, provider, url, language, level,
duration, free, licence — plus the provider's own `summary` and `tags`, which
exist because the downstream matcher needs text to scan. The public catalogue
drops the summary; see the schema's field doc.

NO SKILL MATCHING HAPPENS HERE, on purpose. The canonical dictionary lives in
reverse_etl/skills, next to the job index it was tuned against, and the crawler
image has no business carrying it: a crawler that guessed at skills would be a
second place where "what counts as a skill" is decided. This parser's whole job
is to make three catalogues look like one table.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Iterable

from api_crawler.shared.ports.parser import IParser

_LOG = logging.getLogger(__name__)

# Microsoft's three labels map onto the schema's bands one-for-one. Anything
# else stays UNKNOWN — a wrong level puts a course at the wrong step of a path,
# which is worse than an absent one.
_MS_LEVEL = {"beginner": "INTRO", "intermediate": "WORKING", "advanced": "DEEP"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _course_id(url: str) -> str:
    """SHA-256 of the url. The url IS the identity: providers reuse titles."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _record(**kw) -> dict:
    """Fill the schema's defaults so every record is complete for Avro."""
    base = {
        "summary": None, "tags": [], "language": "en", "is_free": True,
        "level": "UNKNOWN", "duration_minutes": None, "license_cc": None,
        "crawled_at": _now(),
    }
    base.update(kw)
    base["id"] = _course_id(base["url"])
    return base


class CoursesParser(IParser):

    def parse(self, raw: str) -> Iterable[dict]:
        envelope = json.loads(raw)
        source, page = envelope["source"], envelope["page"]
        handler = {
            "mslearn": self._mslearn,
            "mitlearn": self._mitlearn,
            "freecodecamp": self._freecodecamp,
        }.get(source)
        if handler is None:
            _LOG.warning("no parser for source %r — page dropped", source)
            return []
        return handler(json.loads(page))

    # ── per-catalogue normalisers ─────────────────────────────────────────
    def _mslearn(self, cat: dict) -> Iterable[dict]:
        """Modules and learning paths. All Microsoft Learn training is free."""
        for kind in ("modules", "learningPaths"):
            for r in cat.get(kind, []):
                url = (r.get("url") or "").split("?")[0]  # drop WT.mc_id tracking
                if not url:
                    continue
                levels = r.get("levels") or []
                yield _record(
                    source="mslearn",
                    provider="Microsoft Learn",
                    title=r["title"],
                    url=url,
                    summary=r.get("summary") or None,
                    # Product and role slugs are hyphenated; the downstream
                    # extractor scans words, so hand it words.
                    tags=[t.replace("-", " ")
                          for t in (r.get("products") or []) + (r.get("roles") or [])],
                    language=(r.get("locale") or "en-us").split("-")[0],
                    level=_MS_LEVEL.get(levels[0], "UNKNOWN") if levels else "UNKNOWN",
                    duration_minutes=r.get("duration_in_minutes"),
                )

    def _mitlearn(self, page: dict) -> Iterable[dict]:
        """One page of free MIT Learn courses."""
        for r in page.get("results", []):
            url = r.get("url") or r.get("learn_url")
            if not url:
                continue
            langs = r.get("languages") or ["en"]
            yield _record(
                source="mitlearn",
                # MIT Learn's one API serves several providers (OCW, MITx,
                # xPRO), so provider comes from the payload, not the source.
                provider=(r.get("platform") or {}).get("name") or "MIT",
                title=r["title"],
                url=url,
                summary=r.get("description") or None,
                tags=[t["name"] for t in (r.get("topics") or []) if t.get("name")],
                language=(langs[0] or "en").split("-")[0],
                is_free=bool(r.get("free", True)),
                license_cc=r.get("license_cc"),
            )

    def _freecodecamp(self, listing: list) -> Iterable[dict]:
        """Certifications, from the curriculum repo's superblock file names.

        The human-language courses (a1-professional-chinese and friends) are
        kept: dropping them is the dictionary's call downstream, where ROLES and
        LANGUAGES are already recorded exclusions, not a second filter here.
        """
        for f in listing:
            name = f.get("name", "")
            if not name.endswith(".json"):
                continue
            slug = name.removesuffix(".json")
            words = slug.replace("-", " ")
            yield _record(
                source="freecodecamp",
                provider="freeCodeCamp",
                title=words.title(),
                url=f"https://www.freecodecamp.org/learn/{slug}/",
                summary=None,
                tags=[words],
            )
