from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from .models import BusinessRecord
from .website import WebsiteCrawlResult, WebsitePage


class ProfileFact(BaseModel):
    """A normalized fact plus the exact page(s) that support it."""

    value: str
    source_urls: list[str] = Field(min_length=1)


class OperatingHours(BaseModel):
    day: str
    opens: str | None = None
    closes: str | None = None
    closed: bool = False
    source_url: str


class BusinessProfile(BaseModel):
    """Evidence-backed profile kept separate from the canonical Part 1 record."""

    lead_id: str
    business_name: str
    services: list[ProfileFact] = Field(default_factory=list)
    products: list[ProfileFact] = Field(default_factory=list)
    target_customers: list[ProfileFact] = Field(default_factory=list)
    about_info: list[ProfileFact] = Field(default_factory=list)
    business_description: ProfileFact | None = None
    operating_hours: list[OperatingHours] = Field(default_factory=list)
    field_sources: dict[str, list[str]] = Field(default_factory=dict)


DAYS = {
    "mon": "Monday", "monday": "Monday",
    "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday",
    "wed": "Wednesday", "weds": "Wednesday", "wednesday": "Wednesday",
    "thu": "Thursday", "thur": "Thursday", "thurs": "Thursday", "thursday": "Thursday",
    "fri": "Friday", "friday": "Friday",
    "sat": "Saturday", "saturday": "Saturday",
    "sun": "Sunday", "sunday": "Sunday",
}
DAY_PATTERN = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tues?|Wed|Weds|Thurs?|Thu|Fri|Sat|Sun)"
TIME_PATTERN = r"(?:\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2})"
HOURS_RE = re.compile(
    rf"(?P<days>{DAY_PATTERN}(?:\s*-\s*{DAY_PATTERN})?)\s*:\s*"
    rf"(?P<schedule>closed|{TIME_PATTERN}\s*(?:-|to)\s*{TIME_PATTERN})",
    re.IGNORECASE,
)
CUSTOMER_RE = re.compile(
    r"\b(?:we\s+serve|serves|our\s+(?:customers|clients|patients)\s+include|designed\s+for|built\s+for)\s+([^.;\n]{3,100})",
    re.IGNORECASE,
)
SERVICE_LEAD_RE = re.compile(r"\b(?:we\s+(?:provide|offer|deliver)|(?:our\s+)?services?\s+include|we\s+speciali[sz]e\s+in)\s+(.+)", re.IGNORECASE)
PRODUCT_LEAD_RE = re.compile(r"\b(?:products?\s+include|we\s+(?:sell|make|manufacture|offer))\s+(.+)", re.IGNORECASE)
GENERIC_HEADINGS = {
    "about", "about us", "our story", "services", "our services", "what we do",
    "our dental services", "dental services", "procedures", "our procedures",
    "products", "our products", "contact", "contact us", "faq", "frequently asked questions",
}
NON_SERVICE_LABELS = {
    "branch", "branches", "location", "locations", "learn more", "read more", "book now",
    "contact", "contact us", "appointment", "appointments", "for teeth", "teeth",
}
CUSTOMER_GROUPS = (
    ("children", re.compile(r"\b(?:children|child|kids|pediatric patients?)\b", re.I)),
    ("adults", re.compile(r"\b(?:adults?|adult patients?)\b", re.I)),
    ("families", re.compile(r"\b(?:famil(?:y|ies))\b", re.I)),
)
CUSTOMER_CATEGORY_ENDINGS = re.compile(
    r"\b(?:patients?|children|kids|adults?|families|business(?:es)?|companies|"
    r"organizations|organisations|nonprofits?|retailers|homeowners|students|"
    r"clients?|customers?|entrepreneurs|visitors|guests)\b$", re.I,
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t\r\n?-*")


def _unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _clean(value)
        key = clean.casefold()
        if clean and key not in seen:
            output.append(clean)
            seen.add(key)
    return output


def _explode_offerings(value: str) -> list[str]:
    """Split explicit enumerations while retaining multiword offering names."""
    value = re.sub(r"^(?:include|including|such as)\s+", "", value.strip(), flags=re.I)
    value = re.sub(r"^(?:a|an)\s+(?:wide|full|comprehensive)\s+range\s+of\s+[^,]+,?\s*(?:including\s+)?", "", value, flags=re.I)
    value = re.sub(r"^.*?\bincluding\s+", "", value, flags=re.I) if re.match(r"^(?:a|an)\s+(?:wide|full|comprehensive)\s+range", value, re.I) else value
    value = re.sub(r"[.!?].*$", "", value)
    pieces = re.split(r"\s*,\s*|\s+(?:and|&)\s+", value)
    return [item for item in (_clean(piece) for piece in pieces) if item and len(item) <= 100]


def _page_lines(page: WebsitePage) -> list[str]:
    return [_clean(line) for line in page.main_text.splitlines() if _clean(line)]


def _extract_offerings(pages: list[WebsitePage], kind: str) -> list[ProfileFact]:
    page_types = {"services"} if kind == "service" else {"products"}
    heading_names = ({"services", "our services", "what we do", "our dental services", "dental services",
                      "procedures", "our procedures"} if kind == "service" else {"products", "our products"})
    lead_re = SERVICE_LEAD_RE if kind == "service" else PRODUCT_LEAD_RE
    collected: list[tuple[str, str]] = []
    for page in pages:
        lines = _page_lines(page)
        if page.page_type not in page_types and not any(line.casefold() in heading_names for line in lines):
            continue
        in_section = page.page_type in page_types
        for line in lines:
            lower = line.casefold().strip(":")
            if lower in GENERIC_HEADINGS:
                in_section = lower in heading_names
                continue
            match = lead_re.search(line)
            if match:
                collected.extend((value, page.page_url) for value in _explode_offerings(match.group(1)))
            elif in_section and 2 <= len(line) <= 100 and not line.endswith((".", "?", "!")):
                # A short heading/list item under an explicit offerings page/section.
                collected.append((line, page.page_url))
    grouped: dict[str, ProfileFact] = {}
    for value, source in collected:
        clean = _clean(value)
        low = clean.casefold().strip(" .,:;!?-")
        if (not clean or low in GENERIC_HEADINGS or low in NON_SERVICE_LABELS
                or re.fullmatch(r"[\d+%., -]+", clean)
                or re.match(r"^(?:for|and|or|with|of|to)\b", low)
                or re.search(r"\b(?:branches|branch|locations|learn more|read more|call us)\b", low)):
            continue
        key = clean.casefold()
        if key not in grouped:
            grouped[key] = ProfileFact(value=clean, source_urls=[source])
        elif source not in grouped[key].source_urls:
            grouped[key].source_urls.append(source)
    return list(grouped.values())


def extract_profile_services(pages: list[WebsitePage]) -> list[ProfileFact]:
    """Extract concise service names from already-crawled page evidence."""
    return _extract_offerings(pages, "service")


def _extract_customers(pages: list[WebsitePage]) -> list[ProfileFact]:
    grouped: dict[str, ProfileFact] = {}
    for page in pages:
        for match in CUSTOMER_RE.finditer(page.main_text):
            segment = _clean(match.group(1))
            segment = re.split(r"\b(?:to|by|through|with|who|that|seeking|looking|so that)\b", segment,
                               maxsplit=1, flags=re.I)[0].strip(" ,:.-")
            phrase_words = segment.split()
            matching_groups = [(label, pattern) for label, pattern in CUSTOMER_GROUPS if pattern.search(segment)]
            if (not matching_groups and segment and len(phrase_words) <= 8 and CUSTOMER_CATEGORY_ENDINGS.search(segment)
                    and not re.search(r"\b(?:provide|providing|help|helping|achieve|maintain|receive|"
                                      r"experience|treatment|offer|offering|designed|located)\b", segment, re.I)):
                key = segment.casefold()
                if key not in grouped:
                    grouped[key] = ProfileFact(value=segment, source_urls=[page.page_url])
                elif page.page_url not in grouped[key].source_urls:
                    grouped[key].source_urls.append(page.page_url)
            for label, _pattern in matching_groups:
                if label not in grouped:
                    grouped[label] = ProfileFact(value=label, source_urls=[page.page_url])
                elif page.page_url not in grouped[label].source_urls:
                    grouped[label].source_urls.append(page.page_url)
    return list(grouped.values())


def _expand_days(expression: str) -> list[str]:
    parts = re.split(r"\s*[-?]\s*", expression)
    if len(parts) == 1:
        day = DAYS.get(parts[0].lower())
        return [day] if day else []
    first, last = DAYS.get(parts[0].lower()), DAYS.get(parts[1].lower())
    order = list(dict.fromkeys(DAYS.values()))
    if first not in order or last not in order:
        return []
    start, end = order.index(first), order.index(last)
    return order[start:end + 1] if start <= end else []


def _normalize_time(value: str) -> str:
    clean = re.sub(r"\s+", " ", value.strip().lower()).replace(".", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", clean)
    if not match:
        return _clean(value)
    hour, minute, meridiem = match.groups()
    return f"{int(hour):02d}:{minute or '00'}" + (f" {meridiem.upper()}" if meridiem else "")


def _extract_hours(pages: list[WebsitePage]) -> list[OperatingHours]:
    output: list[OperatingHours] = []
    seen: set[tuple[str, str | None, str | None, bool]] = set()
    for page in pages:
        for line in _page_lines(page):
            match = HOURS_RE.search(line)
            if not match:
                continue
            schedule = match.group("schedule")
            closed = schedule.casefold() == "closed"
            times = re.split(r"\s*(?:-|to)\s*", schedule, maxsplit=1, flags=re.I) if not closed else []
            opens = _normalize_time(times[0]) if times else None
            closes = _normalize_time(times[1]) if len(times) > 1 else None
            for day in _expand_days(match.group("days")):
                identity = (day, opens, closes, closed)
                if identity not in seen:
                    seen.add(identity)
                    output.append(OperatingHours(day=day, opens=opens, closes=closes, closed=closed, source_url=page.page_url))
    return output


def _about_facts(pages: list[WebsitePage]) -> list[ProfileFact]:
    output: list[ProfileFact] = []
    seen: set[str] = set()
    for page in pages:
        if page.page_type not in {"about", "home"} or not page.main_text:
            continue
        lines = []
        for line in _page_lines(page):
            low = line.casefold()
            if len(line) < 45 or len(line) > 360 or low in GENERIC_HEADINGS or any(term in low for term in (
                "what our patients say", "testimonials", "happy stories", "book an appointment",
                "call us today", "read more", "gallery", "copyright", "privacy policy",
                "home services about contact", "all rights reserved", "cookie policy",
            )):
                continue
            lines.append(line)
            if len(lines) == 3:
                break
        text = _clean(" ".join(lines))
        if not text:
            continue
        if text.casefold() not in seen:
            output.append(ProfileFact(value=text, source_urls=[page.page_url]))
            seen.add(text.casefold())
    return output


def extract_profile_about(pages: list[WebsitePage]) -> list[ProfileFact]:
    """Return concise, source-linked about facts from stored website pages."""
    return _about_facts(pages)


def _description(business: BusinessRecord, pages: list[WebsitePage]) -> ProfileFact | None:
    # Prefer explicit, page-authored metadata; retain it verbatim rather than inventing claims.
    for page in pages:
        if page.page_type in {"home", "about"} and page.meta_description:
            return ProfileFact(value=_clean(page.meta_description)[:320], source_urls=[page.page_url])
    # A verified source-record description is acceptable and keeps its original provenance.
    if business.description and business.source_ref:
        return ProfileFact(value=_clean(business.description), source_urls=[business.source_ref])
    for page in pages:
        if page.page_type in {"home", "about"}:
            for line in _page_lines(page):
                if line.casefold() not in GENERIC_HEADINGS and len(line) >= 35:
                    return ProfileFact(value=line[:320], source_urls=[page.page_url])
    return None


def build_business_profile(business: BusinessRecord, website: WebsiteCrawlResult) -> BusinessProfile:
    """Build a conservative, source-linked profile from already-crawled page content."""
    if business.lead_id != website.lead_id:
        raise ValueError("business and website result lead_id values must match")
    pages = [page for page in website.website_content if page.main_text.strip()]
    profile = BusinessProfile(
        lead_id=business.lead_id,
        business_name=business.business_name,
        services=_extract_offerings(pages, "service"),
        products=_extract_offerings(pages, "product"),
        target_customers=_extract_customers(pages),
        about_info=_about_facts(pages),
        business_description=_description(business, pages),
        operating_hours=_extract_hours(pages),
    )
    profile.field_sources = {
        "services": _sources(profile.services),
        "products": _sources(profile.products),
        "target_customers": _sources(profile.target_customers),
        "about_info": _sources(profile.about_info),
        "business_description": profile.business_description.source_urls if profile.business_description else [],
        "operating_hours": _unique_sources(entry.source_url for entry in profile.operating_hours),
    }
    return profile


def _sources(facts: list[ProfileFact]) -> list[str]:
    return _unique_sources(url for fact in facts for url in fact.source_urls)


def _unique_sources(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))
