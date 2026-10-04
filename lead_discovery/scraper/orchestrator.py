from __future__ import annotations

import json
import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from pydantic import BaseModel, Field

from .browser import BrowserConfig
from .contact import ContactExtraction, extract_contacts, normalize_phone
from .discovery import BusinessDiscovery
from .models import BusinessRecord
from .output_schema import LeadOutput, compact_scoring_evidence
from .profile import BusinessProfile, build_business_profile, extract_profile_about, extract_profile_services
from .social import SocialMediaExtraction, extract_social_media
from .scoring import ScoringConfig, score_lead
from .technology import TechnologyExtraction, detect_technologies
from .website import WebsiteCrawlResult, WebsiteCrawler, WebsiteCrawlerConfig, WebsitePage, WebsiteStatus

logger = logging.getLogger(__name__)


class ExtractionMode(str, Enum):
    BASIC = "basic"
    WEBSITE = "website"
    PROFILE = "profile"
    CONTACTS = "contacts"
    SOCIAL = "social"
    TECHNOLOGY = "technology"
    FULL = "full"
    SCORING = "scoring"
    CUSTOM = "custom"


class ModuleStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_REQUESTED = "not_requested"
    NOT_AVAILABLE = "not_available"
    SKIPPED = "skipped"


MODULES = ("business_discovery", "website", "profile", "contacts", "social", "technology", "lead_scoring")
MUST_COVERAGE_FIELDS = ("business.latitude", "business.longitude", "business.review_count", "business.rating")
PROFILE_FIELDS = ("services", "products", "target_customers", "about_info", "business_description", "operating_hours")
BUSINESS_FIELDS = ("business_name", "category", "sub_category", "description", "address", "phone",
                   "email", "website", "latitude", "longitude", "rating", "review_count")
WEBSITE_FIELDS = ("status", "website_url", "final_url", "website_content", "about", "services",
                  "contact_page_url", "technology_stack", "pages_visited", "crawl_errors")
CONTACT_FIELDS = ("contact_person", "emails", "phone_numbers", "contact_page_url")
FIELD_MODULES = {
    **{field: "profile" for field in PROFILE_FIELDS},
    **{field: "business_discovery" for field in BUSINESS_FIELDS},
    **{field: "website" for field in WEBSITE_FIELDS if field not in {"technology_stack", "services"}},
    "contact_person": "contacts", "emails": "contacts", "email": "contacts",
    "phone": "contacts", "phone_numbers": "contacts", "contact_page_url": "contacts",
    **{field: "social" for field in ("facebook", "instagram", "linkedin", "twitter", "social_links")},
    "technology_stack": "technology", "website_analysis": "website", "website_content": "website",
    "lead_scoring": "lead_scoring",
    "business": "business_discovery", "business_info": "business_discovery",
}


class BusinessOutput(BaseModel):
    lead_id: str
    business_name: str
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
    review_count: int | None = None


class LeadScoringOutput(BaseModel):
    lead_score: float | None = None
    priority: str = "Unknown"
    qualification_status: str = "Unknown"
    score_breakdown: dict[str, Any] = Field(default_factory=dict)


class ExtractionMetadata(BaseModel):
    requested_modules: list[str]
    completed_modules: list[str] = Field(default_factory=list)
    module_status: dict[str, ModuleStatus]
    requested_fields: list[str] = Field(default_factory=list)
    source: str | None = None
    pages_visited: list[str] = Field(default_factory=list)
    field_sources: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    discovery_status: str = "success"
    enrichment_status: str = "not_requested"
    overall_status: str = "success"
    extraction_status: str


class CanonicalLeadOutput(BaseModel):
    business: BusinessOutput
    profile: dict[str, Any] | None = None
    website_analysis: dict[str, Any] | None = None
    contacts: dict[str, Any] | None = None
    social_links: dict[str, Any] | None = None
    lead_scoring: LeadScoringOutput = Field(default_factory=LeadScoringOutput)
    extraction_metadata: ExtractionMetadata


@dataclass
class ScrapeRun:
    run_id: str
    output_dir: Path
    leads: list[CanonicalLeadOutput]
    summary: dict[str, Any]
    extraction_report: dict[str, Any]


def _error_text(exc: Exception) -> str:
    message = re.sub(r"https?://\S+", "[url]", str(exc)[:400], flags=re.I)
    message = re.sub(r"(?i)(password|passwd|token|api[_-]?key|secret)=([^&\s]+)", r"\1=[redacted]", message)
    return f"{type(exc).__name__}: {message}"


def _modules_for_mode(mode: ExtractionMode) -> list[str]:
    return {
        ExtractionMode.BASIC: ["business_discovery"],
        ExtractionMode.WEBSITE: ["website"],
        ExtractionMode.PROFILE: ["profile"],
        ExtractionMode.CONTACTS: ["contacts"],
        ExtractionMode.SOCIAL: ["social"],
        ExtractionMode.TECHNOLOGY: ["technology"],
        ExtractionMode.FULL: list(MODULES),
        ExtractionMode.SCORING: ["business_discovery", "lead_scoring"],
        ExtractionMode.CUSTOM: [],
    }[mode]


def _custom_selection(
    modules: Iterable[str] | None, fields: Iterable[str] | None,
) -> tuple[list[str], dict[str, set[str]], set[str]]:
    selected: list[str] = []
    whole: set[str] = set()
    by_field: dict[str, set[str]] = defaultdict(set)
    aliases = {"basic": "business_discovery", "website_analysis": "website", "social_links": "social"}
    for raw in modules or ():
        name = aliases.get(raw.strip().lower(), raw.strip().lower())
        if name not in MODULES:
            raise ValueError(f"Unsupported custom module: {raw}")
        if name not in selected:
            selected.append(name)
        whole.add(name)
    for raw in fields or ():
        field = raw.strip().lower()
        module = FIELD_MODULES.get(field)
        if module is None:
            raise ValueError(f"Unsupported custom field: {raw}")
        if module not in selected:
            selected.append(module)
        by_field[module].add(field)
        if field == "website_analysis":
            whole.add("website")
    if not selected:
        raise ValueError("CUSTOM mode requires at least one --module or --field")
    return selected, dict(by_field), whole


