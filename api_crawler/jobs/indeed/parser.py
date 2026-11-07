"""Indeed search-page parser: one results page -> one record per job card.

WHERE THE DATA COMES FROM. Indeed renders its cards from a JSON blob assigned
to `window.mosaic.providerData["mosaic-provider-jobcards"]`. That blob is read
here rather than the rendered HTML: the markup is minified, class names are
generated per deploy, and the blob carries structured fields the markup only
formats -- `extractedSalary` as numbers, `pubDate` as an epoch, the location
split into city and state.

WHAT AN INDEED RECORD CANNOT CONTAIN, AND WHY IT IS NOT A BUG.
robots.txt Disallows /viewjob, so the detail page is off limits (see
source.py). The card's `snippet` is a TRUNCATED description -- Indeed cuts it
with an ellipsis -- and there is no responsibilities, requirements or benefits
list anywhere on a search page. Those fields are therefore empty, and
`description` holds the snippet with its truncation marked. An Indeed posting
will yield far fewer skills than an itviec one; that is the ceiling of what
this source permits, not a parser to improve later.

SALARY. Indeed publishes `extractedSalary` {min, max, type} plus the displayed
text, in VND for Vietnamese postings. `currency` here names the unit of the
`salary_*_usd` numbers, the same convention careerviet's parser documents, so
VND amounts are carried natively with currency="VND" and no FX rate is
invented. The numbers are normalised to MONTHLY, because that is what the
schema's salary fields mean -- and a figure that comes out implausibly low
after that conversion is dropped rather than published; see _salary.

The output dict must match data-contracts/schemas/raw/indeed.avsc and uses the
same field names as the itviec and careerviet records, so the bronze/silver
dbt models and the reverse ETL keep working. Pinned by a saved-HTML fixture
and tests that validate the record against that schema.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime

from api_crawler.shared.ports.parser import IParser

_LOG = logging.getLogger(__name__)
_BASE = "https://vn.indeed.com"
_INT32_MAX = 2_147_483_647
_ASSIGN = 'window.mosaic.providerData["mosaic-provider-jobcards"]'

#: Hours per month used to turn an hourly rate into a monthly one: 8h x 22d.
_HOURS_PER_MONTH = 176

#: Below this, a "monthly" Vietnamese salary is not a salary. Vietnam's region-I
#: minimum wage is ~4.96M VND/month, so 2M is already generous as a floor. It
#: exists because Indeed's own extractor mislabels periods -- a real card seen
#: while building this carried "10.000.000 đ - 20.000.000 đ / năm" with
#: type=YEARLY for what is plainly a monthly range, which would publish
#: 833,333 VND/month. A number we know is wrong is worse than no number: the
#: posting is recorded as NEGOTIABLE instead, and nothing downstream averages a
#: fiction into a salary chart.
_MIN_PLAUSIBLE_VND_MONTH = 2_000_000

#: The same floor in USD, for the rare card that quotes dollars.
_MIN_PLAUSIBLE_USD_MONTH = 80

_ELLIPSIS = re.compile(r"[…]|\.\.\.\s*$")


class IndeedParser(IParser):
    def parse(self, raw: str) -> Iterable[dict]:
        for card in self._cards(raw):
            record = self._record(card)
            if record is not None:
                yield record

    # ── the embedded card data ─────────────────────────────────────────────
    def _cards(self, raw: str) -> list[dict]:
        blob = self._blob(raw)
        if blob is None:
            return []
        try:
            model = blob["metaData"]["mosaicProviderJobCardsModel"]
        except (KeyError, TypeError):
            _LOG.warning("job-card blob has no mosaicProviderJobCardsModel")
            return []
        results = model.get("results")
        return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []

    def _blob(self, raw: str) -> dict | None:
        """The JSON object assigned to the job-cards provider, or None.

        Scanned with a brace counter that respects strings rather than a
        regex: the blob is ~1MB of nested JSON containing every brace and
        quote character you can think of, and a greedy or lazy pattern gets
        either the first nested object or the rest of the document.
        """
        at = raw.find(_ASSIGN)
        if at < 0:
            return None
        start = raw.find("{", at)
        if start < 0:
            return None
        depth, i, in_string, escaped = 0, start, False, False
        while i < len(raw):
            ch = raw[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
            i += 1
        try:
            return json.loads(raw[start:i])
        except ValueError as exc:
            _LOG.warning("job-card blob is not valid JSON: %s", exc)
            return None

    # ── one card -> one record ─────────────────────────────────────────────
    def _record(self, card: dict) -> dict | None:
        title = (card.get("displayTitle") or card.get("title") or "").strip()
        jobkey = (card.get("jobkey") or "").strip()
        if not title or not jobkey:
            return None  # not a job card

        # The canonical detail URL is the natural key, and it is only ever
        # STORED: nothing here requests it, because robots forbids /viewjob.
        job_url = f"{_BASE}/viewjob?jk={jobkey}"
        salary_type, salary_min, salary_max, currency = self._salary(card)

        return {
            "title": title,
            "company_name": (card.get("company") or "").strip(),
            "company_url": self._company_url(card),
            "salary_type": salary_type,
            "salary_min_usd": salary_min,
            "salary_max_usd": salary_max,
            "currency": currency,
            "location": (card.get("formattedLocation")
                         or card.get("jobLocationCity") or "").strip(),
            "work_arrangement": "REMOTE" if card.get("remoteLocation") else "AT_OFFICE",
            "posted_days_ago": self._posted_days_ago(card),
            "company_type": None,
            "company_industry": None,
            "company_size": None,
            "country": card.get("country") or "Vietnam",
            "working_days": None,
            "overtime_policy": None,
            # Not on a search card. Inferring it from the title ("Senior" => 5
            # years) would be a guess wearing a number's clothes.
            "min_years_experience": None,
            "job_domain": None,
            "tech_stack": [],
            "description": self._description(card),
            # All three live on the detail page, which robots puts out of reach.
            "responsibilities": [],
            "skills": [],
            "nice_to_have": [],
            "benefits": self._benefits(card),
            "job_url": job_url,
            "job_id": hashlib.sha256(job_url.encode("utf-8")).hexdigest(),
            "crawled_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "indeed",
        }

    def _company_url(self, card: dict) -> str | None:
        link = card.get("companyOverviewLink")
        if not link or not isinstance(link, str):
            return None
        return link if link.startswith("http") else f"{_BASE}{link}"

    def _description(self, card: dict) -> str | None:
        """The card snippet, with its truncation stated rather than hidden.

        Indeed cuts the snippet mid-sentence. Downstream reads this column as
        the posting's text, so the cut is marked: a reader (or an evaluation)
        can tell a short advert from a long one that was clipped.
        """
        snippet = (card.get("snippet") or "").strip()
        if not snippet:
            return None
        snippet = re.sub(r"<[^>]+>", " ", snippet)       # cards carry light markup
        snippet = " ".join(snippet.split())
        if _ELLIPSIS.search(snippet):
            snippet = _ELLIPSIS.sub("", snippet).strip()
            return f"{snippet} […truncated by Indeed; full text is on the posting]"
        return snippet

    def _benefits(self, card: dict) -> list[str]:
        """`rankedBenefits` is the one list a search card does carry."""
        out: list[str] = []
        for item in card.get("rankedBenefits") or []:
            label = (item or {}).get("label") if isinstance(item, dict) else None
            if label:
                out.append(str(label).strip())
        return list(dict.fromkeys(out))

    def _posted_days_ago(self, card: dict) -> int | None:
        """From `pubDate` (epoch ms), not the "30+ ngày trước" wording.

        The relative string is capped and localized; the epoch is neither.
        """
        raw = card.get("pubDate")
        if not isinstance(raw, (int, float)):
            return None
        posted = datetime.fromtimestamp(raw / 1000.0, tz=UTC)
        days = (datetime.now(UTC) - posted).days
        return days if 0 <= days <= _INT32_MAX else None

    # ── salary ─────────────────────────────────────────────────────────────
    def _salary(self, card: dict) -> tuple[str, int | None, int | None, str]:
        """(salary_type, min, max, currency) -- monthly, in the stated currency."""
        extracted = card.get("extractedSalary")
        snippet = card.get("salarySnippet") or {}
        currency = str(snippet.get("currency") or "VND").upper()
        if not isinstance(extracted, dict):
            return "NEGOTIABLE", None, None, currency

        per_month = self._to_monthly(extracted.get("type"))
        if per_month is None:
            return "NEGOTIABLE", None, None, currency

        low = self._amount(extracted.get("min"), per_month)
        high = self._amount(extracted.get("max"), per_month)
        floor = _MIN_PLAUSIBLE_USD_MONTH if currency == "USD" else _MIN_PLAUSIBLE_VND_MONTH
        if low is not None and low < floor:
            low = None
        if high is not None and high < floor:
            high = None

        if low is not None and high is not None:
            return ("RANGE" if high > low else "FIXED"), low, high, currency
        if low is not None:
            return "FROM", low, None, currency
        if high is not None:
            return "UP_TO", None, high, currency
        return "NEGOTIABLE", None, None, currency

    def _to_monthly(self, period: object) -> float | None:
        """Factor that turns Indeed's period into a monthly figure."""
        return {
            "MONTHLY": 1.0,
            "YEARLY": 1 / 12,
            "WEEKLY": 52 / 12,
            "DAILY": 22.0,
            "HOURLY": float(_HOURS_PER_MONTH),
        }.get(str(period or "").upper())

    def _amount(self, value: object, per_month: float) -> int | None:
        if not isinstance(value, (int, float)) or value <= 0:
            return None
        monthly = round(value * per_month)
        return monthly if 0 < monthly <= _INT32_MAX else None
