"""CareerViet detail-page parser: one job detail HTML -> one record dict.

Mirrors the itviec parser: schema.org JobPosting JSON-LD first (CareerViet
serves a rich one on every detail page — title, salary, industry, experience,
location, benefits), with CSS fallbacks anchored on the stable Vietnamese
section HEADING TEXT ("Mô tả Công việc", "Yêu Cầu Công Việc", "Phúc lợi")
rather than on volatile utility classes.

Salary / currency convention — the same one itviec's parser uses: the
`currency` field names the unit the `salary_*_usd` numbers are expressed in
(itviec: "USD"). CareerViet publishes `baseSalary.currency` = "VND" with VND
amounts, so records carry currency="VND" and the native VND amounts; no FX rate
is invented here. Downstream converts if it wants a single unit.

The output dict must match data-contracts/schemas/raw/careerviet.avsc and uses
the same field names as the itviec records, so the bronze/silver dbt models and
the reverse ETL keep working. Pinned by saved-HTML fixtures + tests that
validate the record against that schema (sentpul.validator.validate_record).
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
_BULLET = "-•*·–— \t"
_INT32_MAX = 2_147_483_647

# Section headings on a CareerViet detail page (vi locale).
_H_DESCRIPTION = "Mô tả Công việc"
_H_REQUIREMENTS = "Yêu Cầu Công Việc"
_H_BENEFITS = "Phúc lợi"

# Vietnamese + English work-arrangement wording, checked against the posting's
# own text only (never the whole page — the nav and "similar jobs" cards would
# poison it). The patterns are deliberately PHRASES, not bare words: "remote
# setup of devices" in a support role is not a remote job, so a bare /remote/
# would mislabel it. First match wins, hybrid before remote.
_WORK_ARRANGEMENT = [
    (re.compile(r"\bhybrid\b|làm việc kết hợp|kết hợp tại nhà", re.I), "HYBRID"),
    (
        re.compile(
            r"\bfully\s+remote\b"
            r"|\b100\s*%\s*remote\b"
            r"|\bremote(?:ly)?\s+(?:work|working|job|position|role)\b"
            r"|\bwork(?:ing)?\s+(?:remotely|from\s+home)\b"
            r"|\bwfh\b"
            r"|làm việc từ xa|làm việc tại nhà|làm việc online",
            re.I,
        ),
        "REMOTE",
    ),
]

# "18 - 25 Triệu VND", "18 Tr - 25 Tr VND", "$1,000 - $2,000", "Trên 20 Triệu"
_MILLION = re.compile(r"\b(tr|tr\.|triệu|trieu|million)\b", re.I)
_NUM = re.compile(r"\d[\d.,]*")


# Default VND per 1 USD. Overridden from the contract (crawler.vnd_per_usd).
DEFAULT_VND_PER_USD = 25_000.0


class CareerVietParser(IParser):
    def __init__(self, vnd_per_usd: float = DEFAULT_VND_PER_USD) -> None:
        self._vnd_per_usd = vnd_per_usd

    def parse(self, raw: str) -> Iterable[dict]:
        soup = BeautifulSoup(raw, "html.parser")

        # schema.org JobPosting structured data — a clean, stable source for the
        # fields CSS scraping can't reliably reach.
        ld = self._jsonld(soup)

        title = self._text(soup.select_one("h1")) or ld.get("title")
        if not title:
            return  # not a job detail page — yield nothing

        # Natural key of the posting. job_id = sha256(job_url) is the stable
        # dedup key the silver layer groups on; crawled_at is its version column.
        job_url = self._job_url(soup, ld)
        job_id = (
            hashlib.sha256(job_url.encode("utf-8")).hexdigest() if job_url else None
        )

        salary_type, salary_min, salary_max, currency = self._salary(soup, ld)
        description = self._section_text(soup, _H_DESCRIPTION)

        record = {
            "title": title,
            "company_name": self._org_name(ld) or self._company_name(soup) or "",
            "company_url": self._org_url(ld) or self._company_url(soup),
            "salary_type": salary_type,
            "salary_min_usd": salary_min,
            "salary_max_usd": salary_max,
            "currency": currency,
            "location": self._location(soup, ld),
            "work_arrangement": self._work_arrangement(title, description),
            "posted_days_ago": self._posted_days_ago(ld),
            "company_type": None,
            "company_industry": ld.get("industry") or self._detail_item(soup, "Ngành nghề"),
            "company_size": None,
            "country": self._country(ld),
            "working_days": None,
            "overtime_policy": None,
            "min_years_experience": self._min_years_experience(ld),
            "job_domain": None,
            "tech_stack": self._tech_stack(soup, ld),
            "description": description,
            "responsibilities": self._section_items(soup, _H_DESCRIPTION),
            "skills": self._section_items(soup, _H_REQUIREMENTS),
            "nice_to_have": [],
            "benefits": self._benefits(soup, ld),
            "job_url": job_url,
            "job_id": job_id,
            "crawled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "careerviet",
        }
        yield record

    # ── JSON-LD (schema.org JobPosting) ────────────────────────────────────
    def _jsonld(self, soup: BeautifulSoup) -> dict:
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            for node in data if isinstance(data, list) else [data]:
                if isinstance(node, dict) and node.get("@type") == "JobPosting":
                    return node
        return {}

    def _org_name(self, ld: dict) -> Optional[str]:
        org = ld.get("hiringOrganization")
        return org.get("name") if isinstance(org, dict) else None

    def _org_url(self, ld: dict) -> Optional[str]:
        org = ld.get("hiringOrganization")
        url = org.get("url") if isinstance(org, dict) else None
        return url or None  # "" -> None

    def _address(self, ld: dict) -> dict:
        """JobPosting.jobLocation.address — CareerViet serves a single Place."""
        loc = ld.get("jobLocation")
        if isinstance(loc, list):
            loc = loc[0] if loc else None
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                return addr
        return {}

    def _country(self, ld: dict) -> Optional[str]:
        return self._address(ld).get("addressCountry") or None

    def _posted_days_ago(self, ld: dict) -> Optional[int]:
        # CareerViet serves a full timestamp ("2026-10-29T09:09:18.141Z"), so
        # take the date part rather than relying on fromisoformat's tz handling.
        ds = ld.get("datePosted")
        if not isinstance(ds, str) or len(ds) < 10:
            return None
        try:
            return max((date.today() - date.fromisoformat(ds[:10])).days, 0)
        except ValueError:
            return None

    def _job_url(self, soup: BeautifulSoup, ld: dict) -> Optional[str]:
        """Canonical URL of this posting — the natural key.

        Prefer <link rel="canonical">, then og:url, then the JSON-LD url.
        CareerViet serves all three on a detail page; the canonical one carries
        the JOB_ID (".../<slug>.35C8C434.html"), so it is a stable key.
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

    # ── salary ─────────────────────────────────────────────────────────────
    def _salary(
        self, soup: BeautifulSoup, ld: dict
    ) -> tuple[str, Optional[int], Optional[int], str]:
        """(salary_type, min, max, currency).

        JSON-LD baseSalary is authoritative: a disclosed range arrives as
        numeric minValue/maxValue, a hidden one as the literal text "Cạnh
        tranh" (negotiable). Falls back to the "Lương" row of the detail box.
        """
        base = ld.get("baseSalary")
        base = base if isinstance(base, dict) else {}
        currency = base.get("currency") or "VND"
        value = base.get("value")
        value = value if isinstance(value, dict) else {}

        lo = self._int32(value.get("minValue"))
        hi = self._int32(value.get("maxValue"))
        single = self._int32(value.get("value"))
        if lo is None and hi is None and single is None:
            text = self._detail_item(soup, "Lương") or ""
            lo, hi, cur = self._salary_from_text(text)
            currency = cur or currency

        # Normalise to USD: the salary_*_usd columns must mean the same thing for
        # every source, because silver_history unions the bronze views with
        # `select *`. A non-USD figure left raw here would silently corrupt any
        # cross-source salary filter.
        rate = 1.0 if str(currency).upper() == "USD" else self._vnd_per_usd
        lo = self._usd(lo, rate)
        hi = self._usd(hi, rate)
        single = self._usd(single, rate)
        currency = "USD"

        if lo is not None and hi is not None:
            return "RANGE", lo, hi, currency
        if lo is not None:
            return "FROM", lo, None, currency
        if hi is not None:
            return "UP_TO", None, hi, currency
        if single is not None:
            return "FIXED", single, single, currency
        return "NEGOTIABLE", None, None, currency

    def _usd(self, amount, rate: float) -> Optional[int]:
        """Convert a native-currency amount to whole USD; None if unusable."""
        try:
            v = float(amount)
        except (TypeError, ValueError):
            return None
        return round(v / rate) if v > 0 else None

    def _salary_from_text(
        self, text: str
    ) -> tuple[Optional[int], Optional[int], Optional[str]]:
        """Parse the displayed salary string, e.g. 'Lương: 18 - 25 Triệu VND'.

        Returns (min, max, currency); (None, None, None) for "Cạnh tranh" /
        anything unrecognised, which the caller turns into NEGOTIABLE.
        """
        if not text:
            return None, None, None
        low = text.lower()
        usd = "usd" in low or "$" in text
        scale = 1 if usd else (1_000_000 if _MILLION.search(low) else 1)
        nums = [
            self._int32(int(float(m.group(0).replace(".", "").replace(",", "")) * scale))
            for m in _NUM.finditer(text)
        ]
        nums = [n for n in nums if n]
        currency = "USD" if usd else "VND"
        if len(nums) >= 2:
            return min(nums[:2]), max(nums[:2]), currency
        if len(nums) == 1:
            n = nums[0]
            if "trên" in low or "từ" in low:
                return n, None, currency
            if "đến" in low or "tới" in low or "dưới" in low:
                return None, n, currency
            return n, n, currency
        return None, None, None

    def _int32(self, value) -> Optional[int]:
        """Coerce to a positive int that fits the schema's Int32, else None."""
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            return None
        return n if 0 < n <= _INT32_MAX else None

    # ── HTML sections ──────────────────────────────────────────────────────
    def _heading(self, soup: BeautifulSoup, text: str) -> Optional[Tag]:
        needle = text.casefold()
        return soup.find(
            lambda t: t.name in ("h2", "h3") and t.get_text(strip=True).casefold() == needle
        )

    def _section_block(self, soup: BeautifulSoup, heading: str) -> Optional[Tag]:
        """The `div.detail-row` wrapping a section heading and its body."""
        h = self._heading(soup, heading)
        if h is None:
            return None
        return h.find_parent("div", class_="detail-row") or h.parent

    def _section_lines(self, soup: BeautifulSoup, heading: str) -> list[str]:
        """Body of a section as clean lines (CareerViet uses <p>, not <li>)."""
        block = self._section_block(soup, heading)
        if block is None:
            return []
        h = self._heading(soup, heading)
        heading_text = h.get_text(strip=True) if h else ""
        lines: list[str] = []
        for el in block.select("li, p"):
            t = self._text(el)
            if not t:
                continue
            t = t.strip(_BULLET).strip()
            if not t or t == heading_text:
                continue
            lines.append(t)
        if lines:
            return lines
        # No <p>/<li> at all — split the block's own text.
        text = block.get_text("\n", strip=True)
        return [
            ln.strip(_BULLET).strip()
            for ln in text.split("\n")
            if ln.strip() and ln.strip() != heading_text
        ]

    def _section_text(self, soup: BeautifulSoup, heading: str) -> Optional[str]:
        lines = self._section_lines(soup, heading)
        return "\n".join(lines) or None

    def _section_items(self, soup: BeautifulSoup, heading: str) -> list[str]:
        return self._section_lines(soup, heading)

    def _detail_item(self, soup: BeautifulSoup, label: str) -> Optional[str]:
        """Value of a `<li><strong>LABEL</strong><p>VALUE</p></li>` detail row.

        The detail box is rendered twice (desktop + mobile) and the rows are not
        in a fixed order, so look the row up BY LABEL — never by index.
        """
        needle = label.casefold()
        for li in soup.select("section.job-detail-content li, div.job-detail-content li"):
            strong = li.find("strong")
            if strong is None:
                continue
            if strong.get_text(" ", strip=True).casefold().rstrip(":") != needle:
                continue
            value = self._text(li.find("p"))
            if value:
                return value
        return None

    # ── fields built from either source ────────────────────────────────────
    def _company_name(self, soup: BeautifulSoup) -> Optional[str]:
        return self._text(soup.select_one("a.job-company-name"))

    def _company_url(self, soup: BeautifulSoup) -> Optional[str]:
        a = soup.select_one("a.job-company-name[href]")
        if a is None:
            return None
        href = a.get("href")
        if href and href.startswith("/"):
            return "https://careerviet.vn" + href
        return href

    def _location(self, soup: BeautifulSoup, ld: dict) -> str:
        # The city ("Hồ Chí Minh"), not the street address — matches itviec,
        # which stores the city chip. Street level stays in the description.
        return (
            self._address(ld).get("addressLocality")
            or self._detail_item(soup, "Địa điểm")
            or self._text(soup.select_one("section.job-detail-content .map p a"))
            or ""
        )

    def _work_arrangement(self, title: str, description: Optional[str]) -> str:
        text = f"{title}\n{description or ''}"
        for pattern, value in _WORK_ARRANGEMENT:
            if pattern.search(text):
                return value
        return "AT_OFFICE"

    def _tech_stack(self, soup: BeautifulSoup, ld: dict) -> list[str]:
        # CareerViet's "Job tags / skills" block — the closest thing it has to
        # itviec's skill tags. JSON-LD `skills` carries the same list joined by
        # commas, used when the block is absent.
        tags = [self._text(a) for a in soup.select(".job-tags a")]
        tags = [t for t in tags if t]
        if not tags:
            tags = self._split_csv(ld.get("skills"))
        return list(dict.fromkeys(tags))

    def _benefits(self, soup: BeautifulSoup, ld: dict) -> list[str]:
        items = [self._text(li) for li in soup.select(".welfare-list li")]
        items = [i for i in items if i]
        if not items:
            items = self._section_items(soup, _H_BENEFITS)
        if not items:
            items = self._split_csv(ld.get("jobBenefits"))
        return list(dict.fromkeys(items))

    # ── helpers ────────────────────────────────────────────────────────────
    def _split_csv(self, value) -> list[str]:
        if not isinstance(value, str):
            return []
        return [p.strip() for p in value.split(",") if p.strip()]

    def _text(self, el: Optional[Tag]) -> Optional[str]:
        return _WS.sub(" ", el.get_text(" ", strip=True)) if el else None
