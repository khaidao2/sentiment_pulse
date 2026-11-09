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
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urlsplit

from api_crawler.shared.ports.parser import IParser

_LOG = logging.getLogger(__name__)

# Microsoft's three labels map onto the schema's bands one-for-one. Anything
# else stays UNKNOWN — a wrong level puts a course at the wrong step of a path,
# which is worse than an absent one.
_MS_LEVEL = {"beginner": "INTRO", "intermediate": "WORKING", "advanced": "DEEP"}

# Microsoft publishes the discriminator itself, so this is a read, not a guess.
# Flattening the two into one shape made a 102-minute learning path and the
# 65-minute module inside it look like the same kind of thing.
_MS_KIND = {"module": "MODULE", "learningPath": "LEARNING_PATH"}

# `[title](url)`, lazy so a title holding its own brackets -- "[Python], UC
# Berkeley" -- is not cut at the inner one: an inner `]` is not followed by
# `(http`, so the match cannot end there.
_MD_LINK = re.compile(r"\[(.+?)\]\((https?://[^)\s]+)\)")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _course_id(url: str) -> str:
    """SHA-256 of the url. The url IS the identity: providers reuse titles."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _record(**kw) -> dict:
    """Fill the schema's defaults so every record is complete for Avro."""
    base = {
        "summary": None, "tags": [], "language": "en", "is_free": True,
        "level": "UNKNOWN", "duration_minutes": None, "license_cc": None,
        "crawled_at": _now(),
        # Hierarchy: absent by default, so a source that publishes none says so
        # rather than implying a flat catalogue is a one-step path.
        "uid": None, "content_kind": "UNKNOWN", "child_uids": [],
        "child_count": None, "first_child_url": None, "parent_uids": [],
        # Duration: source-provided only. duration_is_derived stays False
        # because nothing here estimates.
        "duration_text": None, "duration_weeks_min": None,
        "weekly_hours_min": None, "duration_is_derived": False,
    }
    base.update(kw)
    base["id"] = _course_id(base["url"])
    return base


