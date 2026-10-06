"""Validated lead export schema and deterministic business lead scoring."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from typing import Any
from urllib.parse import urlparse

class StrictOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class BusinessSection(StrictOutputModel):
    name: str
    category: str | None = None
    sub_category: str | None = None
    description: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    rating: float | None = None

class HoursEntry(StrictOutputModel):
    opens: str | None = None
    closes: str | None = None
    closed: bool = False

class ProfileSection(StrictOutputModel):
    services: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    target_customers: list[str] = Field(default_factory=list)
    about_info: str = ""
    operating_hours: dict[str, HoursEntry] = Field(default_factory=dict)

class WebsiteSection(StrictOutputModel):
    status: str = "not_checked"
    about: str | None = None
    services: list[str] = Field(default_factory=list)
    contact_page_url: str | None = None
    technology_stack: list[str] = Field(default_factory=list)

class ContactsSection(StrictOutputModel):
    contact_person: str | None = None
    emails: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)

class SocialLinksSection(StrictOutputModel):
    facebook: HttpUrl | None = None
    instagram: HttpUrl | None = None
    linkedin: HttpUrl | None = None

class ScoringSection(StrictOutputModel):
    lead_score: float | None = None
    priority: str = "Unknown"
    qualification_status: str = "Unknown"

class LeadOutput(StrictOutputModel):
    lead_id: str
    business: BusinessSection
    profile: ProfileSection
    website_analysis: WebsiteSection
    contacts: ContactsSection
    social_links: SocialLinksSection
    lead_scoring: ScoringSection

def compact_scoring_evidence(value: Any) -> None:
    """Trim and deduplicate score evidence before it reaches the lead export."""
    if isinstance(value, dict):
        for name, child in list(value.items()):
            if name == "evidence_used" and isinstance(child, list):
                compact: list[dict[str, Any]] = []
                seen: set[tuple[str, str]] = set()
                for item in child:
                    if not isinstance(item, dict):
                        continue
                    raw_text = item.get("snippet") or item.get("value")
                    text = re.sub(r"\s+", " ", str(raw_text or "")).strip()
                    if len(text) > 200:
                        text = text[:197].rstrip() + "..."
                    sources = item.get("source_urls")
                    source_url = item.get("source_url")
                    if not source_url and isinstance(sources, list):
                        source_url = next((url for url in sources if isinstance(url, str)), None)
                    source_url = source_url if isinstance(source_url, str) else None
                    signature = (text, source_url or "")
                    if not text or signature in seen:
                        continue
                    seen.add(signature)
                    compact.append({"value": text, "source_url": source_url, "snippet": text})
                value[name] = compact
            else:
                compact_scoring_evidence(child)
    elif isinstance(value, list):
        for item in value:
            compact_scoring_evidence(item)


def remove_scoring_evidence(value: Any) -> None:
    """Remove verbose evidence payloads from exported score breakdowns in place."""
    if isinstance(value, dict):
        value.pop("evidence_used", None)
        for child in value.values():
            remove_scoring_evidence(child)
    elif isinstance(value, list):
        for item in value:
            remove_scoring_evidence(item)

DIMENSIONS = {
    "business_relevance": 20,
    "website_quality": 20,
    "business_reputation": 15,
    "digital_presence": 15,
    "contact_accessibility": 15,
    "business_maturity": 15,
}

@dataclass(frozen=True)
class ScoringConfig:
    """Central, tunable rule thresholds. No model-generated judgments are used."""

    minimum_evidence_coverage: float = 60.0
    high_priority_minimum: float = 80.0
    medium_priority_minimum: float = 60.0
    needs_review_minimum: float = 40.0
    relevant_category_keywords: tuple[str, ...] = (
        "dentist", "dental", "doctor", "clinic", "medical", "hospital", "health",
        "lawyer", "legal", "real estate", "property", "school", "college", "education",
        "training", "hotel", "travel", "automotive", "repair", "plumbing", "hvac",
        "marketing", "agency", "consulting", "accounting", "insurance", "financial",
        "retail", "ecommerce", "restaurant", "salon", "fitness", "spa", "logistics",
        "manufacturing", "construction", "home service",
    )
    reference_year: int = field(default_factory=lambda: datetime.now(timezone.utc).year)

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_evidence_coverage <= 100:
            raise ValueError("minimum_evidence_coverage must be between 0 and 100")
        if not 0 <= self.needs_review_minimum <= self.medium_priority_minimum <= self.high_priority_minimum <= 100:
            raise ValueError("score thresholds must satisfy 0 <= review <= medium <= high <= 100")

def _dump(value: Any) -> Any:
    return value.model_dump(mode="python") if hasattr(value, "model_dump") else value

def _mapping(value: Any) -> dict[str, Any]:
    value = _dump(value)
    return value if isinstance(value, dict) else {}

def _text(value: Any) -> str | None:
    value = _dump(value)
    if isinstance(value, str) and value.strip():
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, dict):
        for key in ("value", "text"):
            if isinstance(value.get(key), str):
                return _text(value[key])
    return None

def _sources(metadata: dict[str, Any], key: str, item: Any = None) -> list[str]:
    item = _mapping(item)
    direct = item.get("source_urls") or ([item["source_url"]] if item.get("source_url") else [])
    if direct:
        return list(dict.fromkeys(str(url) for url in direct if url))
    source_map = _mapping(metadata.get("field_sources"))
    urls = source_map.get(key) or []
    return list(dict.fromkeys(str(url) for url in urls if url)) if isinstance(urls, list) else []

def _fact_records(value: Any, metadata: dict[str, Any], source_key: str) -> list[dict[str, Any]]:
    value = _dump(value)
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        text = _text(item)
        if not text:
            continue
        normalized = text.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        output.append({"value": text, "source_urls": _sources(metadata, source_key, item)})
    return output

def _module_status(metadata: dict[str, Any], module: str) -> str:
    statuses = _mapping(metadata.get("module_status"))
    status = statuses.get(module, "")
    return str(getattr(status, "value", status)).casefold()

def _checked(metadata: dict[str, Any], module: str) -> bool:
    return _module_status(metadata, module) in {"success", "partial", "not_available"}

def _url_is_http(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False

def _valid_phone(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    digits = re.sub(r"\D", "", value)
    return 7 <= len(digits) <= 15 and len(set(digits)) > 1

def _valid_email(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    value = value.strip()
    return bool(re.fullmatch(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+", value, re.I))

def _factor(maximum: float, assessable: bool, earned: float = 0, evidence: list[dict[str, Any]] | None = None,
            missing: str | None = None) -> dict[str, Any]:
    evidence = evidence or []
    return {
        "maximum_points": maximum,
        "earned_points": earned if assessable else 0,
        "assessable_weight": maximum if assessable else 0,
        "status": "assessed" if assessable else "unavailable",
        "evidence_used": evidence,
        "missing_or_unavailable": [] if assessable else [missing or "No verified evidence available"],
    }

def _combine_facts(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    by_value: dict[str, dict[str, Any]] = {}
    for group in groups:
        for fact in group:
            key = re.sub(r"\s+", " ", fact["value"]).casefold().strip()
            if key in by_value:
                by_value[key]["source_urls"] = list(dict.fromkeys(
                    by_value[key]["source_urls"] + fact["source_urls"]
                ))
            else:
                copy = {"value": fact["value"], "source_urls": list(fact["source_urls"])}
                by_value[key] = copy
                output.append(copy)
    return output

def classify_score(score: float | None, evidence_coverage: float, config: ScoringConfig | None = None) -> tuple[str, str]:
    """Apply the shared configurable priority and qualification bands."""
    config = config or ScoringConfig()
    if score is None:
        return "Low", "Needs Review"
    priority = (
        "High" if score >= config.high_priority_minimum else
        "Medium" if score >= config.medium_priority_minimum else "Low"
    )
    qualification = (
        "Needs Review" if evidence_coverage < config.minimum_evidence_coverage else
        "Qualified" if score >= config.medium_priority_minimum else
        "Needs Review" if score >= config.needs_review_minimum else "Not Qualified"
    )
    return priority, qualification

def score_lead(lead: Any, config: ScoringConfig | None = None) -> dict[str, Any]:
    """Score one existing enriched lead object; never fetches or invents evidence."""
    config = config or ScoringConfig()
    data = _mapping(lead)
    business = _mapping(data.get("business"))
    profile = _mapping(data.get("profile"))
    website = _mapping(data.get("website_analysis"))
    contacts = _mapping(data.get("contacts"))
    social = _mapping(data.get("social_links"))
    metadata = _mapping(data.get("extraction_metadata"))

    services = _combine_facts(
        _fact_records(profile.get("services"), metadata, "profile.services"),
        _fact_records(website.get("services"), metadata, "website.services"),
    )
    products = _fact_records(profile.get("products"), metadata, "profile.products")
    customers = _fact_records(profile.get("target_customers"), metadata, "profile.target_customers")
    profile_checked = _checked(metadata, "profile")
    website_checked = _checked(metadata, "website")
    contacts_checked = _checked(metadata, "contacts")
    social_checked = _checked(metadata, "social")

    category = _text(business.get("category")) or _text(business.get("sub_category"))
    category_fact = [{"value": category, "source_urls": _sources(metadata, "business.category")} ] if category else []
    relevant = bool(category and any(term.casefold() in category.casefold() for term in config.relevant_category_keywords))
    service_assessable = bool(services) or profile_checked
    customer_assessable = bool(customers) or profile_checked

    dimensions: dict[str, dict[str, Any]] = {}

    # A. Relevance: category match is configurable; explicit offers and audiences are verifiable.
    dimensions["business_relevance"] = {
        "category_relevance": _factor(10, bool(category), 10 if relevant else 0, category_fact,
                                       "Business category unavailable"),
        "service_relevance": _factor(6, service_assessable, 6 if services else 0, services,
                                      "Services not assessed"),
        "target_customer_relevance": _factor(4, customer_assessable, 4 if customers else 0, customers,
                                              "Target customers not assessed"),
    }

    # B. Website: blocked, unreachable, and uncertain sites are unavailable, not poor.
    pages = website.get("website_content") if isinstance(website.get("website_content"), list) else []
    website_status = str(website.get("status") or "not_checked").casefold()
    website_url = business.get("website") or website.get("website_url")
    availability_assessable = website_status in {"active", "not_found", "invalid_url"}
    if website_status == "not_checked" and website_checked:
        availability_assessable = True
        website_status = "not_found" if not website_url else "uncertain"
    website_availability_score = 5 if website_status == "active" and _url_is_http(website_url) else 0
    availability_evidence = ([{"value": f"Website status: {website_status}", "source_urls": [website_url] if _url_is_http(website_url) else []}]
                             if availability_assessable else [])
    dimensions["website_quality"] = {
        "website_availability": _factor(5, availability_assessable, website_availability_score,
                                         availability_evidence, "Website was not assessed or crawl failed"),
    }
    active_pages = pages if website_status == "active" else []
    page_texts = [p.get("main_text", "") for p in active_pages if isinstance(p, dict) and isinstance(p.get("main_text"), str)]
    titles = [p.get("page_title") for p in active_pages if isinstance(p, dict) and p.get("page_title")]
    metas = [p.get("meta_description") for p in active_pages if isinstance(p, dict) and p.get("meta_description")]
    tech_signals = [signal.casefold() for p in active_pages if isinstance(p, dict)
                    for signal in p.get("technology_signals", []) if isinstance(signal, str)]
    has_viewport = any(signal.startswith("meta-viewport:") for signal in tech_signals)
    website_content_assessable = bool(active_pages)
    content_score = (
        int(bool(titles)) + int(bool(metas)) + int(sum(len(text) for text in page_texts) >= 400)
        + int(len(set(website.get("pages_visited") or [p.get("page_url") for p in active_pages])) >= 2)
        + int(any(len(text.split()) >= 80 for text in page_texts))
    )
    dimensions["website_quality"].update({
        "usability_mobile_evidence": _factor(
            5, website_content_assessable, 5 if has_viewport else 0,
            [{"value": signal, "source_urls": [p.get("page_url") for p in active_pages if isinstance(p, dict) and signal in p.get("technology_signals", [])]}
             for signal in tech_signals if signal.startswith("meta-viewport:")],
            "No accessible website content to inspect",
        ),
        "website_content_quality": _factor(
            5, website_content_assessable, content_score,
            [{"value": f"{len(active_pages)} crawled pages; {sum(len(text) for text in page_texts)} text characters",
              "source_urls": [p.get("page_url") for p in active_pages if isinstance(p, dict) and p.get("page_url")] }]
            if website_content_assessable else [], "Website content could not be assessed",
        ),
    })
    about_facts = _combine_facts(
        _fact_records(profile.get("about_info"), metadata, "profile.about_info"),
        _fact_records(website.get("about"), metadata, "website.about"),
    )
    description_facts = _fact_records(profile.get("business_description"), metadata, "profile.business_description")
    if not description_facts and _text(business.get("description")):
        description_facts = [{"value": _text(business.get("description")), "source_urls": _sources(metadata, "business.description")}]
    distinct_descriptions = _combine_facts(about_facts, description_facts)
    raw_hours = _dump(profile.get("operating_hours"))
    if isinstance(raw_hours, dict):
        hours = list(raw_hours.items())
    elif isinstance(raw_hours, list):
        hours = [(item.get("day"), item) for item in raw_hours if isinstance(item, dict) and item.get("day")]
    else:
        hours = []
    operating_hours_evidence = [{"value": str(day) + ": " + str(value), "source_urls": _sources(metadata, "profile.operating_hours")}
                                for day, value in hours]
    address = _text(business.get("address"))
    useful_assessable = website_content_assessable or profile_checked
    about_text_keys = {item["value"].casefold() for item in about_facts}
    distinct_description = [item for item in description_facts if item["value"].casefold() not in about_text_keys]
    useful_items = [
        bool(about_facts), bool(distinct_description), bool(products), bool(operating_hours_evidence), bool(address),
    ]
    useful_evidence = _combine_facts(
        about_facts, distinct_description, products, operating_hours_evidence,
        ([{"value": address, "source_urls": _sources(metadata, "business.address")}] if address else []),
    )
    dimensions["website_quality"]["useful_business_information"] = _factor(
        5, useful_assessable, sum(useful_items), useful_evidence, "Website/profile information not assessed",
    )

    # C. Reputation: only valid, explicitly available Google rating and review counts score.
    rating = business.get("rating")
    rating_valid = isinstance(rating, (int, float)) and not isinstance(rating, bool) and 0 <= rating <= 5
    rating_score = 0 if not rating_valid else (10 if rating >= 4.5 else 8 if rating >= 4 else 6 if rating >= 3.5 else 3 if rating >= 3 else 0)
    rating_ev = [{"value": str(rating), "source_urls": _sources(metadata, "business.rating")}] if rating_valid else []
    reviews = business.get("review_count")
    reviews_valid = isinstance(reviews, int) and not isinstance(reviews, bool) and reviews >= 0
    review_score = 0 if not reviews_valid or reviews == 0 else 1 if reviews <= 10 else 2 if reviews <= 50 else 3 if reviews <= 100 else 4 if reviews <= 500 else 5
    reviews_ev = [{"value": f"{reviews} reviews", "source_urls": _sources(metadata, "business.review_count")}] if reviews_valid else []
    dimensions["business_reputation"] = {
        "google_rating": _factor(10, rating_valid, rating_score, rating_ev, "Valid rating unavailable"),
        "review_volume": _factor(5, reviews_valid, review_score, reviews_ev, "Valid review count unavailable"),
    }

    # D. Digital presence: count only platform-profile URLs linked from the official site.
    official_social: list[dict[str, Any]] = []
    allowed_hosts = {"facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com"}
    for platform in ("facebook", "instagram", "linkedin", "twitter"):
        item = _mapping(social.get(platform))
        value = item.get("url") if item else social.get(platform)
        social_sources = _sources(metadata, f"social.{platform}", item)
        if (_url_is_http(value) and social_sources
                and (urlparse(value).hostname or "").lower().removeprefix("www.") in allowed_hosts):
            official_social.append({"value": value, "source_urls": social_sources})
    dimensions["digital_presence"] = {
        "official_social_profiles": _factor(
            8, social_checked or bool(official_social), min(8, 2 * len(official_social)), official_social,
            "Official social links were not assessed",
        ),
    }
    discovery_source = str(metadata.get("source") or "").casefold()
    listing_evidence = bool(discovery_source in {"google_places", "google_maps_browser"} and business.get("business_name"))
    if listing_evidence:
        listing_used = [{"value": f"Business discovered via {discovery_source}", "source_urls": _sources(metadata, "business.business_name")}]
    else:
        listing_used = []
    dimensions["digital_presence"]["meaningful_online_presence"] = _factor(
        7, listing_evidence, 7 if listing_evidence else 0, listing_used,
        "No verified business listing or online-presence evidence",
    )

    # E. Contact: format validation is local; no messages or calls are made.
    phone_values = [business.get("phone")]
    phone_list = contacts.get("phone_numbers") or []
    if isinstance(phone_list, list):
        phone_values.extend(_mapping(item).get("normalized") or _mapping(item).get("value") or item for item in phone_list)
    has_phone = any(_valid_phone(value) for value in phone_values)
    invalid_phone_present = any(value for value in phone_values if isinstance(value, str)) and not has_phone
    phone_assessed = has_phone or invalid_phone_present or contacts_checked
    phone_evidence = [{"value": str(value), "source_urls": _sources(metadata, "contacts.phone_numbers")} for value in phone_values if _valid_phone(value)]
    email_values = [business.get("email")]
    email_list = contacts.get("emails") or []
    if isinstance(email_list, list):
        email_values.extend(_mapping(item).get("email") or item for item in email_list)
    has_email = any(_valid_email(value) for value in email_values)
    invalid_email_present = any(value for value in email_values if isinstance(value, str)) and not has_email
    email_assessed = has_email or invalid_email_present or contacts_checked
    email_evidence = [{"value": str(value), "source_urls": _sources(metadata, "contacts.emails")} for value in email_values if _valid_email(value)]
    contact_url = contacts.get("contact_page_url") or website.get("contact_page_url")
    contact_assessed = _url_is_http(contact_url) or website_content_assessable or contacts_checked
    dimensions["contact_accessibility"] = {
        "valid_business_phone": _factor(5, phone_assessed, 5 if has_phone else 0, phone_evidence,
                                          "Business phone not assessed"),
        "valid_business_email": _factor(5, email_assessed, 5 if has_email else 0, email_evidence,
                                          "Business email not assessed"),
        "contact_page": _factor(5, contact_assessed, 5 if _url_is_http(contact_url) else 0,
                                 [{"value": contact_url, "source_urls": [contact_url]}] if _url_is_http(contact_url) else [],
                                 "Contact page not assessed"),
    }
    # Invalid values are explicitly disclosed without awarding points.
    if invalid_phone_present:
        dimensions["contact_accessibility"]["valid_business_phone"]["missing_or_unavailable"] = ["Phone evidence present but invalid"]
    if invalid_email_present:
        dimensions["contact_accessibility"]["valid_business_email"]["missing_or_unavailable"] = ["Email evidence present but invalid"]

    # F. Maturity: only explicit scale/history text and deduplicated offerings count.
    pages_text = [p.get("main_text", "") for p in pages if isinstance(p, dict) and isinstance(p.get("main_text"), str)]
    maturity_text = "\n".join(pages_text + [fact["value"] for fact in about_facts + description_facts]).casefold()

    def matching_page_sources(phrase: str) -> list[str]:
        needle = phrase.casefold()
        return list(dict.fromkeys(
            str(page["page_url"]) for page in pages
            if isinstance(page, dict) and page.get("page_url") and needle in str(page.get("main_text", "")).casefold()
        ))

    scale_ev: list[dict[str, Any]] = []
    scale_assessed = False
    scale_score = 0
    count_patterns = (
        re.compile(r"\b(?:team|staff|workforce|employees?)\s*(?:of|:)?\s*(\d{1,6})\b", re.I),
        re.compile(r"\b(\d{1,6})\s+(?:employees?|team members|staff|professionals)\b", re.I),
    )
    sizes = [int(match.group(1)) for pattern in count_patterns for match in pattern.finditer(maturity_text)]
    if sizes:
        scale_assessed = True
        size = max(sizes)
        scale_score = 5 if size >= 10 else 3 if size >= 2 else 1
        phrase = next((match.group(0) for pattern in count_patterns for match in pattern.finditer(maturity_text)), str(size))
        scale_ev = [{"value": phrase, "source_urls": matching_page_sources(phrase) or _sources(metadata, "profile.about_info")}]
    elif re.search(r"\b(?:multiple|several)\s+(?:offices|locations|branches)\b", maturity_text):
        scale_assessed, scale_score = True, 4
        phrase = re.search(r"\b(?:multiple|several)\s+(?:offices|locations|branches)\b", maturity_text).group(0)
        scale_ev = [{"value": phrase, "source_urls": matching_page_sources(phrase) or _sources(metadata, "profile.about_info")}]
    elif re.search(r"\b(?:our|the)\s+team\b", maturity_text):
        scale_assessed, scale_score = True, 2
        phrase = re.search(r"\b(?:our|the)\s+team\b", maturity_text).group(0)
        scale_ev = [{"value": phrase, "source_urls": matching_page_sources(phrase) or _sources(metadata, "profile.about_info")}]

    history_matches = list(re.finditer(r"\b(?:(?:founded|established)(?:\s+in)?|serving\s+(?:customers|clients)?\s*since|since)\s+(19\d{2}|20\d{2})\b", maturity_text, re.I))
    history_assessed = bool(history_matches)
    history_score = 0
    history_evidence: list[dict[str, Any]] = []
    if history_matches:
        year = int(history_matches[0].group(1))
        age = max(0, config.reference_year - year)
        history_score = 5 if age >= 10 else 4 if age >= 5 else 3 if age >= 2 else 2
        history_evidence = [{"value": history_matches[0].group(0), "source_urls": matching_page_sources(history_matches[0].group(0)) or _sources(metadata, "profile.about_info")}]
    else:
        experience = re.search(r"\b(\d{1,2})\+?\s+years?\s+(?:of\s+)?(?:experience|in\s+business)\b", maturity_text, re.I)
        if experience:
            years = int(experience.group(1))
            history_assessed = True
            history_score = 5 if years >= 10 else 4 if years >= 5 else 3 if years >= 2 else 2
            history_evidence = [{"value": experience.group(0), "source_urls": matching_page_sources(experience.group(0)) or _sources(metadata, "profile.about_info")}]

    offers = _combine_facts(services, products)
    breadth_assessed = bool(offers) or profile_checked
    breadth_score = min(5, len(offers))
    dimensions["business_maturity"] = {
        "team_or_business_scale": _factor(5, scale_assessed, scale_score, scale_ev, "No explicit size evidence"),
        "operating_history": _factor(5, history_assessed, history_score, history_evidence,
                                      "No explicit operating-history evidence"),
        "offering_breadth": _factor(5, breadth_assessed, breadth_score, offers,
                                    "Offerings were not assessed"),
    }

    breakdown: dict[str, Any] = {}
    total_earned = 0.0
    assessable_weight = 0.0
    for name, maximum in DIMENSIONS.items():
        factors = dimensions[name]
        earned = sum(item["earned_points"] for item in factors.values())
        assessed = sum(item["assessable_weight"] for item in factors.values())
        evidence = [e for item in factors.values() for e in item["evidence_used"]]
        missing = [f"{factor}: {entry}" for factor, item in factors.items()
                   for entry in item["missing_or_unavailable"]]
        breakdown[name] = {
            "maximum_points": maximum,
            "earned_points": round(earned, 2),
            "assessable_weight": round(assessed, 2),
            "factors": factors,
            "evidence_used": evidence,
            "missing_or_unavailable": missing,
        }
        total_earned += earned
        assessable_weight += assessed

    coverage = round(assessable_weight, 1)  # The assessable maximum is 100 points.
    score = round((total_earned / assessable_weight) * 100, 1) if assessable_weight else None
    priority, qualification = classify_score(score, coverage, config)
    breakdown["evidence_coverage"] = coverage
    breakdown["assessable_weight"] = round(assessable_weight, 1)
    breakdown["provisional_earned_points"] = round(total_earned, 2)
    breakdown["normalization"] = "earned_points / assessable_weight * 100"
    return {
        "lead_score": score,
        "priority": priority,
        "qualification_status": qualification,
        "score_breakdown": breakdown,
    }