def _requested_field_paths(mode: ExtractionMode, selected: list[str], fields: Iterable[str] | None, whole: set[str]) -> list[str]:
    paths = [f"business.{name}" for name in BUSINESS_FIELDS]
    all_by_module = {
        "website": [f"website_analysis.{name}" for name in WEBSITE_FIELDS if name != "technology_stack"],
        "profile": [f"profile.{name}" for name in PROFILE_FIELDS],
        "contacts": [f"contacts.{name}" for name in CONTACT_FIELDS],
        "social": [f"social_links.{name}" for name in ("facebook", "instagram", "linkedin", "twitter")],
        "technology": ["website_analysis.technology_stack"],
    }
    if mode != ExtractionMode.CUSTOM:
        for module in selected:
            paths.extend(all_by_module.get(module, []))
    else:
        for module in selected:
            if module in whole:
                paths.extend(all_by_module.get(module, []))
        for raw in fields or ():
            name = raw.strip().lower()
            module = FIELD_MODULES[name]
            if name in BUSINESS_FIELDS:
                paths.append(f"business.{name}")
            elif module == "profile":
                paths.append(f"profile.{name}")
            elif module == "contacts":
                contact_name = {"email": "emails", "phone": "phone_numbers"}.get(name, name)
                paths.append(f"contacts.{contact_name}")
            elif module == "social":
                if name == "social_links":
                    paths.extend(all_by_module["social"])
                else:
                    paths.append(f"social_links.{name}")
            elif module == "technology":
                paths.append("website_analysis.technology_stack")
            elif module == "website":
                paths.append(f"website_analysis.{name}")
    return list(dict.fromkeys(paths))


def _profile_payload(profile: BusinessProfile, only_fields: set[str] | None = None) -> dict[str, Any]:
    data = profile.model_dump(mode="json")
    if only_fields is not None:
        for name in PROFILE_FIELDS:
            if name not in only_fields:
                data[name] = None
        data["field_sources"] = {key: val for key, val in data["field_sources"].items() if key in only_fields}
    return data


def _website_payload(site: WebsiteCrawlResult, technology: TechnologyExtraction | None = None) -> dict[str, Any]:
    return {
        "status": site.website_status.value,
        "website_url": site.website_url,
        "final_url": site.final_url,
        "website_content": [page.model_dump(mode="json") for page in site.website_content],
        "about": site.about,
        "services": site.services,
        "contact_page_url": site.contact_page_url,
        "technology_stack": [item.model_dump(mode="json") for item in technology.technologies] if technology else None,
        "pages_visited": site.pages_visited,
        "crawl_errors": [item.model_dump(mode="json") for item in site.crawl_errors],
    }


def _contact_payload(contact: ContactExtraction) -> dict[str, Any]:
    return {
        "contact_person": contact.contact_person.model_dump(mode="json") if contact.contact_person else None,
        "emails": [item.model_dump(mode="json") for item in contact.contact_emails],
        "phone_numbers": [item.model_dump(mode="json") for item in contact.phone_numbers],
        "contact_page_url": contact.contact_page_url,
    }


def _social_payload(social: SocialMediaExtraction) -> dict[str, Any]:
    return {key: getattr(social, key).model_dump(mode="json") if getattr(social, key) else None
            for key in ("facebook", "instagram", "linkedin", "twitter")}


def _plain_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = profile or {}
    def facts(key: str) -> list[str]:
        value = profile.get(key)
        if value is None:  # A custom-mode field that was not requested.
            return []
        if not isinstance(value, list):
            raise TypeError(f"profile.{key} expected array of evidence objects, got {type(value).__name__}")
        output = []
        for index, item in enumerate(value):
            if not isinstance(item, dict) or not isinstance(item.get("value"), str):
                raise TypeError(f"profile.{key}[{index}] expected evidence object with string value")
            if item["value"].strip():
                output.append(item["value"].strip())
        return output
    about_items = facts("about_info")
    description = profile.get("business_description")
    if isinstance(description, dict):
        description = description.get("value")
    hours: dict[str, Any] = {}
    raw_hours = profile.get("operating_hours")
    if raw_hours is not None and not isinstance(raw_hours, list):
        raise TypeError(f"profile.operating_hours expected array, got {type(raw_hours).__name__}")
    for index, item in enumerate(raw_hours or []):
        if not isinstance(item, dict) or not isinstance(item.get("day"), str) or not item["day"].strip():
            raise TypeError(f"profile.operating_hours[{index}] expected object with day")
        hours[item["day"]] = {"opens": item.get("opens"), "closes": item.get("closes"),
                               "closed": bool(item.get("closed"))}
    return {
        "services": facts("services"), "products": facts("products"),
        "target_customers": facts("target_customers"),
        "about_info": " ".join(about_items) or None,
        "business_description": description if isinstance(description, str) and description.strip() else None,
        "operating_hours": hours,
    }


