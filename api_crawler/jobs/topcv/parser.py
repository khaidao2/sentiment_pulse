"""TopCV detail-page parser: one job detail HTML -> one record dict.

Anchoring choice (differs from itviec on purpose): TopCV serves the job-detail
section headings in the *visitor's locale* — the same page renders "Mô tả công
việc" or "Job Description" depending on the request — so heading text is NOT a
stable anchor here. What is stable is the schema.org JobPosting JSON-LD, whose
`description` / `jobBenefits` embed the three sections with Vietnamese `<h2>`
markers regardless of UI locale. So the JSON-LD is the primary source and CSS is
only the fallback for title/company/size.

Salary: TopCV publishes `baseSalary` in VND. The contract's salary fields are
USD (`salary_min_usd` / `salary_max_usd`), and `currency` names the currency of
those values — itviec's convention — so VND is converted at `vnd_per_usd`
(contract-fed, see config.VND_PER_USD) and `currency` stays "USD". That keeps
silver/gold and the reverse ETL comparing like with like across sources.

The output dict must match data-contracts/schemas/raw/topcv.avsc. Pinned by a
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

# Section headings inside the JSON-LD description HTML (always Vietnamese).
_SEC_DESCRIPTION = "mô tả công việc"
_SEC_REQUIREMENTS = "yêu cầu ứng viên"
_SEC_BENEFITS = "quyền lợi"

# TopCV writers mark the optional half of the requirements section with a short
# bold paragraph before a second <ul>. Everything after it is nice_to_have.
_NICE_TO_HAVE_MARKERS = (
    "nice-to-have",
    "nice to have",
    "ưu tiên",
    "điểm cộng",
    "preferred",
    "là một lợi thế",
)

# Substrings that reveal the work arrangement, scanned over the job-detail
# body only (not the whole page — related-job cards would poison it).
_WORK_ARRANGEMENT = {
    "hybrid": "HYBRID",
    "kết hợp từ xa": "HYBRID",
    "remote": "REMOTE",
    "làm việc từ xa": "REMOTE",
    "làm từ xa": "REMOTE",
}

# Label of the company-size row in the right-hand company box. TopCV keeps these
# labels Vietnamese even on the English UI, but accept the English one too.
_SIZE_LABELS = ("quy mô", "company size", "size")

# Default VND per 1 USD. Overridden from the contract (crawler.vnd_per_usd).
DEFAULT_VND_PER_USD = 25_000.0


class TopcvParser(IParser):
    def __init__(self, vnd_per_usd: float = DEFAULT_VND_PER_USD) -> None:
        self._vnd_per_usd = vnd_per_usd

    def parse(self, raw: str) -> Iterable[dict]:
        soup = BeautifulSoup(raw, "html.parser")

        # schema.org JobPosting structured data — a clean, locale-independent
        # source for nearly every field.
        ld = self._jsonld(soup)

        title = self._text(soup.select_one("h1")) or self._str(ld.get("title"))
        if not title:
            return  # not a job detail page — yield nothing

        # Natural key of the posting. job_id = sha256(job_url), the same stable
        # dedup key itviec emits, so the silver layer groups sources uniformly.
        job_url = self._job_url(soup, ld)
        job_id = (
            hashlib.sha256(job_url.encode("utf-8")).hexdigest() if job_url else None
        )

        sections = self._description_sections(ld)
        salary = self._salary(ld)

        record = {
            "title": title,
            "company_name": self._org_name(ld)
            or self._text(soup.select_one(".company-name"))
            or "",
            "company_url": self._org_url(ld),
            "salary_type": salary["salary_type"],
            "salary_min_usd": salary["salary_min_usd"],
            "salary_max_usd": salary["salary_max_usd"],
            "currency": salary["currency"],
            "location": self._location(ld, soup),
            "work_arrangement": self._work_arrangement(soup),
            "posted_days_ago": self._posted_days_ago(ld),
            "company_type": None,
            "company_industry": self._str(ld.get("industry")),
            "company_size": self._company_size(soup),
            "country": self._country(ld),
            "working_days": None,
            "overtime_policy": None,
            "min_years_experience": self._min_years_experience(ld),
            "job_domain": None,
            # TopCV's "Kỹ năng cần có" chips — mirrored into the JSON-LD
            # `skills` string — are the tech tags, the analogue of itviec's
            # .itag list. The requirement bullets go to `skills`.
            "tech_stack": self._csv_list(ld.get("skills")),
            "description": sections.get(_SEC_DESCRIPTION, {}).get("text"),
            "responsibilities": sections.get(_SEC_DESCRIPTION, {}).get("items", []),
            "skills": sections.get(_SEC_REQUIREMENTS, {}).get("items", []),
            "nice_to_have": sections.get(_SEC_REQUIREMENTS, {}).get(
                "nice_to_have", []
            ),
            "benefits": self._benefits(ld, sections),
            "job_url": job_url,
            "job_id": job_id,
            "crawled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "topcv",
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
        return self._str(org.get("name")) if isinstance(org, dict) else None

    def _org_url(self, ld: dict) -> Optional[str]:
        org = ld.get("hiringOrganization")
        return self._str(org.get("sameAs")) if isinstance(org, dict) else None

    def _address(self, ld: dict) -> dict:
        """jobLocation may be a Place or a list of them — normalize to one address."""
        loc = ld.get("jobLocation")
        if isinstance(loc, list):
            loc = loc[0] if loc else None
        if not isinstance(loc, dict):
            return {}
        addr = loc.get("address")
        return addr if isinstance(addr, dict) else {}

    def _country(self, ld: dict) -> Optional[str]:
        return self._str(self._address(ld).get("addressCountry"))

    def _location(self, ld: dict, soup: BeautifulSoup) -> str:
        """City / province of the posting — the itviec `location` granularity.

        addressRegion is the province ("Hồ Chí Minh"); addressLocality is the
        ward, which is too fine, so it is only a fallback.
        """
        addr = self._address(ld)
        value = self._str(addr.get("addressRegion")) or self._str(
            addr.get("addressLocality")
        )
        return value or self._text(soup.select_one(".address .city-text")) or ""

    def _posted_days_ago(self, ld: dict) -> Optional[int]:
        ds = ld.get("datePosted")
        try:
            return (date.today() - date.fromisoformat(ds)).days if ds else None
        except (ValueError, TypeError):
            return None

    def _min_years_experience(self, ld: dict) -> Optional[int]:
        exp = ld.get("experienceRequirements")
        if isinstance(exp, dict) and isinstance(exp.get("monthsOfExperience"), int):
            return exp["monthsOfExperience"] // 12
        return None

    def _job_url(self, soup: BeautifulSoup, ld: dict) -> Optional[str]:
        """Canonical URL of this posting — the natural key.

        Prefer <link rel="canonical">, then og:url, then the JSON-LD url. The
        canonical link is free of the tracking query string the listing links
        carry, which is why it, not the fetched URL, is the key.
        """
        link = soup.find("link", rel="canonical")
        url = link.get("href") if link else None
        if not url:
            og = soup.find("meta", attrs={"property": "og:url"}) or soup.find(
                "meta", attrs={"name": "og:url"}
            )
            url = og.get("content") if og else None
        if not url:
            url = self._str(ld.get("url"))
        return url.strip() if url else None

    # ── salary ─────────────────────────────────────────────────────────────
    def _salary(self, ld: dict) -> dict:
        """baseSalary (VND or USD, monthly) -> salary_type + USD min/max.

        TopCV omits baseSalary when the posting says "Thoả thuận" (negotiable),
        which maps to NEGOTIABLE + null amounts, exactly as itviec does.
        """
        negotiable = {
            "salary_type": "NEGOTIABLE",
            "salary_min_usd": None,
            "salary_max_usd": None,
            "currency": "USD",
        }
        base = ld.get("baseSalary")
        if not isinstance(base, dict):
            return negotiable
        value = base.get("value")
        if not isinstance(value, dict):
            return negotiable

        rate = 1.0 if str(base.get("currency", "")).upper() == "USD" else self._vnd_per_usd
        lo = self._usd(value.get("minValue"), rate)
        hi = self._usd(value.get("maxValue"), rate)
        if lo is None and hi is None:
            lo = hi = self._usd(value.get("value"), rate)

        if lo is not None and hi is not None:
            salary_type = "FIXED" if lo == hi else "RANGE"
        elif lo is not None:
            salary_type = "FROM"
        elif hi is not None:
            salary_type = "UP_TO"
        else:
            return negotiable

        return {
            "salary_type": salary_type,
            "salary_min_usd": lo,
            "salary_max_usd": hi,
            "currency": "USD",
        }

    def _usd(self, amount, rate: float) -> Optional[int]:
        try:
            v = float(amount)
        except (TypeError, ValueError):
            return None
        return round(v / rate) if v > 0 else None

    # ── sections (parsed out of the JSON-LD description HTML) ──────────────
    def _description_sections(self, ld: dict) -> dict[str, dict]:
        """Split the JSON-LD description HTML on its <h2> section markers.

        Returns {canonical heading: {"text", "items", "nice_to_have"}}. Headings
        are canonicalised by prefix because TopCV writes e.g. "Quyền lợi" and
        "Quyền lợi được hưởng". Bullets after a "Nice-to-Have" style marker go
        to `nice_to_have` instead of `items`.
        """
        html = self._str(ld.get("description"))
        if not html:
            return {}
        soup = BeautifulSoup(html, "html.parser")
        out: dict[str, dict] = {}
        for h in soup.find_all("h2"):
            key = self._canon_heading(h.get_text(strip=True))
            if not key:
                continue
            body, items, optional = [], [], []
            in_optional = False
            for el in h.find_all_next():
                if el.name == "h2":
                    break
                if el.name == "li":
                    t = self._text(el)
                    if t:
                        (optional if in_optional else items).append(t)
                elif el.name in ("p", "div") and not el.find(["li", "p"]):
                    lines = self._lines(el)
                    if self._is_nice_to_have_marker(lines):
                        in_optional = True
                        continue
                    body.extend(lines)
            out[key] = {
                "text": " ".join(items + body).strip() or None,
                "items": items or body,
                "nice_to_have": optional,
            }
        return out

    def _is_nice_to_have_marker(self, lines: list[str]) -> bool:
        """A short block whose only job is to label the optional bullets below."""
        if len(lines) != 1 or len(lines[0]) > 80:
            return False
        low = lines[0].lower()
        return any(m in low for m in _NICE_TO_HAVE_MARKERS)

    def _canon_heading(self, text: str) -> Optional[str]:
        t = _WS.sub(" ", text).strip().lower()
        for known in (_SEC_DESCRIPTION, _SEC_REQUIREMENTS, _SEC_BENEFITS):
            if t.startswith(known):
                return known
        return None

    def _benefits(self, ld: dict, sections: dict[str, dict]) -> list[str]:
        """Benefits: the dedicated jobBenefits HTML, else the section bullets."""
        html = self._str(ld.get("jobBenefits"))
        if html:
            items = [
                t
                for t in (
                    self._text(el)
                    for el in BeautifulSoup(html, "html.parser").find_all("li")
                )
                if t
            ]
            if items:
                return items
            lines = self._lines(BeautifulSoup(html, "html.parser"))
            if lines:
                return lines
        return sections.get(_SEC_BENEFITS, {}).get("items", [])

    # ── CSS fallbacks ──────────────────────────────────────────────────────
    def _company_size(self, soup: BeautifulSoup) -> Optional[str]:
        for label in soup.select(".box-company-info-detail__list--item-label"):
            text = (self._text(label) or "").lower().rstrip(":")
            if any(text.startswith(w) for w in _SIZE_LABELS):
                sibling = label.find_next_sibling()
                value = self._text(sibling)
                if value:
                    return value
        return None

    def _work_arrangement(self, soup: BeautifulSoup) -> str:
        blocks = soup.select(".box-job-information-detail-item")
        text = " ".join(b.get_text(" ", strip=True) for b in blocks).lower()
        if not text:
            text = (self._text(soup.select_one("h1")) or "").lower()
        for needle, value in _WORK_ARRANGEMENT.items():
            if needle in text:
                return value
        return "AT_OFFICE"

    # ── helpers ────────────────────────────────────────────────────────────
    def _text(self, el: Optional[Tag]) -> Optional[str]:
        return _WS.sub(" ", el.get_text(" ", strip=True)).strip() if el else None

    def _lines(self, el: Tag) -> list[str]:
        """Split a block into bullet-ish lines (TopCV writes "- foo<br>- bar")."""
        raw = el.get_text("\n", strip=True)
        return [
            _WS.sub(" ", ln).strip(" -•*").strip()
            for ln in raw.split("\n")
            if ln.strip(" -•*").strip()
        ]

    def _csv_list(self, value) -> list[str]:
        """"A, B, C" -> ["A", "B", "C"], deduped, order kept."""
        text = self._str(value)
        if not text:
            return []
        items = [_WS.sub(" ", p).strip() for p in text.split(",")]
        return list(dict.fromkeys(i for i in items if i))

    def _str(self, value) -> Optional[str]:
        return value.strip() if isinstance(value, str) and value.strip() else None
