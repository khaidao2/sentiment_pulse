"""Courses source: pull raw pages from the open course catalogues.

Three catalogues, chosen by their terms rather than their size. Each one is
recorded here with the check that let it in, because the next person to add a
source needs to make the same check and not guess:

  mslearn — Microsoft Learn Catalog API. Public, no authentication, no charge,
    under the Microsoft APIs Terms of Use, and its documented use case is
    exactly ours: "pull a list of the training offerings in order to link to
    them from your own online experience". robots.txt disallows only /answers/.
    https://learn.microsoft.com/en-us/training/support/catalog-api
    The whole catalogue is ONE ~14 MB JSON document, so there is no pagination
    and no delay to apply. Microsoft's docs say this endpoint is planned for
    deprecation after June 2026 in favour of the Learn Platform API; as of
    2026-10-31 it still serves, and a 404 here is the signal to migrate.

  mitlearn — MIT Learn API over OCW, MITx and the Open Learning Library, with
    explicit per-course `free` and `license_cc` flags. We ask for free only.
    https://api.learn.mit.edu/api/v1/courses/

  freecodecamp — the BSD-3-Clause curriculum repository. The superblock file
    names ARE the public curriculum paths, so the catalogue is derived from the
    repo rather than scraped off the site. robots.txt is `Allow: /`.
    https://github.com/freeCodeCamp/freeCodeCamp

DELIBERATELY EXCLUDED — F8 / fullstack.edu.vn, the obvious Vietnamese free
source and the one that would most improve coverage. Its robots.txt sets
`Content-Signal: ai-train=no, use=reference` and names ClaudeBot, anthropic-ai,
Claude-Web, GPTBot and CCBot under `Disallow: /`. An agent crawling it on a
user's behalf is what that forbids, so it is not crawled. A person may read it
and hand-add rows to the curated catalogue — the "reference" use the signal
does allow.

Each page is yielded as an ENVELOPE: {"source": ..., "page": <raw payload>}.
ISource is typed `Iterable[str]` and the parser has to know which dialect it is
reading, so the envelope carries that one fact instead of widening both ports.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Iterable

from api_crawler.shared.ports.source import ISource

_LOG = logging.getLogger(__name__)

USER_AGENT = "sentiment-pulse-crawler/1.0 (course catalogue for a job-market index)"
_TIMEOUT = 120.0

MSLEARN_URL = "https://learn.microsoft.com/api/catalog/"
MITLEARN_URL = "https://api.learn.mit.edu/api/v1/courses/?free=true&limit=100"
FCC_URL = (
    "https://api.github.com/repos/freeCodeCamp/freeCodeCamp"
    "/contents/curriculum/structure/superblocks"
)


class CoursesSource(ISource):
    def __init__(self, sources: list[str], delay: float = 0.2, max_pages: int = 40) -> None:
        self._sources = sources
        self._delay = delay
        self._max_pages = max_pages

    # ── transport ─────────────────────────────────────────────────────────
    def _get(self, url: str) -> str:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8")

    @staticmethod
    def _envelope(source: str, page: str) -> str:
        return json.dumps({"source": source, "page": page})

    # ── per-catalogue fetchers ────────────────────────────────────────────
    def _mslearn(self) -> Iterable[str]:
        yield self._envelope("mslearn", self._get(MSLEARN_URL))

    def _mitlearn(self) -> Iterable[str]:
        url, pages = MITLEARN_URL, 0
        while url and pages < self._max_pages:
            raw = self._get(url)
            yield self._envelope("mitlearn", raw)
            pages += 1
            url = json.loads(raw).get("next")
            if url:
                time.sleep(self._delay)
        if url:
            _LOG.warning("mitlearn: stopped at the %d-page ceiling, more remain", pages)

    def _freecodecamp(self) -> Iterable[str]:
        yield self._envelope("freecodecamp", self._get(FCC_URL))

    # ── ISource ───────────────────────────────────────────────────────────
    def fetch(self) -> Iterable[str]:
        """Yield every catalogue's raw pages.

        One dead catalogue must not cost the others their run: a fetch error is
        logged and that source is skipped, so a Microsoft outage still lets MIT
        and freeCodeCamp land. A run that fetched nothing at all publishes
        nothing, and the DAG's own retry handles it.
        """
        fetchers = {
            "mslearn": self._mslearn,
            "mitlearn": self._mitlearn,
            "freecodecamp": self._freecodecamp,
        }
        for name in self._sources:
            fetcher = fetchers.get(name)
            if fetcher is None:
                _LOG.warning("unknown source %r in SOURCES — skipped", name)
                continue
            _LOG.info("fetching %s", name)
            try:
                pages = 0
                for page in fetcher():
                    pages += 1
                    yield page
                _LOG.info("%s: %d page(s)", name, pages)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
                _LOG.error("%s failed, skipping: %s: %s", name, type(exc).__name__, exc)
