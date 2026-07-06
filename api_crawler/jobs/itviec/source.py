"""ITviec source: walk listing pages, then yield each job's DETAIL page HTML.

itviec is a two-level crawl (listing -> detail). That navigation is
itviec-specific, so it lives here in a per-source ISource adapter. The parser
stays single-purpose: one detail page -> one record.

Uses curl_cffi (browser TLS impersonation) to get past Cloudflare, and paces
requests with a small delay so a larger crawl doesn't trip rate limiting.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi

from api_crawler.shared.ports.source import ISource

_LOG = logging.getLogger(__name__)
_BASE = "https://itviec.com"


class ItviecSource(ISource):
    def __init__(
        self,
        listing_url: str = f"{_BASE}/it-jobs",
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
            for page in range(self._start_page, self._start_page + self._max_pages):
                listing = self._get(session, self._listing_url, params={"page": page})
                if listing is None:
                    continue
                urls = self._detail_urls(listing)
                _LOG.info("listing page=%d | %d detail urls", page, len(urls))
                for url in urls:
                    time.sleep(self._delay)  # be polite — avoid rate limiting
                    detail = self._get(session, url)
                    if detail is not None:
                        yield detail

    def _detail_urls(self, listing_html: str) -> list[str]:
        """Extract each job's detail URL from the listing cards."""
        soup = BeautifulSoup(listing_html, "html.parser")
        urls: list[str] = []
        for card in soup.select("div.job-card"):
            raw = card.get("data-search--job-selection-job-url-value")
            if not raw:
                continue
            # raw looks like "/it-jobs/{slug}/content?job_index=0&locale=en"
            path = raw.split("/content")[0]
            urls.append(urljoin(_BASE, path))
        return list(dict.fromkeys(urls))  # dedupe, keep order

    def _get(self, session, url: str, params: dict | None = None) -> str | None:
        try:
            resp = session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # curl_cffi raises its own error types
            _LOG.warning("fetch failed | url=%s | err=%s", url, e)
            return None
