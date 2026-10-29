"""CareerViet source: walk listing pages, then yield each job's DETAIL page HTML.

Same two-level crawl as itviec (listing -> detail); the navigation is
careerviet-specific so it lives here in a per-source ISource adapter, and the
parser stays single-purpose: one detail page -> one record.

Paging: CareerViet category listings are plain HTML pages whose URL carries the
page number before the locale suffix —

    page 1: https://careerviet.vn/viec-lam/cntt-phan-mem-c1-vi.html
    page N: https://careerviet.vn/viec-lam/cntt-phan-mem-c1-trang-N-vi.html

(An earlier crawler of this site used the `POST /search-jobs` XHR endpoint; that
endpoint now 404s, so the HTML listing is the supported path.)

Uses curl_cffi (browser TLS impersonation) like the itviec source, warms the
session on the home page first (CareerViet sets cookies there), and paces the
detail fetches with a small delay so a larger crawl stays polite.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from api_crawler.shared.ports.source import ISource

_LOG = logging.getLogger(__name__)
_BASE = "https://careerviet.vn"

# ".../cntt-phan-mem-c1-vi.html" -> locale suffix we inject "-trang-N" in front of.
_LOCALE_SUFFIX = re.compile(r"-(vi|en)\.html$")
# Job detail pages: /vi/tim-viec-lam/<slug>.<JOB_ID>.html (robots-allowed;
# /vi/jobs/* and /api/* are not).
_DETAIL_PATH = re.compile(r"/(vi|en)/tim-viec-lam/.+\.html$")


class CareerVietSource(ISource):
    def __init__(
        self,
        listing_url: str = f"{_BASE}/viec-lam/cntt-phan-mem-c1-vi.html",
        max_pages: int = 5,
        start_page: int = 1,
        impersonate: str = "chrome",
        timeout: float = 30.0,
        delay: float = 0.5,
    ) -> None:
        self._listing_url = listing_url
        self._max_pages = max_pages
        self._start_page = start_page
        self._impersonate = impersonate
        self._timeout = timeout
        self._delay = delay
        self._headers = {"Accept-Language": "vi,en;q=0.9"}

    def fetch(self) -> Iterable[str]:
        """Yield the raw HTML of every job detail page across the listing pages."""
        with cffi.Session(impersonate=self._impersonate, headers=self._headers) as session:
            # Warm cookies — CareerViet hands them out on the home page and some
            # listing responses are thin without them.
            self._get(session, _BASE + "/")
            seen: set[str] = set()
            for page in range(self._start_page, self._start_page + self._max_pages):
                listing = self._get(session, self._page_url(page))
                if listing is None:
                    continue
                urls = [u for u in self._detail_urls(listing) if u not in seen]
                if not urls:
                    # No (new) jobs on this page → we've walked past the last one.
                    # This is the real "crawl everything" stop; max_pages is only
                    # an upper safety bound.
                    _LOG.info("listing page=%d empty → stop", page)
                    break
                seen.update(urls)
                _LOG.info("listing page=%d | %d detail urls", page, len(urls))
                for url in urls:
                    time.sleep(self._delay)  # be polite — avoid rate limiting
                    detail = self._get(session, url)
                    if detail is not None:
                        yield detail

    def _page_url(self, page: int) -> str:
        """URL of the Nth listing page (page 1 is the bare listing URL)."""
        if page <= 1:
            return self._listing_url
        new, n = _LOCALE_SUFFIX.subn(rf"-trang-{page}-\1.html", self._listing_url)
        if n:
            return new
        # Unknown URL shape → fall back to a ?page= query, harmless if ignored.
        sep = "&" if "?" in self._listing_url else "?"
        return f"{self._listing_url}{sep}page={page}"

    def _detail_urls(self, listing_html: str) -> list[str]:
        """Extract each job's detail URL from the listing cards."""
        soup = BeautifulSoup(listing_html, "html.parser")
        urls: list[str] = []
        for card in soup.select("div.job-item"):
            # The card repeats the same link (title, salary, location blocks);
            # the title anchor is the canonical one.
            a = card.select_one("div.title a[href]") or card.select_one("a.job_link[href]")
            href = a.get("href") if a else None
            if not href or not _DETAIL_PATH.search(href.split("?")[0]):
                continue
            urls.append(urljoin(_BASE, href))
        return list(dict.fromkeys(urls))  # dedupe, keep order

    def _get(self, session, url: str, params: dict | None = None) -> str | None:
        try:
            resp = session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # curl_cffi raises its own error types
            _LOG.warning("fetch failed | url=%s | err=%s", url, e)
            return None