class CoursesParser(IParser):

    def parse(self, raw: str) -> Iterable[dict]:
        """Dispatch on the envelope's source; the handler decodes its own page.

        The page is handed over as the RAW string rather than pre-decoded JSON,
        because csvideo's page is markdown. Decoding here would mean this method
        knowing which dialects happen to be JSON, which is the one fact the
        envelope exists to keep out of it.
        """
        envelope = json.loads(raw)
        source, page = envelope["source"], envelope["page"]
        handler = {
            "mslearn": self._mslearn,
            "mitlearn": self._mitlearn,
            "freecodecamp": self._freecodecamp,
            "csvideo": self._csvideo,
        }.get(source)
        if handler is None:
            _LOG.warning("no parser for source %r — page dropped", source)
            return []
        return handler(page)

    # ── per-catalogue normalisers ─────────────────────────────────────────
    def _mslearn(self, page: str) -> Iterable[dict]:
        """Modules and learning paths, kept distinct.

        Two passes, because parent_uids may only name a container that is
        actually in this crawl. The catalogue is paged and capped, so a path
        routinely lists modules that were never fetched; inventing an edge for
        those would fabricate a curriculum. An unresolved child simply gets no
        edge, and child_count vs len(child_uids) is what records the gap.
        """
        cat = json.loads(page)
        records: list[dict] = []
        by_uid: dict[str, dict] = {}

        for kind in ("modules", "learningPaths"):
            for r in cat.get(kind, []):
                url = (r.get("url") or "").split("?")[0]  # drop WT.mc_id tracking
                if not url:
                    continue
                levels = r.get("levels") or []
                # A path lists `modules`, a module lists `units`. Both are
                # ordered by the publisher and stored verbatim -- index 0 is
                # step 1. Units are reference-only: the catalogue exposes no
                # units array, so they never become rows of their own.
                children = [c for c in (r.get("modules") or r.get("units") or []) if c]
                first_child = (r.get("firstModuleUrl") or r.get("firstUnitUrl") or "")
                rec = _record(
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
                    uid=r.get("uid") or None,
                    content_kind=_MS_KIND.get(r.get("type"), "UNKNOWN"),
                    child_uids=children,
                    child_count=r.get("number_of_children"),
                    first_child_url=first_child.split("?")[0] or None,
                )
                records.append(rec)
                if rec["uid"]:
                    by_uid[rec["uid"]] = rec

        for rec in records:
            if rec["content_kind"] != "LEARNING_PATH" or not rec["uid"]:
                continue
            for child in rec["child_uids"]:
                target = by_uid.get(child)
                if target is not None and rec["uid"] not in target["parent_uids"]:
                    target["parent_uids"].append(rec["uid"])

        return records

    def _mitlearn(self, raw: str) -> Iterable[dict]:
        """One page of free MIT Learn courses."""
        page = json.loads(raw)
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
                # MIT is a standalone course, not a container.
                content_kind="COURSE",
                # MIT DOES publish duration -- in weeks plus a weekly
                # commitment, which an int-minutes field cannot hold. Keeping
                # the raw string and the two numbers beats dropping all three:
                # 6 weeks x 5 h/week is only effort if you assume a study rate
                # MIT never states, so no conversion happens here.
                duration_text=r.get("duration") or r.get("time_commitment") or None,
                duration_weeks_min=r.get("min_weeks"),
                weekly_hours_min=r.get("min_weekly_hours"),
            )

    def _freecodecamp(self, raw: str) -> Iterable[dict]:
        """Certifications, from the curriculum repo's superblock file names.

        The human-language courses (a1-professional-chinese and friends) are
        kept: dropping them is the dictionary's call downstream, where ROLES and
        LANGUAGES are already recorded exclusions, not a second filter here.
        """
        for f in json.loads(raw):
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
                # A certification is standalone here: the superblock file names
                # carry no ordering and no uid, so there is no hierarchy to
                # record and none is invented.
                content_kind="COURSE",
            )

    def _csvideo(self, page: str) -> Iterable[dict]:
        """cs-video-courses' README -> one record per course link.

        The dialect is markdown, so the structure IS the document: a `### `
        heading opens a subject section and every `- ` bullet under it is one
        course. The table of contents is skipped without special-casing it,
        because its bullets link to `#anchor` fragments and the pattern below
        only accepts http(s).

        Two shapes of bullet, both in the file:
          - [<title>](<url>)                      most entries
          - <title> | [Video Playlist](<url>)     title outside the link
        The leading text wins when there is any, because that is where the
        curator put the course's identity; the link text in the second shape is
        a label ("Video Playlist"), not a title.

        THE QUERY STRING IS KEPT, unlike mslearn where it is stripped. 674 of
        these urls are youtube.com/playlist?list=..., where the parameter IS the
        resource: stripping it would collapse every YouTube entry onto one id.

        A url listed in several sections yields ONE record carrying both
        sections as tags, not two rows -- the url is the identity, so a
        duplicate would be the same course twice under two ids.
        """
        records: list[dict] = []
        by_url: dict[str, dict] = {}
        section: str | None = None

        for line in page.splitlines():
            if line.startswith("### "):
                section = line[4:].strip()
                continue
            stripped = line.lstrip()
            # A bullet before the first `### ` is front matter or the contents
            # list, and belongs to no subject.
            if section is None or not stripped.startswith("- "):
                continue
            body = stripped[2:].strip()
            match = _MD_LINK.search(body)
            if match is None:
                continue
            # Trailing sentence punctuation is the curator's, not the url's.
            url = match.group(2).rstrip(".,")
            lead = body[: match.start()].strip(" -|\t")
            title = lead or match.group(1).strip()
            if not title:
                continue

            seen = by_url.get(url)
            if seen is not None:
                if section not in seen["tags"]:
                    seen["tags"].append(section)
                continue
            record = _record(
                source="csvideo",
                # A BOOKMARK LIST HAS NO PROVIDER. The host is the one
                # provider fact the entry actually carries; the university is
                # usually in the title too, but only usually, and parsing it
                # out would be the guess this parser refuses elsewhere.
                provider=urlsplit(url).netloc.lower().removeprefix("www."),
                title=title,
                url=url,
                # The repo's premise is freely available video lectures
                # (NOTES.md), which is the same claim is_free makes. No entry
                # states a level, a duration or a licence, so all three stay at
                # their absent defaults rather than being inferred.
                tags=[section],
                # Standalone: the list records no ordering and no container,
                # so there is no hierarchy to carry.
                content_kind="COURSE",
            )
            by_url[url] = record
            records.append(record)

        return records
