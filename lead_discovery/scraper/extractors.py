from __future__ import annotations

from dataclasses import dataclass

from .contact import ContactExtraction, extract_contacts
from .models import BusinessRecord
from .social import SocialMediaExtraction, extract_social_media
from .technology import TechnologyExtraction, detect_technologies
from .website import WebsiteCrawlResult


@dataclass
class RequestedExtractions:
    contacts: ContactExtraction | None = None
    social: SocialMediaExtraction | None = None
    technology: TechnologyExtraction | None = None


def extract_requested(
    business: BusinessRecord,
    website: WebsiteCrawlResult,
    *,
    contacts: bool = False,
    social: bool = False,
    technology: bool = False,
) -> RequestedExtractions:
    """Run only the requested independent extractors; never performs website requests."""
    if business.lead_id != website.lead_id:
        raise ValueError("business and website result lead_id values must match")
    result = RequestedExtractions()
    if contacts:
        result.contacts = extract_contacts(business, website)
    if social:
        result.social = extract_social_media(business, website)
    if technology:
        result.technology = detect_technologies(website)
    return result


def extract_all(business: BusinessRecord, website: WebsiteCrawlResult) -> RequestedExtractions:
    """Explicit convenience entry point for callers that want all three results."""
    return extract_requested(business, website, contacts=True, social=True, technology=True)
