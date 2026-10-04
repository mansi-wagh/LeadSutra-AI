from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from .website import WebsiteCrawlResult

RULES: dict[str, tuple[str, ...]] = {
    "WordPress": ("meta-generator:wordpress", "/wp-content/", "/wp-includes/"),
    "Shopify": ("cdn.shopify.com", "shopify-section", "header:x-shopify-stage:"),
    "Wix": ("wixstatic.com", "meta-generator:wix", "wix.com/"),
    "React": ("data-reactroot", "react-dom", "react.production.min.js"),
    "Next.js": ("/_next/", "__next_data__", "header:x-powered-by:next.js"),
    "Google Analytics": ("googletagmanager.com", "google-analytics.com", "gtag("),
    "Webflow": ("webflow.js", "meta-generator:webflow"),
    "Squarespace": ("static1.squarespace.com", "meta-generator:squarespace"),
    "Drupal": ("meta-generator:drupal", "/sites/default/files/"),
    "Joomla": ("meta-generator:joomla", "/media/system/js/"),
    "Bootstrap": ("bootstrap.min.css", "bootstrap.min.js"),
}


class DetectedTechnology(BaseModel):
    name: str
    evidence: list[str] = Field(min_length=1)
    source_urls: list[str] = Field(min_length=1)


class TechnologyExtraction(BaseModel):
    lead_id: str
    technologies: list[DetectedTechnology] = Field(default_factory=list)
    field_sources: dict[str, list[str]] = Field(default_factory=dict)


def detect_technologies(website: WebsiteCrawlResult) -> TechnologyExtraction:
    """Detect technologies from evidence captured during Part 2 crawling."""
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"evidence": [], "source_urls": []})
    for page in website.website_content:
        signals = list(page.technology_signals)
        signals.extend(f"header:{key.lower()}:{value}" for key, value in page.response_headers.items())
        normalized_signals = [signal.casefold() for signal in signals]
        for technology, signatures in RULES.items():
            matches = [
                original for original, normalized in zip(signals, normalized_signals)
                if any(signature in normalized for signature in signatures)
            ]
            if matches:
                bucket = grouped[technology]
                bucket["evidence"].extend(item for item in matches if item not in bucket["evidence"])
                if page.page_url not in bucket["source_urls"]:
                    bucket["source_urls"].append(page.page_url)
    items = [
        DetectedTechnology(name=name, evidence=data["evidence"], source_urls=data["source_urls"])
        for name, data in sorted(grouped.items())
    ]
    sources = list(dict.fromkeys(url for item in items for url in item.source_urls))
    return TechnologyExtraction(lead_id=website.lead_id, technologies=items, field_sources={"technologies": sources})
