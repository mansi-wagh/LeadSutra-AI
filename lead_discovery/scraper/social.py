from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel

from .models import BusinessRecord
from .website import WebsiteCrawlResult

PLATFORMS = {
    "facebook": {"facebook.com"},
    "instagram": {"instagram.com"},
    "linkedin": {"linkedin.com"},
    "twitter": {"twitter.com", "x.com"},
}
IGNORE_SLUGS = {"home", "share", "intent", "login", "signup", "explore", "hashtag", "sharer", "plugins"}
STOP_WORDS = {"the", "and", "of", "for", "inc", "llc", "ltd", "limited", "company", "co", "studio", "official"}
NON_PROFILE_PATHS = {"posts", "post", "reel", "reels", "stories", "story", "photos", "photo", "watch",
                     "events", "groups", "dialog", "accounts", "p", "status", "search", "feed"}


class SocialAccount(BaseModel):
    url: str
    source_url: str


class SocialMediaExtraction(BaseModel):
    lead_id: str
    facebook: SocialAccount | None = None
    instagram: SocialAccount | None = None
    linkedin: SocialAccount | None = None
    twitter: SocialAccount | None = None
    field_sources: dict[str, list[str]]


def _normalize_social_url(url: str, allowed_hosts: set[str]) -> str | None:
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        host = parsed.hostname.lower().removeprefix("www.")
        if host not in allowed_hosts:
            return None
        path = re.sub(r"/+", "/", parsed.path).rstrip("/")
        if not path or path.casefold() in {"/home", "/share", "/intent", "/login", "/explore"}:
            return None
        parts = [part.casefold() for part in path.strip("/").split("/") if part]
        first = parts[0]
        if first in IGNORE_SLUGS or first in NON_PROFILE_PATHS or first.endswith(".php"):
            return None
        if host in {"instagram.com", "twitter.com", "x.com"} and len(parts) != 1:
            return None
        if host == "linkedin.com" and (len(parts) != 2 or first not in {"company", "in", "school"}):
            return None
        if host == "facebook.com" and (len(parts) > 3 or any(part in NON_PROFILE_PATHS for part in parts)):
            return None
        return urlunparse(("https", host, path, "", "", ""))
    except (ValueError, UnicodeError):
        return None


def _business_tokens(name: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", name.casefold()) if len(token) > 2 and token not in STOP_WORDS}


def extract_social_media(business: BusinessRecord, website: WebsiteCrawlResult) -> SocialMediaExtraction:
    """Find social accounts explicitly linked from the stored official-site pages."""
    if business.lead_id != website.lead_id:
        raise ValueError("business and website result lead_id values must match")
    found: dict[str, SocialAccount | None] = {platform: None for platform in PLATFORMS}
    business_tokens = _business_tokens(business.business_name)
    for page in website.website_content:
        for link in page.outgoing_links:
            parsed = urlparse(link.url)
            host = (parsed.hostname or "").lower().removeprefix("www.")
            platform = next((name for name, hosts in PLATFORMS.items() if host in hosts), None)
            if not platform or found[platform] is not None:
                continue
            normalized = _normalize_social_url(link.url, PLATFORMS[platform])
            if not normalized:
                continue
            path_tokens = set(re.findall(r"[a-z0-9]+", urlparse(normalized).path.casefold()))
            label_tokens = _business_tokens(link.text or "")
            if business_tokens and not (business_tokens & path_tokens or business_tokens & label_tokens):
                continue
            found[platform] = SocialAccount(url=normalized, source_url=page.page_url)
    result = SocialMediaExtraction(lead_id=business.lead_id, **found, field_sources={})
    result.field_sources = {
        platform: [account.source_url] if account else []
        for platform, account in found.items()
    }
    return result
