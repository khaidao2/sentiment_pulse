"""Indeed Vietnam source: walk SEARCH pages only, one query at a time.

WHY THERE IS NO DETAIL FETCH, UNLIKE EVERY OTHER SOURCE HERE.
`https://vn.indeed.com/robots.txt` carries `Disallow: /viewjob` (and
`/m/viewjob`). The detail page is where the full requirement text lives, so on
Indeed we do not have it and do not go looking: this crawler reads the search
results only. Each result card carries title, company, location, salary, the
posting date and a TRUNCATED description snippet, and that is the whole of
what an Indeed record can honestly contain. The skills extractor downstream
therefore sees a sentence or two per Indeed posting instead of a full advert --
see the parser's module docstring.

PAGINATION IS BEHIND A LOGIN, SO ONE PAGE PER QUERY IS THE YIELD.
robots.txt enumerates Allow lines for start=0..90, but Indeed answers an
anonymous request for start>=10 with its sign-in page ("Đăng nhập | Tài khoản
Indeed"), not results -- measured 2026-11-05. Getting page two would mean
crawling with someone's account, which this does not do. So a query returns
the ~15 cards on its first page, and breadth comes from the QUERIES list
rather than from depth. MAX_PAGES stays configurable in case that wall moves,
and the sign-in page is detected explicitly so the log says what happened.

WHAT ROBOTS ALLOWS, AND WHAT THIS STAYS INSIDE OF:
  * `/jobs?q=&l=` search pages -- allowed under `Allow: /`
  * `Disallow` paths this must never touch: /viewjob, /m/viewjob,
    /advanced_search, /alert, /applystart, /cmp/_*, and the query parameters
    rt=nc, &alid=, &calert=, &iafilter=, &mna=, ?rss, &serpstart=, /vaclk
_assert_allowed below is the guard, because the URL arrives from a contract
and a typo there must fail loudly rather than quietly crawl a forbidden path.

Uses curl_cffi (browser TLS impersonation) like the other sources in this
package: Indeed sits behind Cloudflare and answers a plain client with a 403
"Security Check" page even on an allowed path.

AND IT RATE-LIMITS HARD. Measured 2026-11-05: roughly half a dozen search
pages from one address inside a few minutes, and the next request is a bare
403. So this source is paced in TENS of seconds, not fractions of one, and a
403 is backed off and retried rather than treated as the end of the query --
losing a whole query to one refusal is how a crawl quietly returns a third of
the postings. The Airflow task is also allowed to run on either node (see the
contract's `airflow.nodes`), and the two have different egress addresses, so
successive runs do not all leave by the same one.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Sequence
from urllib.parse import urlparse

from curl_cffi import requests as cffi

from api_crawler.shared.ports.source import ISource

_LOG = logging.getLogger(__name__)
_BASE = "https://vn.indeed.com"
_PAGE_SIZE = 10  # Indeed's own `start` step; the Allow lines are multiples of 10

#: Paths robots.txt forbids. Checked against the configured URL, not built by it.
_FORBIDDEN_PATHS = (
    "/viewjob",
    "/m/viewjob",
    "/advanced_search",
    "/alert",
    "/applystart",
    "/vaclk",
    "/cmp/",
    "/api/",
)
#: Query parameters robots.txt forbids.
_FORBIDDEN_PARAMS = ("rt=nc", "alid=", "calert=", "iafilter=", "mna=", "rss", "serpstart=")


#: Indeed's sign-in page, served instead of results for start>=10. Matched on
#: the account-page title rather than on body copy, which is localized.
_SIGN_IN_TITLE = "Tài khoản Indeed"


def _is_sign_in_page(html: str) -> bool:
    head = html[:4000]
    return _SIGN_IN_TITLE in head or "/account/login" in head


def _assert_allowed(url: str) -> None:
    parsed = urlparse(url)
    path = parsed.path or "/"
    for bad in _FORBIDDEN_PATHS:
        if path.startswith(bad):
            raise ValueError(
                f"{url} is Disallowed by vn.indeed.com/robots.txt ({bad}); "
                f"this crawler reads search pages only"
            )
    for bad in _FORBIDDEN_PARAMS:
        if bad in (parsed.query or ""):
            raise ValueError(
                f"{url} carries the robots-Disallowed parameter {bad!r}"
            )


class _Refused(RuntimeError):
    """The edge served a challenge instead of results."""


class IndeedSource(ISource):
    def __init__(
        self,
        search_url: str = f"{_BASE}/jobs",
        queries: Sequence[str] = ("data engineer",),
        location: str = "",
        max_pages: int = 1,
        impersonate: str = "chrome",
        timeout: float = 30.0,
        delay: float = 15.0,
        backoff: float = 60.0,
        attempts: int = 3,
    ) -> None:
        _assert_allowed(search_url)
        self._search_url = search_url
        self._queries = [q for q in (q.strip() for q in queries) if q]
        self._location = location
        # One by default: see the module docstring -- page two is a login page.
        self._max_pages = max_pages
        self._impersonate = impersonate
        self._timeout = timeout
        # Slower than the other sources by an order of magnitude, on purpose:
        # these are search pages behind a bot-management edge that starts
        # refusing after a handful, and one page here yields fifteen postings
        # where itviec needs fifteen requests for the same.
        self._delay = delay
        self._backoff = backoff
        self._attempts = max(1, attempts)
        self._headers = {"Accept-Language": "vi,en;q=0.9"}

    def fetch(self) -> Iterable[str]:
        """Yield the raw HTML of each search page, for every configured query."""
        if not self._queries:
            _LOG.warning("no queries configured → nothing to crawl")
            return
        with cffi.Session(impersonate=self._impersonate, headers=self._headers) as session:
            for query in self._queries:
                for page in range(self._max_pages):
                    params = {"q": query, "l": self._location, "start": page * _PAGE_SIZE}
                    html = self._get(session, self._search_url, params=params)
                    if html is None:
                        # A blocked or failed page ends THIS query, not the run:
                        # the next query may well be served.
                        _LOG.warning("query=%r page=%d failed → next query", query, page)
                        break
                    if _is_sign_in_page(html):
                        _LOG.info(
                            "query=%r page=%d is Indeed's sign-in page → stop "
                            "(anonymous pagination ends after page 1)",
                            query, page,
                        )
                        break
                    if '"jobkey"' not in html:
                        # No job cards on the page -> walked past the last one.
                        # Tested on the embedded card data rather than on
                        # Indeed's localized "no results" sentence, which is
                        # one copy change away from never matching again.
                        _LOG.info("query=%r page=%d has no cards → stop", query, page)
                        break
                    _LOG.info("query=%r page=%d | %d bytes", query, page, len(html))
                    yield html
                    time.sleep(self._delay)  # be polite — one page every `delay`s

    def _get(self, session, url: str, params: dict | None = None) -> str | None:
        """One search page, with a backoff on refusal. None when it never came.

        A 403 or a challenge page means "too fast from this address", and the
        answer to that is to wait, not to give up on the query: the attempt
        after a minute usually lands. Anything else (a timeout, a reset) gets
        the same treatment, because the distinction does not change what we
        can do about it.
        """
        for attempt in range(1, self._attempts + 1):
            try:
                resp = session.get(url, params=params, timeout=self._timeout)
                resp.raise_for_status()
                if "Security Check" in resp.text[:4000]:
                    # A 200 is not proof: the edge serves the challenge page
                    # with one too. Treating it as HTML would parse zero jobs
                    # and look like an empty result set.
                    raise _Refused("challenge page")
                return resp.text
            except Exception as e:  # curl_cffi raises its own error types
                last = e
                if attempt < self._attempts:
                    wait = self._backoff * attempt   # 60s, then 120s
                    _LOG.warning(
                        "refused | url=%s | attempt=%d/%d | err=%s | waiting %.0fs",
                        url, attempt, self._attempts, e, wait,
                    )
                    time.sleep(wait)
        _LOG.warning("gave up | url=%s | err=%s", url, last)
        return None