def _clean_lead_payload(lead: CanonicalLeadOutput) -> dict[str, Any]:
    internal = lead.model_dump(mode="json")
    business = dict(internal["business"])
    lead_id = business.pop("lead_id")
    profile = _plain_profile(internal.get("profile"))
    website = internal.get("website_analysis") or {}
    profile_services = profile["services"]
    pages = [WebsitePage.model_validate(page) for page in (website.get("website_content") or [])]
    if not profile_services and pages:
        profile_services = [fact.value for fact in extract_profile_services(pages)]
    website_about = profile["about_info"]
    if not website_about and pages:
        about_facts = extract_profile_about(pages)
        website_about = " ".join(item.value for item in about_facts) or None
    raw_technology = website.get("technology_stack")
    if raw_technology is not None and not isinstance(raw_technology, list):
        raise TypeError(f"website_analysis.technology_stack expected array, got {type(raw_technology).__name__}")
    technology_stack = []
    for index, item in enumerate(raw_technology or []):
        if isinstance(item, str):
            technology_stack.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            technology_stack.append(item["name"])
        else:
            raise TypeError(f"website_analysis.technology_stack[{index}] expected technology name")
    clean_website = {
        "status": website.get("status") or "not_checked",
        "website_url": website.get("website_url") if website.get("status") != "not_checked" else None,
        "about": website_about,
        "services": list(dict.fromkeys(profile_services)),
        "contact_page_url": website.get("contact_page_url"),
        "technology_stack": technology_stack,
    }
    contacts = internal.get("contacts") or {"contact_person": None, "emails": [], "phone_numbers": [],
                                               "contact_page_url": None}
    person = contacts.get("contact_person")
    def contact_values(name: str, primary: str, secondary: str | None = None) -> list[str]:
        raw = contacts.get(name)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise TypeError(f"contacts.{name} expected array, got {type(raw).__name__}")
        values = []
        for index, item in enumerate(raw):
            if isinstance(item, str):
                values.append(item)
            elif isinstance(item, dict):
                value = item.get(primary) or (item.get(secondary) if secondary else None)
                if not isinstance(value, str):
                    raise TypeError(f"contacts.{name}[{index}] missing verified string value")
                values.append(value)
            else:
                raise TypeError(f"contacts.{name}[{index}] expected string or evidence object")
        return values
    if isinstance(person, dict):
        person = person.get("value")
        if person is not None and not isinstance(person, str):
            raise TypeError("contacts.contact_person expected string evidence value")
    contacts = {"contact_person": person,
                "emails": contact_values("emails", "email"),
                "phone_numbers": contact_values("phone_numbers", "normalized", "value"),
                "contact_page_url": contacts.get("contact_page_url")}
    social = internal.get("social_links") or {key: None for key in ("facebook", "instagram", "linkedin", "twitter")}
    social = {key: value.get("url") if isinstance(value, dict) else value
              for key, value in social.items()}
    scoring = internal["lead_scoring"]
    scoring["priority"] = scoring.get("priority") or "Unknown"
    scoring["qualification_status"] = scoring.get("qualification_status") or "Unknown"
    compact_scoring_evidence(scoring.get("score_breakdown", {}))
    return {"lead_id": lead_id, "business": business, "profile": profile,
            "website_analysis": clean_website, "contacts": contacts,
            "social_links": social, "lead_scoring": scoring}


