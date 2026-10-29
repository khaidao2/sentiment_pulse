"""TopCV source: walk listing pages, then yield each job's DETAIL page HTML.

Same shape as the itviec source — a two-level crawl (listing -> detail) is
navigation, which is source-specific, so it lives behind this per-source
ISource adapter. The parser stays single-purpose: one detail page -> one record.

Uses curl_cffi (browser TLS impersonation) because TopCV sits behind bot
protection, and paces requests with a delay so a larger crawl doesn't get 403'd.
Detail URLs are stripped of their tracking query string (`ta_source`,
`u_sr_id`, ...) so the URL we fetch and key on equals the page's canonical URL.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from api_crawler.shared.ports.source import ISource

_LOG = logging.getLogger(__name__)
_BASE = "https://www.topcv.vn"


class TopcvSource(ISource):
    def __init__(
        self,
        listing_url: str = f"{_BASE}/tim-viec-lam-cong-nghe-thong-tin-cr257",
        max_pages: int = 10,
        start_page: int = 1,
        impersonate: str = "chrome",
        timeout: float = 30.0,
        delay: float = 4.0,
        max_retries: int = 3,
    ) -> None:
        self._listing_url = listing_url
        self._max_pages = max_pages
        self._start_page = start_page
        self._impersonate = impersonate
        self._timeout = timeout
        self._delay = delay
        self._max_retries = max_retries
        self._headers = {
            "Accept-Language": "vi,en;q=0.9",
            # TopCV 403s a detail fetch that arrives without a same-site
            # referer, so present the listing we came from.
            "Referer": listing_url,
            "Upgrade-Insecure-Requests": "1",
        }

    def fetch(self) -> Iterable[str]:
        """Yield the raw HTML of every job detail page across the listing pages."""
        with cffi.Session(impersonate=self._impersonate, headers=self._headers) as session:
            for page in range(self._start_page, self._start_page + self._max_pages):
                # Listing pages were getting throttled as hard as details, so
                # they are paced on the same clock.
                time.sleep(self._delay)
                listing = self._get(session, self._listing_url, params={"page": page})
                if listing is None:
                    continue
                urls = self._detail_urls(listing)
                if not urls:
                    # No job cards on this page → we've walked past the last one.
                    # This is the real stop condition; max_pages is only an upper
                    # safety bound.
                    _LOG.info("listing page=%d empty → stop", page)
                    break
                _LOG.info("listing page=%d | %d detail urls", page, len(urls))
                for url in urls:
                    time.sleep(self._delay)  # be polite — avoid rate limiting
                    detail = self._get(session, url)
                    if detail is not None:
                        yield detail

    def _detail_urls(self, listing_html: str) -> list[str]:
        """Extract each job's canonical detail URL from the listing cards.

        Only `div.job-item-search-result` cards are real search hits — the page
        carries other `data-job-id` blocks (suggested/related boxes) we must not
        follow.
        """
        soup = BeautifulSoup(listing_html, "html.parser")
        urls: list[str] = []
        for card in soup.select("div.job-item-search-result"):
            a = card.select_one("h3.title a")
            href = a.get("href") if a else None
            if not href:
                continue
            urls.append(self._canonical(urljoin(_BASE, href)))
        return list(dict.fromkeys(urls))  # dedupe, keep order

    @staticmethod
    def _canonical(url: str) -> str:
        """Drop query + fragment: listing links carry per-session tracking params."""
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    # Statuses worth waiting out. 429 and 5xx are the server asking for time.
    # 403 is included because TopCV uses it for throttling as well as refusal:
    # the same URL, fetched the same way with no change of identity, returns 200
    # again after a pause. Waiting longer and asking again is the polite reading;
    # we never rotate the user agent, impersonation profile or anything else to
    # look like a different client, which would be evading the block rather than
    # respecting it.
    _RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})

    def _get(self, session, url: str, params: dict | None = None) -> str | None:
        """Fetch a page, backing off when the server asks us to.

        Returns the body, or None once the retries are spent — the caller logs
        and skips, so one stubborn URL never aborts the crawl. Without this the
        first 429 dropped the page outright: a run on 2026-10-29 published 47
        records out of ~250 listed because every throttled fetch was discarded
        on the spot.
        """
        for attempt in range(self._max_retries + 1):
            try:
                resp = session.get(url, params=params, timeout=self._timeout)
            except Exception as e:  # curl_cffi raises its own error types
                _LOG.warning("fetch error | url=%s | err=%s", url, e)
                return None

            if resp.status_code < 400:
                return resp.text

            if resp.status_code not in self._RETRY_STATUSES or attempt == self._max_retries:
                _LOG.warning("fetch failed | url=%s | status=%s", url, resp.status_code)
                return None

            # Honour Retry-After when the server sends one; otherwise exponential
            # backoff off the configured delay, with jitter so a batch of retries
            # does not come back in lockstep.
            wait = self._retry_after(resp) or self._delay * (2 ** attempt)
            wait += random.uniform(0, self._delay / 2)
            _LOG.info(
                "throttled | url=%s | status=%s | backoff=%.1fs | attempt=%d/%d",
                url, resp.status_code, wait, attempt + 1, self._max_retries,
            )
            time.sleep(wait)
        return None

    @staticmethod
    def _retry_after(resp) -> float | None:
        """Seconds from a Retry-After header, if it carries a usable value."""
        raw = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
        if not raw:
            return None
        try:
            # Only the delta-seconds form; an HTTP-date would need parsing and
            # TopCV does not send one.
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            return None
