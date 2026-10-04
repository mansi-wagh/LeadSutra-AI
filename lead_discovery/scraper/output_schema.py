"""Strict JSON contract for exported LeadSutra lead records."""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class StrictOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BusinessSection(StrictOutputModel):
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


class HoursEntry(StrictOutputModel):
    opens: str | None = None
    closes: str | None = None
    closed: bool = False


class ProfileSection(StrictOutputModel):
    services: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    target_customers: list[str] = Field(default_factory=list)
    about_info: str | None = None
    business_description: str | None = None
    operating_hours: dict[str, HoursEntry] = Field(default_factory=dict)


class WebsiteSection(StrictOutputModel):
    status: str = "not_checked"
    website_url: str | None = None
    about: str | None = None
    services: list[str] = Field(default_factory=list)
    contact_page_url: str | None = None
    technology_stack: list[str] = Field(default_factory=list)


class ContactsSection(StrictOutputModel):
    contact_person: str | None = None
    emails: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)
    contact_page_url: str | None = None


class SocialLinksSection(StrictOutputModel):
    facebook: HttpUrl | None = None
    instagram: HttpUrl | None = None
    linkedin: HttpUrl | None = None
    twitter: HttpUrl | None = None


class ScoringSection(StrictOutputModel):
    lead_score: float | None = None
    priority: str = "Unknown"
    qualification_status: str = "Unknown"
    score_breakdown: dict[str, Any] = Field(default_factory=dict)


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