def _validate_output_payloads(leads: list[CanonicalLeadOutput]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Serialize and validate every exported record before any artifact is written."""
    payloads: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for lead in leads:
        lead_id = str(lead.business.lead_id or "<missing>")
        try:
            candidate = _clean_lead_payload(lead)
            lead_id = str(candidate.get("lead_id", "<missing>"))
            validated = LeadOutput.model_validate(candidate)
        except Exception as exc:
            details = getattr(exc, "errors", lambda: [])()
            for detail in details:
                loc = ".".join(str(part) for part in detail.get("loc", ())) or "<record>"
                actual = type(detail.get("input")).__name__
                error_type = detail.get("type", "")
                expected = {
                    "list_type": "array/list", "dict_type": "object/dict", "string_type": "string",
                    "float_type": "number", "int_type": "integer", "bool_type": "boolean",
                    "missing": "required field",
                    "extra_forbidden": "no unexpected field",
                    "url_parsing": "valid HTTP or HTTPS URL",
                }.get(error_type, detail.get("msg", "valid schema value"))
                errors.append({"lead_id": lead_id, "field": loc, "expected": expected, "actual": actual})
            if not details:
                message = str(exc)
                field = message.split(" expected ", 1)[0] if " expected " in message else "<serialization>"
                expected = message.split(" expected ", 1)[1].split(", got ", 1)[0] if " expected " in message else "valid canonical output"
                actual = message.rsplit(", got ", 1)[1] if ", got " in message else type(exc).__name__
                errors.append({"lead_id": lead_id, "field": field, "expected": expected, "actual": actual})
        else:
            payloads.append(validated.model_dump(mode="json"))
    if errors:
        formatted = "; ".join(
            f"lead_id={item['lead_id']} field={item['field']} expected={item['expected']} actual={item['actual']}"
            for item in errors
        )
        raise ValueError("Canonical lead output validation failed: " + formatted)
    return payloads, {
        "status": "valid", "schema": "LeadOutput", "validated_records": len(payloads), "errors": [],
    }


class ScraperOrchestrator:
    def __init__(
        self,
        *,
        browser_config: BrowserConfig | None = None,
        website_config: WebsiteCrawlerConfig | None = None,
        discovery: Any | None = None,
        website_crawler_factory: Callable[..., Any] = WebsiteCrawler,
        output_root: str | Path = "scraper_outputs",
        progress: Callable[[str], None] | None = None,
        verbose: bool = False,
        scoring_config: ScoringConfig | None = None,
    ) -> None:
        self.browser_config = browser_config or BrowserConfig()
        self.website_config = website_config or WebsiteCrawlerConfig()
        self.discovery = discovery or BusinessDiscovery(self.browser_config)
        self.website_crawler_factory = website_crawler_factory
        self.output_root = Path(output_root)
        self.progress = progress
        self.verbose = verbose
        self.scoring_config = scoring_config or ScoringConfig()

    def _emit(self, message: str) -> None:
        if self.progress:
            self.progress(message)

    def _emit_detail(self, message: str) -> None:
        if self.verbose:
            self._emit(message)

    async def run(
        self,
        query: str,
        location: str,
        limit: int = 50,
        mode: ExtractionMode | str = ExtractionMode.BASIC,
        *,
        custom_modules: Iterable[str] | None = None,
        custom_fields: Iterable[str] | None = None,
        qualification: str = "mixed",
        candidate_limit: int | None = None,
        businesses: list[BusinessRecord] | None = None,
        website_results: dict[str, WebsiteCrawlResult] | None = None,
    ) -> ScrapeRun:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query is required")
        if not isinstance(location, str) or not location.strip():
            raise ValueError("location is required")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("result limit must be a positive integer")
        qualification_aliases = {
            "qualified": "Qualified", "needs_review": "Needs Review",
            "not_qualified": "Not Qualified", "mixed": None,
        }
        qualification_key = str(qualification).strip().lower()
        if qualification_key not in qualification_aliases:
            raise ValueError("qualification must be qualified, needs_review, not_qualified, or mixed")
        if candidate_limit is not None and (
            isinstance(candidate_limit, bool) or not isinstance(candidate_limit, int) or candidate_limit < limit
        ):
            raise ValueError("candidate limit must be an integer greater than or equal to --limit")
        try:
            selected_mode = mode if isinstance(mode, ExtractionMode) else ExtractionMode(str(mode).lower())
        except ValueError as exc:
            raise ValueError(f"Invalid extraction mode: {mode}") from exc
        requested = _modules_for_mode(selected_mode)
        field_selection: dict[str, set[str]] = {}
        whole_modules = set(requested)
        if selected_mode == ExtractionMode.CUSTOM:
            requested, field_selection, whole_modules = _custom_selection(custom_modules, custom_fields)
        requested_field_paths = _requested_field_paths(selected_mode, requested, custom_fields, whole_modules)
        selected = set(requested)
        if qualification_key != "mixed" and "lead_scoring" not in selected:
            raise ValueError("qualification filters require a mode that runs lead_scoring (full or scoring)")
        discovery_limit = limit
        if qualification_key != "mixed":
            discovery_limit = candidate_limit or min(max(limit * 5, limit + 20), max(60, limit))
        needs_enrichment = bool(selected & {"profile", "contacts", "social", "technology"})
        needs_website = "website" in selected or needs_enrichment

        started = datetime.now(timezone.utc)
        run_id = started.strftime("run_%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
        output_dir = self.output_root / run_id
        output_dir.mkdir(parents=True, exist_ok=False)
        logs = [f"[START] {started.isoformat()} run_id={run_id}",
                f"[INFO] mode={selected_mode.value} query={query.strip()} location={location.strip()} limit={limit}"]
        self._emit("[START] Scraper execution started")
        self._emit(f"[INFO] Mode: {selected_mode.value.upper()}")
        self._emit(f"[INFO] Search query: {query.strip()}")
        self._emit(f"[INFO] Location: {location.strip()}")
        self._emit(f"[INFO] Requested limit: {limit}")
        if qualification_key != "mixed":
            self._emit(f"[INFO] Qualification: {qualification_key}; candidate pool limit: {discovery_limit}")

        run_warnings: list[str] = []
        if businesses is None:
            try:
                records = (await self.discovery.search(query.strip(), location.strip(), discovery_limit))[:discovery_limit]
                discovery_status = ModuleStatus.SUCCESS
            except Exception as exc:
                records = []
                discovery_status = ModuleStatus.FAILED
                run_warnings.append("Business discovery failed: " + _error_text(exc))
        else:
            records = businesses[:discovery_limit]
            discovery_status = ModuleStatus.SUCCESS
        discovery_diagnostics = getattr(self.discovery, "diagnostics", {}) if businesses is None else {
            "source": "provided_records", "fallback_used": False, "fallback_reason": None,
        }
        logs.append("[DISCOVERY_SOURCE] " + json.dumps(discovery_diagnostics, ensure_ascii=False, default=str))
        logs.extend("[WARNING] " + warning for warning in run_warnings)
        logs.append(f"[DISCOVERY] businesses={len(records)} status={discovery_status.value}")
        self._emit(f"[DISCOVERY] Business discovery completed ({len(records)} businesses)")
        self._emit(
            f"[DISCOVERY] Source: {discovery_diagnostics.get('source', 'unknown')}; "
            f"browser fallback: {'used' if discovery_diagnostics.get('fallback_used') else 'not used'}"
        )

        supplied_sites = website_results or {}
        leads: list[CanonicalLeadOutput] = []
        events: list[dict[str, Any]] = []
        for record in records:
            statuses = {name: ModuleStatus.NOT_REQUESTED for name in MODULES}
            statuses["business_discovery"] = discovery_status
            warnings: list[str] = []
            field_sources: dict[str, list[str]] = {}
            site = supplied_sites.get(record.lead_id)
            profile = contact = social = technology = None

            record_source = record.source_url or record.source_ref
            for name in BusinessOutput.model_fields:
                if name != "lead_id" and getattr(record, name) is not None:
                    field_sources[f"business.{name}"] = [record_source] if record_source else []

            if needs_website:
                if site is None:
                    try:
                        crawler = self.website_crawler_factory(self.website_config, self.browser_config)
                        site = await crawler.crawl(record)
                    except Exception as exc:
                        statuses["website"] = ModuleStatus.FAILED
                        warnings.append("Website extraction failed: " + _error_text(exc))
                        logs.append(f"[ERROR] business={record.lead_id} module=website {_error_text(exc)}")
                if site is not None:
                    if site.website_status == WebsiteStatus.NOT_FOUND:
                        statuses["website"] = ModuleStatus.NOT_AVAILABLE
                    elif site.website_status in {WebsiteStatus.INVALID_URL, WebsiteStatus.UNREACHABLE, WebsiteStatus.BLOCKED}:
                        statuses["website"] = ModuleStatus.FAILED
                    elif site.website_status == WebsiteStatus.UNCERTAIN or site.crawl_errors or not site.website_content:
                        statuses["website"] = ModuleStatus.PARTIAL
                    else:
                        statuses["website"] = ModuleStatus.SUCCESS
                    field_sources.update({f"website.{key}": urls for key, urls in site.field_sources.items()})
                    warnings.extend("Website crawl: " + item.message for item in site.crawl_errors)
                logs.append(f"[WEBSITE] business={record.lead_id} status={statuses['website'].value} pages={len(site.pages_visited) if site else 0}")
                self._emit_detail(f"[WEBSITE] business={record.lead_id} status={statuses['website'].value}")

            has_pages = bool(site and site.website_content)
            failed: list[str] = ["website"] if statuses["website"] == ModuleStatus.FAILED else []
            if "profile" in selected:
                if not has_pages:
                    statuses["profile"] = ModuleStatus.SKIPPED
                    warnings.append("Profile skipped: website content is unavailable.")
                else:
                    try:
                        profile = build_business_profile(record, site)
                        wanted = (field_selection.get('profile', set(PROFILE_FIELDS)) if 'profile' not in whole_modules else set(PROFILE_FIELDS))
                        for key in wanted:
                            field_sources[f"profile.{key}"] = profile.field_sources.get(key, [])
                        values = [getattr(profile, key) for key in wanted]
                        statuses["profile"] = (
                            ModuleStatus.NOT_AVAILABLE if not any(values)
                            else ModuleStatus.SUCCESS if all(values) else ModuleStatus.PARTIAL
                        )
                    except Exception as exc:
                        statuses["profile"] = ModuleStatus.FAILED
                        warnings.append("Profile extraction failed: " + _error_text(exc))
                if statuses["profile"] == ModuleStatus.FAILED and "profile" not in failed:
                    failed.append("profile")
                logs.append(f"[PROFILE] business={record.lead_id} status={statuses['profile'].value}")
                self._emit_detail(f"[PROFILE] business={record.lead_id} status={statuses['profile'].value}")

            if "contacts" in selected:
                try:
                    contact_site = site or WebsiteCrawlResult(lead_id=record.lead_id, website_status=WebsiteStatus.NOT_FOUND)
                    contact = extract_contacts(record, contact_site)
                    exists = bool(contact.contact_person or contact.contact_emails or contact.phone_numbers or contact.contact_page_url)
                    statuses["contacts"] = ModuleStatus.SUCCESS if exists else ModuleStatus.NOT_AVAILABLE
                    field_sources.update({f"contacts.{key}": urls for key, urls in contact.field_sources.items()})
                except Exception as exc:
                    statuses["contacts"] = ModuleStatus.FAILED
                    warnings.append("Contact extraction failed: " + _error_text(exc))
                if statuses["contacts"] == ModuleStatus.FAILED and "contacts" not in failed:
                    failed.append("contacts")
                logs.append(f"[CONTACTS] business={record.lead_id} status={statuses['contacts'].value}")
                self._emit_detail(f"[CONTACTS] business={record.lead_id} status={statuses['contacts'].value}")

            if "social" in selected:
                if not has_pages:
                    statuses["social"] = ModuleStatus.SKIPPED
                    warnings.append("Social extraction skipped: website links are unavailable.")
                else:
                    try:
                        social = extract_social_media(record, site)
                        exists = any(getattr(social, key) for key in ("facebook", "instagram", "linkedin", "twitter"))
                        statuses["social"] = ModuleStatus.SUCCESS if exists else ModuleStatus.NOT_AVAILABLE
                        field_sources.update({f"social.{key}": val for key, val in social.field_sources.items()})
                    except Exception as exc:
                        statuses["social"] = ModuleStatus.FAILED
                        warnings.append("Social extraction failed: " + _error_text(exc))
                if statuses["social"] == ModuleStatus.FAILED and "social" not in failed:
                    failed.append("social")
                logs.append(f"[SOCIAL] business={record.lead_id} status={statuses['social'].value}")
                self._emit_detail(f"[SOCIAL] business={record.lead_id} status={statuses['social'].value}")

            if "technology" in selected:
                if not has_pages:
                    statuses["technology"] = ModuleStatus.SKIPPED
                    warnings.append("Technology detection skipped: website evidence is unavailable.")
                else:
                    try:
                        technology = detect_technologies(site)
                        statuses["technology"] = ModuleStatus.SUCCESS if technology.technologies else ModuleStatus.NOT_AVAILABLE
                        field_sources["technology.technologies"] = technology.field_sources.get("technologies", [])
                    except Exception as exc:
                        statuses["technology"] = ModuleStatus.FAILED
                        warnings.append("Technology detection failed: " + _error_text(exc))
                if statuses["technology"] == ModuleStatus.FAILED and "technology" not in failed:
                    failed.append("technology")
                logs.append(f"[TECHNOLOGY] business={record.lead_id} status={statuses['technology'].value}")
                self._emit_detail(f"[TECHNOLOGY] business={record.lead_id} status={statuses['technology'].value}")

            values = record.model_dump()
            if values.get("phone"):
                values["phone"] = normalize_phone(values["phone"]) or values["phone"]
            if contact:
                get_email = selected_mode != ExtractionMode.CUSTOM or "contacts" in whole_modules or bool(field_selection.get("contacts", set()) & {"email", "emails"})
                get_phone = selected_mode != ExtractionMode.CUSTOM or "contacts" in whole_modules or bool(field_selection.get("contacts", set()) & {"phone", "phone_numbers"})
                if get_email: values["email"] = values.get("email") or contact.email
                if get_phone: values["phone"] = normalize_phone(values.get("phone")) or contact.phone or values.get("phone")
                if contact.email:
                    field_sources["business.email"] = contact.field_sources.get("contact_emails", [])
                if contact.phone:
                    field_sources["business.phone"] = contact.field_sources.get("phone_numbers", [])
            business_out = BusinessOutput(**{key: values.get(key) for key in BusinessOutput.model_fields})

            if "profile" in selected:
                profile_subset = field_selection.get('profile') if 'profile' not in whole_modules else None
                profile_out = _profile_payload(profile, profile_subset) if profile else {
                    "lead_id": record.lead_id, "business_name": record.business_name,
                    **{key: None for key in PROFILE_FIELDS}, "field_sources": {},
                }
            else:
                profile_out = None
            website_out = _website_payload(site, technology if "technology" in selected else None) if (
                site is not None and ("website" in selected or "technology" in selected)
            ) else None
            if ("website" in selected or "technology" in selected) and website_out is None:
                website_out = _empty_website(record.website, "unreachable")
            if website_out is not None and selected_mode == ExtractionMode.CUSTOM and "website" not in whole_modules:
                allowed = field_selection.get("website", set())
                for key in WEBSITE_FIELDS:
                    if key not in allowed:
                        website_out[key] = None
            contacts_out = _contact_payload(contact) if contact and "contacts" in selected else (
                {"contact_person": None, "emails": [], "phone_numbers": [], "contact_page_url": None}
                if "contacts" in selected else None
            )
            if contacts_out is not None and selected_mode == ExtractionMode.CUSTOM and "contacts" not in whole_modules:
                allowed = set(field_selection.get("contacts", set()))
                if "email" in allowed: allowed.add("emails")
                if "phone" in allowed: allowed.add("phone_numbers")
                for key in CONTACT_FIELDS:
                    if key not in allowed:
                        contacts_out[key] = [] if key in {"emails", "phone_numbers"} else None
            social_out = _social_payload(social) if social and "social" in selected else (
                {key: None for key in ("facebook", "instagram", "linkedin", "twitter")}
                if "social" in selected else None
            )
            if social_out is not None and selected_mode == ExtractionMode.CUSTOM and "social" not in whole_modules:
                allowed = set(field_selection.get("social", set()))
                if "social_links" in allowed:
                    allowed.update(("facebook", "instagram", "linkedin", "twitter"))
                for key in ("facebook", "instagram", "linkedin", "twitter"):
                    if key not in allowed:
                        social_out[key] = None
            # Technology-only keeps the fixed website_analysis envelope and leaves unrelated modules unrequested.
            if "technology" in selected and website_out is None and site is not None:
                website_out = _website_payload(site, technology)
            if "technology" in selected and website_out is None:
                website_out = _empty_website(record.website, "unreachable")
                website_out["technology_stack"] = []

            scoring_out = LeadScoringOutput()
            if "lead_scoring" in selected:
                scoring_input = {
                    "lead_id": record.lead_id,
                    "business": business_out.model_dump(mode="python"),
                    "profile": profile_out,
                    "website_analysis": website_out,
                    "contacts": contacts_out,
                    "social_links": social_out,
                    "extraction_metadata": {
                        "source": record.discovery_source,
                        "field_sources": field_sources,
                        "module_status": statuses,
                    },
                }
                try:
                    scoring_out = LeadScoringOutput.model_validate(score_lead(scoring_input, self.scoring_config))
                    statuses["lead_scoring"] = ModuleStatus.SUCCESS
                except Exception as exc:
                    statuses["lead_scoring"] = ModuleStatus.FAILED
                    failed.append("lead_scoring")
                    warning = "Lead scoring failed: " + _error_text(exc)
                    warnings.append(warning)
                    logs.append(f"[ERROR] business={record.lead_id} module=lead_scoring {_error_text(exc)}")
                logs.append(f"[LEAD_SCORING] business={record.lead_id} status={statuses['lead_scoring'].value}")
                self._emit_detail(f"[LEAD_SCORING] business={record.lead_id} status={statuses['lead_scoring'].value}")

            enrichment_modules = selected - {"business_discovery"}
            enrichment_states = [statuses[name] for name in enrichment_modules]
            if not enrichment_modules:
                enrichment_status = "not_requested"
            elif all(status == ModuleStatus.SUCCESS for status in enrichment_states):
                enrichment_status = "success"
            else:
                # Missing fields, skipped dependencies, and module failures affect enrichment only.
                enrichment_status = "partial"
            discovery_state = "success" if discovery_status == ModuleStatus.SUCCESS else "discovery_failed"
            overall_status = (
                "discovery_failed" if discovery_state != "success"
                else "success" if enrichment_status in {"success", "not_requested"}
                else "success_with_partial_enrichment"
            )
            # Keep the Part 5 field values stable for existing consumers.
            overall = {
                "success": "success",
                "success_with_partial_enrichment": "partial",
                "discovery_failed": "failed",
            }[overall_status]
            completed = [name for name, status in statuses.items()
                         if status in {ModuleStatus.SUCCESS, ModuleStatus.PARTIAL, ModuleStatus.FAILED, ModuleStatus.NOT_AVAILABLE}]
            metadata = ExtractionMetadata(
                requested_modules=requested, completed_modules=completed, module_status=statuses,
                requested_fields=requested_field_paths,
                source=record.discovery_source,
                pages_visited=site.pages_visited if site and needs_website else [],
                field_sources={key: value for key, value in field_sources.items() if value},
                warnings=warnings, discovery_status=discovery_state,
                enrichment_status=enrichment_status, overall_status=overall_status,
                extraction_status=overall,
            )
            lead = CanonicalLeadOutput(
                business=business_out, profile=profile_out, website_analysis=website_out,
                contacts=contacts_out, social_links=social_out, lead_scoring=scoring_out,
                extraction_metadata=metadata,
            )
            for warning in warnings:
                safe_warning = re.sub(r"https?://\S+", "[url]", warning, flags=re.I)
                logs.append(f"[WARNING] business={record.lead_id} {safe_warning}")
            leads.append(lead)
            events.append({
                "lead_id": record.lead_id,
                "status": overall_status,
                "enrichment_status": enrichment_status,
                "enrichment_requested": bool(enrichment_modules),
                "website_attempted": needs_website,
                "website_available": bool(site and site.website_status == WebsiteStatus.ACTIVE and site.website_content),
                "enrichment_error": bool(failed or (site and site.crawl_errors)),
                "failed_modules": failed,
                "warnings": warnings,
                "discovery_source": record.discovery_source,
                "place_id": record.place_id,
                "primary_type": record.primary_type,
                "source_url": record.source_url,
                "source_attributions": record.source_attributions,
                "fallback_used": record.fallback_used,
                "fallback_reason": record.fallback_reason,
                "field_sources": {key: value for key, value in field_sources.items() if value},
                "pages_visited": site.pages_visited if site and needs_website else [],
                "crawl_errors": [item.model_dump(mode="json") for item in site.crawl_errors] if site else [],
                "website_content": [page.model_dump(mode="json") for page in site.website_content] if site else [],
            })
            logs.append(f"[BUSINESS] id={record.lead_id} status={overall}")

        candidate_leads = leads
        qualified_label = qualification_aliases[qualification_key]
        if qualified_label is not None:
            matching = [lead for lead in candidate_leads
                        if lead.lead_scoring.qualification_status == qualified_label]
            matching.sort(
                key=lambda lead: lead.lead_scoring.lead_score
                if lead.lead_scoring.lead_score is not None else -1,
                reverse=True,
            )
            leads = matching[:limit]
            if len(leads) < limit:
                run_warnings.append(
                    f"Only {len(leads)} {qualification_key} lead(s) were found in "
                    f"the {len(candidate_leads)} candidates examined (requested up to {limit})."
                )
        else:
            matching = candidate_leads

        counts = defaultdict(int)
        for lead in candidate_leads:
            counts[lead.extraction_metadata.extraction_status] += 1
        enrichment_requested_count = sum(bool(event["enrichment_requested"]) for event in events)
        enrichment_summary = {
            "businesses_fully_enriched": sum(event["enrichment_status"] == "success" for event in events),
            "businesses_partially_enriched": sum(event["enrichment_status"] == "partial" for event in events),
            "businesses_without_available_website": sum(
                event["website_attempted"] and not event["website_available"] for event in events
            ),
            "businesses_with_enrichment_errors": sum(event["enrichment_error"] for event in events),
            "businesses_without_enrichment_requested": sum(not event["enrichment_requested"] for event in events),
            "businesses_with_enrichment_requested": enrichment_requested_count,
        }
        discovery_failures = 1 if discovery_status == ModuleStatus.FAILED else 0
        summary = {
            "run_id": run_id, "extraction_mode": selected_mode.value, "search_query": query.strip(),
            "location": location.strip(), "requested_limit": limit,
            "total_businesses_requested": limit, "businesses_discovered": len(candidate_leads),
            "discovery_failures": discovery_failures, **enrichment_summary,
            "total_businesses": len(candidate_leads),
            "successful": counts["success"], "partial": counts["partial"], "failed": counts["failed"],
            "qualification_summary": {
                "filter": qualification_key, "candidate_limit": discovery_limit,
                "candidates_discovered": len(candidate_leads),
                "qualified_candidates": sum(item.lead_scoring.qualification_status == "Qualified" for item in candidate_leads),
                "needs_review_candidates": sum(item.lead_scoring.qualification_status == "Needs Review" for item in candidate_leads),
                "not_qualified_candidates": sum(item.lead_scoring.qualification_status == "Not Qualified" for item in candidate_leads),
                "matching_candidates": len(matching), "leads_returned": len(leads),
                "returned_order": "lead_score descending" if qualified_label is not None else "discovery order",
            },
            "count_definitions": {
                "total_businesses_requested": "The requested result limit; it is a maximum, not an expected result count.",
                "businesses_discovered": "Records returned successfully by business discovery.",
                "discovery_failures": "Failed discovery operations (currently at most one per run).",
                "businesses_fully_enriched": "Discovered records whose requested enrichment modules all completed successfully.",
                "businesses_partially_enriched": "Discovered records with requested enrichment that was missing, skipped, partial, or failed.",
                "businesses_without_available_website": "Enrichment attempts with no verified active website content.",
                "businesses_with_enrichment_errors": "Records with a failed module or website crawl error.",
                "total_businesses": "All candidates examined (kept as a compatibility field).",
                "total_leads": "Lead records actually written to leads.json after any qualification filter.",
                "successful/partial/failed": "Extraction outcomes across all candidates examined, before qualification filtering.",
                "qualification_summary": "Counts for score bands across examined candidates and the number returned after filtering.",
            },
            "execution_summary": {"requested_modules": requested, "business_discovery_status": discovery_status.value,
                                  "discovery": discovery_diagnostics},
        }
        report = _make_report(candidate_leads, events, requested, run_warnings)
        report["discovery"] = discovery_diagnostics
        bad_coverage = [path for path in MUST_COVERAGE_FIELDS
                        if report["field_coverage"].get(path, {}).get("available", 0) == 0]
        coverage_warnings = [f"0% coverage: {path}" for path in bad_coverage]
        for warning in coverage_warnings:
            logger.warning("0%% coverage: %s", warning.removeprefix("0% coverage: "))
        serialized_leads, schema_validation = _validate_output_payloads(leads)
        report["schema_validation"] = schema_validation
        all_warnings = list(run_warnings)
        all_warnings.extend(
            f"{item['lead_id']}: {warning}"
            for item in report["business_warnings"] for warning in item["warnings"]
        )
        all_warnings.extend(coverage_warnings)
        all_warnings = list(dict.fromkeys(all_warnings))
        summary.update({
            "status": "degraded" if bad_coverage else "failed" if discovery_status == ModuleStatus.FAILED else "success",
            "total_leads": len(leads),
            "field_coverage": report["field_coverage"],
            "module_statuses": report["module_statuses"],
            "warnings": all_warnings,
            "extraction_failures": report["extraction_failures"],
            "schema_validation": schema_validation,
        })
        ended = datetime.now(timezone.utc)
        logs.extend([f"[QUALIFICATION] filter={qualification_key} candidates={len(candidate_leads)} matched={len(matching)} returned={len(leads)}",
                     f"[SUMMARY] total={len(leads)} candidates={len(candidate_leads)} successful={counts['success']} partial={counts['partial']} failed={counts['failed']}",
                     f"[END] {ended.isoformat()}"])
        _write_artifacts(output_dir, summary, serialized_leads=serialized_leads)
        for name in ("leads.json", "summary.json"):
            self._emit(f"[SAVED] {output_dir / name}")
        self._emit(f"[DISCOVERY] Requested: {limit}; candidates examined: {len(candidate_leads)}; discovery failures: {discovery_failures}")
        if qualified_label is not None:
            self._emit(f"[QUALIFICATION] Matched: {len(matching)}; returned: {len(leads)} (limit {limit}), ranked by lead score")
        self._emit(
            "[ENRICHMENT] Fully enriched: "
            f"{enrichment_summary['businesses_fully_enriched']}; partially enriched: "
            f"{enrichment_summary['businesses_partially_enriched']}; without available website: "
            f"{enrichment_summary['businesses_without_available_website']}; with errors: "
            f"{enrichment_summary['businesses_with_enrichment_errors']}"
        )
        self._emit(
            f"[SUMMARY] Overall success: {counts['success']}; partial enrichment: "
            f"{counts['partial']}; discovery failed: {discovery_failures}"
        )
        return ScrapeRun(run_id, output_dir, leads, summary, report)


def _empty_website(website_url: str | None, status: str = "not_checked") -> dict[str, Any]:
    return {"status": status, "website_url": website_url, "final_url": None, "website_content": [],
            "about": [], "services": None, "contact_page_url": None, "technology_stack": None,
            "pages_visited": [], "crawl_errors": []}


def _requested_field(lead: CanonicalLeadOutput, path: str) -> bool:
    return path in lead.extraction_metadata.requested_fields


def _value_at(lead: CanonicalLeadOutput, path: str) -> Any:
    value: Any = lead.model_dump(mode="json")
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _make_report(leads: list[CanonicalLeadOutput], events: list[dict[str, Any]], requested: list[str], run_warnings: list[str]) -> dict[str, Any]:
    paths = [
        *(f"business.{key}" for key in ("business_name", "category", "sub_category", "description", "address", "phone", "email", "website", "latitude", "longitude", "rating", "review_count")),
        *(f"profile.{key}" for key in PROFILE_FIELDS),
        "website_analysis.website_content", "website_analysis.contact_page_url", "website_analysis.technology_stack",
        "contacts.contact_person", "contacts.emails", "contacts.phone_numbers",
        *(f"social_links.{key}" for key in ("facebook", "instagram", "linkedin", "twitter")),
    ]
    coverage: dict[str, Any] = {}
    for path in paths:
        available = missing = not_requested = 0
        missing_ids = []
        for lead in leads:
            if not _requested_field(lead, path):
                not_requested += 1
            elif _value_at(lead, path) is None or _value_at(lead, path) == [] or _value_at(lead, path) == "":
                missing += 1
                missing_ids.append(lead.business.lead_id)
            else:
                available += 1
        coverage[path] = {"requested_businesses": available + missing, "available": available,
                          "missing": missing, "not_requested": not_requested, "missing_lead_ids": missing_ids}
    module_counts: dict[str, dict[str, int]] = {}
    for module in MODULES:
        statuses: dict[str, int] = defaultdict(int)
        for lead in leads:
            statuses[lead.extraction_metadata.module_status[module].value] += 1
        module_counts[module] = dict(statuses)
    return {
        "total_businesses": len(leads),
        "successful": sum(lead.extraction_metadata.overall_status == "success" for lead in leads),
        "partial": sum(lead.extraction_metadata.overall_status == "success_with_partial_enrichment" for lead in leads),
        "failed": sum(lead.extraction_metadata.overall_status == "discovery_failed" for lead in leads),
        "businesses_discovered": len(leads),
        "discovery_failures": sum(event["status"] == "discovery_failed" for event in events)
            or (1 if run_warnings and not leads else 0),
        "businesses_fully_enriched": sum(event.get("enrichment_status") == "success" for event in events),
        "businesses_partially_enriched": sum(event.get("enrichment_status") == "partial" for event in events),
        "businesses_without_available_website": sum(
            bool(event.get("website_attempted")) and not bool(event.get("website_available")) for event in events
        ),
        "businesses_with_enrichment_errors": sum(bool(event.get("enrichment_error")) for event in events),
        "requested_modules": requested, "field_coverage": coverage, "module_statuses": module_counts,
        "business_warnings": [{"lead_id": lead.business.lead_id, "warnings": lead.extraction_metadata.warnings}
                              for lead in leads if lead.extraction_metadata.warnings],
        "extraction_failures": [{"lead_id": event["lead_id"], "failed_modules": event["failed_modules"], "warnings": event["warnings"]}
                                for event in events if event["failed_modules"]],
        "lead_diagnostics": [{
            "lead_id": event["lead_id"], "discovery_source": event.get("discovery_source"),
            "place_id": event.get("place_id"), "fallback_used": event.get("fallback_used", False),
            "source_url": event.get("source_url"), "source_attributions": event.get("source_attributions", []),
            "fallback_reason": event.get("fallback_reason"), "field_sources": event.get("field_sources", {}),
            "pages_visited": event.get("pages_visited", []), "crawl_errors": event.get("crawl_errors", []),
            "website_content": event.get("website_content", []),
            "module_statuses": leads[index].extraction_metadata.model_dump(mode="json").get("module_status", {}),
        } for index, event in enumerate(events)],
        "run_warnings": run_warnings,
    }


def _write_artifacts(output_dir: Path, summary: dict[str, Any], *, serialized_leads: list[dict[str, Any]]) -> None:
    (output_dir / "leads.json").write_text(json.dumps(serialized_leads, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
