"""ITviec detail-page parser: one job detail HTML -> one record dict.

Selectors are anchored on stable section HEADING TEXT ("Job description", ...)
rather than itviec's volatile utility CSS classes, so a restyle is less likely
to break parsing. Fields itviec hides from anonymous visitors (salary) default
to NEGOTIABLE / null — matching the Avro schema.

The output dict must match data-contracts/schemas/raw/itviec.avsc. Pin it with a
saved-HTML fixture + a test that validates the record against that schema
(sentpul.validator.validate_record) so drift is caught.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from bs4 import BeautifulSoup, Tag

from api_crawler.shared.ports.parser import IParser

_WS = re.compile(r"\s+")

_WORK_ARRANGEMENT = {
    "at office": "AT_OFFICE",
    "remote": "REMOTE",
    "hybrid": "HYBRID",
}


class ItviecParser(IParser):
    def parse(self, raw: str) -> Iterable[dict]:
        soup = BeautifulSoup(raw, "html.parser")

        title = self._text(soup.select_one("h1"))
        if not title:
            return  # not a job detail page — yield nothing

        # schema.org JobPosting structured data — a clean, stable source for the
        # fields CSS scraping can't reliably reach.
        ld = self._jsonld(soup)

        # Natural key of the posting. job_id = sha256(job_url) is the stable
        # dedup key the silver layer groups on; crawled_at is its version column.
        job_url = self._job_url(soup, ld)
        job_id = (
            hashlib.sha256(job_url.encode("utf-8")).hexdigest() if job_url else None
        )

        record = {
            "title": title,
            "company_name": self._org_name(ld) or self._text(soup.select_one("h3")) or "",
            "company_url": self._attr(soup.select_one("h3 a"), "href"),
            # Salary is hidden behind a login wall for anonymous crawls.
            "salary_type": "NEGOTIABLE",
            "salary_min_usd": None,
            "salary_max_usd": None,
            "currency": "USD",
            "location": self._location(soup),
            "work_arrangement": self._work_arrangement(soup),
            "posted_days_ago": self._posted_days_ago(ld),
            "company_type": None,
            "company_industry": ld.get("industry"),
            "company_size": None,
            "country": self._country(ld),
            "working_days": None,
            "overtime_policy": None,
            "min_years_experience": self._min_years_experience(ld),
            "job_domain": None,
            "tech_stack": self._tech_stack(soup),
            "description": self._section_text(soup, "Job description"),
            "responsibilities": self._section_items(soup, "Job description"),
            "skills": self._section_items(soup, "Your skills and experience"),
            "nice_to_have": [],
            "benefits": self._section_items(soup, "Why you'll love working here"),
            "job_url": job_url,
            "job_id": job_id,
            "crawled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "itviec",
        }
        yield record

    # ── JSON-LD (schema.org JobPosting) ────────────────────────────────────
    def _jsonld(self, soup: BeautifulSoup) -> dict:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
        return {}

    def _org_name(self, ld: dict) -> Optional[str]:
        org = ld.get("hiringOrganization")
        return org.get("name") if isinstance(org, dict) else None

    def _country(self, ld: dict) -> Optional[str]:
        loc = ld.get("jobLocation")
        if isinstance(loc, list) and loc:
            addr = loc[0].get("address", {})
            return addr.get("addressCountry")
        return None

    def _posted_days_ago(self, ld: dict) -> Optional[int]:
        ds = ld.get("datePosted")
        try:
            return (date.today() - date.fromisoformat(ds)).days if ds else None
        except (ValueError, TypeError):
            return None

    def _job_url(self, soup: BeautifulSoup, ld: dict) -> Optional[str]:
        """Canonical URL of this posting — the natural key.

        Prefer <link rel="canonical">, then the og:url meta tag, then the
        JSON-LD url. itviec serves og:url on every detail page even when the
        canonical link is absent, so this is reliable enough to key on.
        """
        link = soup.find("link", rel="canonical")
        url = link.get("href") if link else None
        if not url:
            og = soup.find("meta", attrs={"property": "og:url"})
            url = og.get("content") if og else None
        if not url:
            u = ld.get("url")
            url = u if isinstance(u, str) else None
        return url.strip() if url else None

    def _min_years_experience(self, ld: dict) -> Optional[int]:
        exp = ld.get("experienceRequirements")
        if isinstance(exp, dict) and isinstance(exp.get("monthsOfExperience"), int):
            return exp["monthsOfExperience"] // 12
        return None

    # ── helpers ────────────────────────────────────────────────────────────
    def _text(self, el: Optional[Tag]) -> Optional[str]:
        return _WS.sub(" ", el.get_text(" ", strip=True)) if el else None

    def _attr(self, el: Optional[Tag], name: str) -> Optional[str]:
        return el.get(name) if el else None

    def _heading(self, soup: BeautifulSoup, text: str) -> Optional[Tag]:
        return soup.find(
            lambda t: t.name in ("h2", "h3") and t.get_text(strip=True) == text
        )

    def _section_body(self, soup: BeautifulSoup, heading: str) -> Optional[Tag]:
        h = self._heading(soup, heading)
        return h.find_next_sibling() if h else None

    def _section_text(self, soup: BeautifulSoup, heading: str) -> Optional[str]:
        body = self._section_body(soup, heading)
        return self._text(body)

    def _section_items(self, soup: BeautifulSoup, heading: str) -> list[str]:
        # A section can hold several sibling blocks (e.g. "[Required]" <p>, a
        # <ul>, "[Preferred]" <p>, another <ul>), so gather every <li> between
        # this heading and the next h2 rather than just the first sibling.
        h = self._heading(soup, heading)
        if h is None:
            return []
        items: list[str] = []
        for el in h.find_all_next():
            if el.name == "h2":
                break
            if el.name == "li":
                t = self._text(el)
                if t:
                    items.append(t)
        if items:
            return items
        # Fallback: no <li> — split the section body text into non-empty lines.
        body = self._section_body(soup, heading)
        if body is None:
            return []
        text = body.get_text("\n", strip=True)
        return [ln.strip("-•* ").strip() for ln in text.split("\n") if ln.strip()]

    def _location(self, soup: BeautifulSoup) -> str:
        # itviec renders the city in a truncated grey chip next to the title.
        el = soup.select_one("div.text-rich-grey.text-truncate")
        return self._text(el) or ""

    def _work_arrangement(self, soup: BeautifulSoup) -> str:
        text = soup.get_text(" ", strip=True).lower()
        for needle, value in _WORK_ARRANGEMENT.items():
            if needle in text:
                return value
        return "AT_OFFICE"

    def _tech_stack(self, soup: BeautifulSoup) -> list[str]:
        # Best-effort: skill tags before the "More jobs for you" section, so we
        # don't pick up tags from related-job cards. Tune with a fixture.
        stop = soup.find(lambda t: t.name == "h2" and "More jobs" in t.get_text())
        tags: list[str] = []
        for a in soup.select("a.itag"):
            if stop is not None and self._is_after(a, stop):
                break
            t = self._text(a)
            if t:
                tags.append(t)
        return list(dict.fromkeys(tags))

    def _is_after(self, node: Tag, marker: Tag) -> bool:
        sl, ml = getattr(node, "sourceline", None), getattr(marker, "sourceline", None)
        return sl is not None and ml is not None and sl > ml
