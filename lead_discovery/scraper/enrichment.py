"""Website crawling and source-traceable business/contact/social extraction."""
from __future__ import annotations

import httpx
import asyncio
import logging
import re
from bs4 import BeautifulSoup, Tag
from collections import OrderedDict, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict, Field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse
from .discovery import BrowserConfig, BrowserManager, BusinessRecord
from .safe_http import PublicTransport, WebsiteFetchError, bounded_get

logger = logging.getLogger(__name__)

SOCIAL_HOSTS = {
    "facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com",
    "tiktok.com", "youtube.com", "youtu.be", "pinterest.com", "reddit.com",
}
DIRECTORY_HOSTS = {
    "yelp.com", "yellowpages.com", "tripadvisor.com", "foursquare.com",
    "google.com", "maps.google.com", "justdial.com", "indiamart.com",
}
SKIP_PATH = re.compile(r"/(?:login|signin|sign-in|cart|checkout|basket|account|wp-admin)(?:/|$)", re.I)
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".mp4", ".mp3")
PAGE_HINTS = {
    "about": ("about", "company", "story"),
    "services": ("service", "what-we-do", "solutions"),
    "products": ("product", "shop", "catalog"),
    "contact": ("contact", "location", "reach-us", "reach-out", "get-in-touch", "getintouch", "contactus"),
    "team": ("team", "people", "staff"),
    "faq": ("faq", "frequently-asked", "help"),
}

class WebsiteStatus(str, Enum):
    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    BLOCKED = "blocked"
    INVALID_URL = "invalid_url"
    NOT_FOUND = "not_found"
    UNCERTAIN = "uncertain"

class CrawlError(BaseModel):
    url: str | None = None
    message: str

class WebsiteLink(BaseModel):
    url: str
    text: str | None = None

class WebsitePage(BaseModel):
    page_url: str
    page_title: str | None = None
    meta_description: str | None = None
    main_text: str = ""
    contact_text: str = ""
    page_type: str = "other"
    extraction_status: str = "success"
    # Optional evidence captured during the existing crawl for later extractors.
    outgoing_links: list[WebsiteLink] = Field(default_factory=list)
    technology_signals: list[str] = Field(default_factory=list)
    response_headers: dict[str, str] = Field(default_factory=dict)

class WebsiteCrawlResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    lead_id: str
    website_status: WebsiteStatus
    website_url: str | None = None
    final_url: str | None = None
    website_content: list[WebsitePage] = Field(default_factory=list)
    about: list[dict[str, str]] = Field(default_factory=list)
    services: list[dict[str, str]] = Field(default_factory=list)
    contact_page_url: str | None = None
    pages_visited: list[str] = Field(default_factory=list)
    crawl_errors: list[CrawlError] = Field(default_factory=list)
    field_sources: dict[str, list[str]] = Field(default_factory=dict)

@dataclass(frozen=True)
class WebsiteCrawlerConfig:
    max_pages: int = 8
    timeout_seconds: float = 15.0
    min_text_for_static: int = 180
    user_agent: str = "LeadSutraResearchBot/1.0 (+website content discovery)"
    concurrency: int = 3
    crawl_timeout_seconds: float = 40.0
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if self.max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.concurrency < 1 or self.crawl_timeout_seconds <= 0 or self.max_response_bytes < 1:
            raise ValueError("concurrency, crawl timeout, and response limit must be positive")

def normalize_url(value: str | None, *, preserve_path: bool = False) -> str | None:
    """Normalize only structurally valid HTTP(S) website URLs."""
    if not value or not value.strip():
        return None
    candidate = value.strip()
    if not re.match(r"^https?://", candidate, re.I):
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if any(ch.isspace() for ch in parsed.netloc) or not parsed.hostname or "." not in parsed.hostname:
            return None
        # Accessing .port also validates malformed port syntax.
        _ = parsed.port
        host = parsed.hostname.lower().encode("idna").decode("ascii")
        netloc = host + (f":{parsed.port}" if parsed.port else "")
        path = parsed.path or "/"
        if path != "/" and not preserve_path:
            path = path.rstrip("/")
        tracking = {"gclid", "fbclid", "dclid", "msclkid"}
        query_items = [
            (key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in tracking
        ]
        query = urlencode(sorted(query_items))
        return urlunparse((parsed.scheme.lower(), netloc, path, "", query, ""))
    except (ValueError, UnicodeError):
        return None

def _host_is_excluded(host: str) -> bool:
    host = host.lower().removeprefix("www.")
    return any(host == excluded or host.endswith("." + excluded) for excluded in SOCIAL_HOSTS | DIRECTORY_HOSTS)

def _page_type(url: str, title: str | None, text: str) -> str:
    haystack = f"{url} {title or ''}".lower()
    for kind, terms in PAGE_HINTS.items():
        if any(term in haystack for term in terms):
            return kind
    return "home" if urlparse(url).path in {"", "/"} else "other"

def _clean_page(html: str, page_url: str) -> tuple[WebsitePage, list[tuple[str, str]]]:
    html = html if isinstance(html, str) else ""
    soup = BeautifulSoup(html, "html.parser")
    outgoing: list[WebsiteLink] = []
    for anchor in soup.find_all("a", href=True):
        suspicious_context = False
        for ancestor in (anchor, *anchor.parents):
            if not isinstance(ancestor, Tag):
                continue
            attrs = ancestor.attrs if isinstance(ancestor.attrs, dict) else {}
            marker = " ".join([
                str(attrs.get("id") or ""),
                " ".join(str(item) for item in (attrs.get("class") or [])),
            ]).casefold()
            if any(term in marker for term in (
                "cookie", "consent", "tracking", "breadcrumb", "social-share", "share-button",
                "share-buttons", "comment", "advert", "ad-container", "embed", "widget",
            )):
                suspicious_context = True
                break
        if suspicious_context:
            continue
        raw_href = anchor.get("href")
        if not isinstance(raw_href, str) or not raw_href.strip():
            continue
        raw_href = raw_href.strip()
        target = urljoin(page_url, raw_href)
        scheme = urlparse(target).scheme.lower()
        if scheme in {"http", "https", "mailto", "tel"}:
            outgoing.append(WebsiteLink(url=target, text=" ".join(anchor.stripped_strings) or None))
    signals: list[str] = []
    generator = soup.find("meta", attrs={"name": re.compile("^generator$", re.I)})
    if generator and generator.get("content"):
        signals.append("meta-generator:" + generator["content"].strip())
    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    if viewport and isinstance(viewport.get("content"), str):
        signals.append("meta-viewport:" + viewport["content"].strip())
    for script in soup.find_all("script", src=True):
        signals.append("script-src:" + script["src"].strip())
    for stylesheet in soup.find_all("link", rel=re.compile("stylesheet", re.I), href=True):
        signals.append("stylesheet:" + stylesheet["href"].strip())
    for node in [soup.html, soup.body]:
        if node:
            if node.get("id"):
                signals.append("html-id:" + str(node["id"]))
            classes = node.get("class") or []
            if isinstance(classes, str):
                classes = [classes]
            for class_name in classes:
                signals.append("html-class:" + str(class_name))
    if soup.find(id="__NEXT_DATA__") or soup.find(id="__next_data__"):
        signals.append("dom-signature:__next_data__")
    if soup.find(attrs={"data-reactroot": True}):
        signals.append("dom-signature:data-reactroot")
    if any("shopify-section" in " ".join(str(value) for value in (node.get("class") or [])).casefold()
           for node in soup.find_all(True)):
        signals.append("dom-signature:shopify-section")
    contact_chunks: list[str] = []
    seen_contact_chunks: set[str] = set()
    for node in soup.find_all(["footer", "address"]):
        if node.find_parent(["script", "style", "noscript"]):
            continue
        if node.has_attr("hidden") or str(node.get("aria-hidden", "")).lower() == "true":
            continue
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", str(node.get("style", "")), re.I):
            continue
        text = " ".join(node.stripped_strings)
        key = re.sub(r"\s+", " ", text).casefold().strip()
        if key and key not in seen_contact_chunks:
            seen_contact_chunks.add(key)
            contact_chunks.append(text)
    title_tag = soup.find("title")
    title = title_tag.get_text(" ", strip=True) if title_tag else None
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    description_value = meta.get("content") if meta else None
    description = description_value.strip() if isinstance(description_value, str) else None

    for node in soup.select("script, style, noscript, svg, iframe, canvas, form, nav, footer, header, aside"):
        node.decompose()
    for node in list(soup.find_all(True)):
        # Removing a noisy ancestor also decomposes its descendants. BeautifulSoup
        # leaves those saved Tag objects in this snapshot with attrs=None, and
        # Tag.get() then raises AttributeError instead of returning a default.
        if not isinstance(node.attrs, dict):
            continue
        node_id = node.get("id") or ""
        node_classes = node.get("class") or []
        if isinstance(node_classes, str):
            node_classes = [node_classes]
        marker = " ".join([str(node_id), " ".join(str(value) for value in node_classes)]).lower()
        if any(term in marker for term in ("cookie", "consent", "tracking", "breadcrumb", "social-share")):
            node.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    chunks: list[str] = []
    seen: set[str] = set()
    for node in root.find_all(["h1", "h2", "h3", "p", "li", "blockquote"]):
        content = " ".join(node.stripped_strings)
        normalized = re.sub(r"\s+", " ", content).strip()
        if normalized and normalized.casefold() not in seen:
            seen.add(normalized.casefold())
            chunks.append(normalized)
    text = "\n".join(chunks)
    if not text:
        text = re.sub(r"\s+", " ", root.get_text(" ", strip=True)).strip()

    links: list[tuple[str, str]] = []
    for link in outgoing:
        if urlparse(link.url).scheme not in {"http", "https"}:
            continue
        target = normalize_url(link.url)
        if target:
            links.append((target, link.text or ""))
    return WebsitePage(
        page_url=page_url, page_title=title or None, meta_description=description or None,
        main_text=text, contact_text="\n".join(contact_chunks), page_type=_page_type(page_url, title, text),
        extraction_status="success" if text else "empty",
        outgoing_links=outgoing, technology_signals=list(dict.fromkeys(signals)),
    ), links

def _link_priority(url: str, label: str) -> int:
    haystack = (url + " " + label).lower()
    for priority, kind in enumerate(("about", "services", "products", "contact", "team", "faq")):
        if any(term in haystack for term in PAGE_HINTS[kind]):
            return priority
    return 99

class WebsiteCrawler:
    """Discover and boundedly crawl one business's candidate official website."""

    def __init__(
        self,
        config: WebsiteCrawlerConfig | None = None,
        browser_config: BrowserConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        browser_manager_factory: Any = BrowserManager,
    ) -> None:
        self.config = config or WebsiteCrawlerConfig()
        self.browser_config = browser_config or BrowserConfig()
        self.client = client
        self.browser_manager_factory = browser_manager_factory

    async def crawl(self, business: BusinessRecord) -> WebsiteCrawlResult:
        raw = business.website
        base = normalize_url(raw)
        if not raw:
            return WebsiteCrawlResult(lead_id=business.lead_id, website_status=WebsiteStatus.NOT_FOUND)
        if not base:
            return WebsiteCrawlResult(lead_id=business.lead_id, website_status=WebsiteStatus.INVALID_URL)
        host = urlparse(base).hostname or ""
        if _host_is_excluded(host):
            return WebsiteCrawlResult(lead_id=business.lead_id, website_status=WebsiteStatus.UNCERTAIN, website_url=base,
                                      crawl_errors=[CrawlError(url=base, message="Candidate is a social or directory page, not an official website")])

        own_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds), follow_redirects=False,
            transport=PublicTransport(), trust_env=False,
            headers={"User-Agent": self.config.user_agent},
        )
        result = WebsiteCrawlResult(lead_id=business.lead_id, website_status=WebsiteStatus.UNREACHABLE, website_url=base)
        visited: set[str] = set()
        queue: deque[str] = deque([base])
        queued: set[str] = {base}
        try:
            async with asyncio.timeout(self.config.crawl_timeout_seconds):
                return await self._crawl_pages(client, host, result, visited, queue, queued)
        except TimeoutError:
            result.crawl_errors.append(CrawlError(message="Website crawl deadline exceeded; keeping partial data"))
            if result.website_content:
                result.website_status = WebsiteStatus.ACTIVE
            return result
        finally:
            result.field_sources = {
                "website_content": [page.page_url for page in result.website_content],
                "about": [entry["source_url"] for entry in result.about],
                "services": [entry["source_url"] for entry in result.services],
                "contact_page_url": [result.contact_page_url] if result.contact_page_url else [],
            }
            if own_client:
                await client.aclose()

    async def _crawl_pages(self, client, host, result, visited, queue, queued):
        browser_manager = None
        try:
            while queue and len(visited) < self.config.max_pages:
                url = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                result.pages_visited.append(url)
                try:
                    response = await self._fetch(client, url, host)
                    if response is None:
                        result.crawl_errors.append(CrawlError(url=url, message="HTTP client returned no response"))
                        continue
                    response.raise_for_status()
                    final_url = normalize_url(str(response.url), preserve_path=True)
                    if not final_url:
                        result.crawl_errors.append(CrawlError(url=url, message="Redirected to an invalid URL"))
                        continue
                    final_host = urlparse(final_url).hostname or ""
                    if _host_is_excluded(final_host) or not _same_site(host, final_host):
                        result.website_status = WebsiteStatus.UNCERTAIN
                        result.crawl_errors.append(CrawlError(url=url, message="Website redirected outside the candidate site"))
                        if not result.website_content:
                            result.final_url = final_url
                        break
                    if result.final_url is None:
                        result.final_url = final_url
                    if response.status_code == 404:
                        result.crawl_errors.append(CrawlError(url=final_url, message="HTTP 404"))
                        continue
                    response_headers = getattr(response, "headers", None) or {}
                    content_type = str(response_headers.get("content-type", "")).lower()
                    if "html" not in content_type and content_type:
                        result.crawl_errors.append(CrawlError(url=final_url, message=f"Unsupported content type: {content_type}"))
                        continue
                    page, links = _clean_page(response.text, final_url)
                    page.response_headers = {
                        key.lower(): value for key, value in response_headers.items()
                        if key.lower() in {"server", "x-powered-by", "x-generator", "x-shopify-stage"}
                    }
                    page.technology_signals.extend(
                        f"header:{key}:{value}" for key, value in page.response_headers.items()
                    )
                    if len(page.main_text) < self.config.min_text_for_static and "<script" in response.text.lower():
                        try:
                            if browser_manager is None:
                                browser_manager = self.browser_manager_factory(self.browser_config)
                                await browser_manager.start()
                                if isinstance(browser_manager, BrowserManager):
                                    await self._guard_browser(browser_manager, client, host, result)
                            rendered = await self._render_page(final_url, browser_manager)
                        except Exception as exc:
                            rendered = None
                            result.crawl_errors.append(CrawlError(url=final_url, message=f"Browser fallback failed: {type(exc).__name__}: {exc}"))
                            if browser_manager is not None:
                                await browser_manager.close()
                            browser_manager = None
                        if rendered is not None:
                            rendered_page, rendered_links = _clean_page(rendered, final_url)
                            rendered_page.response_headers = page.response_headers
                            rendered_page.technology_signals = list(dict.fromkeys(
                                page.technology_signals + rendered_page.technology_signals
                                + [f"header:{key}:{value}" for key, value in page.response_headers.items()]
                            ))
                            page = rendered_page
                            links = links + rendered_links
                        else:
                            result.crawl_errors.append(CrawlError(url=final_url, message="Playwright rendering failed or timed out"))
                    result.website_content.append(page)
                    if page.page_type == "about" and page.main_text:
                        result.about.append({"text": page.main_text, "source_url": page.page_url})
                    if page.page_type == "services" and page.main_text:
                        result.services.append({"text": page.main_text, "source_url": page.page_url})
                    if page.page_type == "contact":
                        result.contact_page_url = page.page_url
                    priority_links = sorted(links, key=lambda item: _link_priority(*item))
                    for target, _label in priority_links:
                        parsed_target = urlparse(target)
                        if not _same_site(host, parsed_target.hostname or ""):
                            continue
                        if SKIP_PATH.search(parsed_target.path) or parsed_target.path.lower().endswith(SKIP_EXTENSIONS):
                            continue
                        if len(queued) < self.config.max_pages and target not in queued and target not in visited:
                            queued.add(target)
                            queue.append(target)
                except (httpx.HTTPError, httpx.InvalidURL, WebsiteFetchError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {401, 403}:
                        if not result.website_content:
                            result.website_status = WebsiteStatus.BLOCKED
                    message = (f"HTTP {exc.response.status_code}: website request failed"
                               if isinstance(exc, httpx.HTTPStatusError) else f"{type(exc).__name__}: {exc}")
                    result.crawl_errors.append(CrawlError(url=url, message=" ".join(message.split())))
                except Exception as exc:
                    logger.exception("Website crawl failed for %s", url)
                    result.crawl_errors.append(CrawlError(url=url, message=f"{type(exc).__name__}: {exc}"))
            if result.website_content and result.website_status != WebsiteStatus.UNCERTAIN:
                result.website_status = WebsiteStatus.ACTIVE
            elif result.website_status == WebsiteStatus.UNREACHABLE and result.crawl_errors and "404" in result.crawl_errors[0].message:
                result.website_status = WebsiteStatus.UNREACHABLE
            return result
        finally:
            if browser_manager is not None:
                await browser_manager.close()

    async def _fetch(self, client, url, host):
        seen = set()
        for _ in range(6):
            if url in seen:
                raise WebsiteFetchError("Website redirect loop detected")
            seen.add(url)
            if host is not None and not _same_site(host, urlparse(url).hostname or ""):
                raise WebsiteFetchError("Blocked redirect outside the candidate site")
            response = (await bounded_get(client, url, self.config.max_response_bytes)
                        if isinstance(client, httpx.AsyncClient) else await client.get(url))
            if response is None or response.status_code not in {301, 302, 303, 307, 308}:
                return response
            url = normalize_url(urljoin(url, response.headers.get("location", "")), preserve_path=True)
            if not url:
                raise WebsiteFetchError("Invalid website redirect")
        raise WebsiteFetchError("Too many website redirects")

    async def _guard_browser(self, manager, client, host, result):
        requests = 0

        async def route_request(route):
            nonlocal requests
            requests += 1
            request = route.request
            if (requests > 40 or request.method != "GET"
                    or request.resource_type in {"image", "media", "font"}
                    or (request.is_navigation_request() and not _same_site(host, urlparse(request.url).hostname or ""))):
                await route.abort()
                return
            try:
                response = await self._fetch(client, request.url, host if request.is_navigation_request() else None)
                await route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
            except Exception as exc:
                result.crawl_errors.append(CrawlError(url=request.url, message=f"Browser resource failed: {type(exc).__name__}: {exc}"))
                logger.warning("Browser resource blocked or failed: %s", type(exc).__name__)
                await route.abort()

        await manager.context.route("**/*", route_request)
        await manager.context.route_web_socket("**/*", lambda socket: socket.close())

    async def _render_page(self, url: str, manager: Any) -> str | None:
        try:
            await manager.page.goto(url, wait_until="domcontentloaded")
            return await manager.page.content()
        except PlaywrightError as exc:
            logger.debug("Browser rendering failed for %s: %s", url, exc)
            return None

def _same_site(original_host: str, candidate_host: str) -> bool:
    original = original_host.lower().removeprefix("www.")
    candidate = candidate_host.lower().removeprefix("www.")
    return original == candidate

async def crawl_business_website(
    business: BusinessRecord,
    *,
    config: WebsiteCrawlerConfig | None = None,
    browser_config: BrowserConfig | None = None,
) -> WebsiteCrawlResult:
    """Convenience function for a single Part 1 business record."""
    return await WebsiteCrawler(config=config, browser_config=browser_config).crawl(business)

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

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9.!#$%&'*+/=?^_{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+", re.I)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{5,}\d)(?:\s*(?:ext\.?|x)\s*\d{1,6})?(?!\w)", re.I)
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.com", "yourdomain.com", "email.com"}
SYSTEM_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"}
PLACEHOLDER_LOCAL_PARTS = {"name", "yourname", "user", "email", "youremail", "test", "example", "john.doe", "jane.doe"}
PERSON_RE = re.compile(
    r"^\s*(?:contact\s+person|primary\s+contact|contact|owner|founder|practice\s+manager|"
    r"for\s+(?:business\s+)?inquiries,?\s*contact)\s*:\s*"
    r"(?P<name>(?:Dr\.?\s+|Mr\.?\s+|Ms\.?\s+|Mrs\.?\s+)?[A-Z][A-Za-z'?-]+(?:\s+[A-Z][A-Za-z'?-]+){1,3})\s*$",
    re.I | re.M,
)

class ContactValue(BaseModel):
    value: str
    source_urls: list[str] = Field(min_length=1)

class ContactEmail(BaseModel):
    email: str
    source_urls: list[str] = Field(min_length=1)

class ContactPhone(BaseModel):
    value: str
    normalized: str
    source_urls: list[str] = Field(min_length=1)

class ContactExtraction(BaseModel):
    lead_id: str
    contact_person: ContactValue | None = None
    contact_emails: list[ContactEmail] = Field(default_factory=list)
    phone_numbers: list[ContactPhone] = Field(default_factory=list)
    contact_page_url: str | None = None
    email: str | None = None
    phone: str | None = None
    field_sources: dict[str, list[str]] = Field(default_factory=dict)

def _valid_email(raw: str) -> str | None:
    address = unquote(raw.strip().strip(".,;:<>[](){}").lower())
    if not EMAIL_RE.fullmatch(address):
        return None
    local, domain = address.rsplit("@", 1)
    if domain in PLACEHOLDER_DOMAINS or "." not in domain:
        return None
    if local.casefold() in SYSTEM_LOCAL_PARTS | PLACEHOLDER_LOCAL_PARTS:
        return None
    if any(token in domain for token in ("sentry.io", "wixpress.com", "wordpress.com", "shopifyemail.com")):
        return None
    return address

def _phone(raw: str) -> tuple[str, str] | None:
    original = re.sub(r"\s+", " ", raw).strip(" .,:;")
    digits = re.sub(r"\D", "", original)
    if not 7 <= len(digits) <= 15 or len(set(digits)) == 1:
        return None
    normalized = ("+" if original.startswith("+") else "") + digits
    return original, normalized

def normalize_phone(raw: str | None) -> str | None:
    """Return the same digits-only representation used in contacts.phone_numbers."""
    if not raw:
        return None
    found = _phone(raw)
    return found[1] if found else None

def _phone_key(normalized: str) -> str:
    digits = normalized.lstrip("+")
    # US country code 1 is often omitted on the same listed number.
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits

def extract_contacts(business: BusinessRecord, website: WebsiteCrawlResult) -> ContactExtraction:
    """Extract contact data only from the given business and previously collected pages."""
    if business.lead_id != website.lead_id:
        raise ValueError("business and website result lead_id values must match")
    emails: OrderedDict[str, list[str]] = OrderedDict()
    phones: OrderedDict[str, tuple[str, str, list[str]]] = OrderedDict()
    person: ContactValue | None = None
    for page in website.website_content:
        content = page.main_text
        contact_text = getattr(page, "contact_text", "") or ""
        path = urlparse(page.page_url).path.casefold()
        page_kind = page.page_type.casefold()
        person_source = page_kind in {"home", "about", "contact", "team"} or bool(
            re.search(r"/(?:about|our-story|contact(?:-us)?|contactus|get-in-touch|reach-out|team|people)(?:/|$)", path)
        )
        phone_source = page_kind in {"home", "about", "contact", "team", "services", "faq"} or bool(
            re.search(r"/(?:about|our-story|contact(?:-us)?|contactus|get-in-touch|reach-out|team|people)(?:/|$)", path)
        )
        if re.search(r"/(?:blog|news|articles?|posts?)(?:/|$)", path):
            content = ""
        contact_content = "\n".join(value for value in (content, contact_text) if value)
        for link in page.outgoing_links:
            parsed = urlparse(link.url)
            if parsed.scheme.lower() in {"mailto", "tel"}:
                contact_content += " " + unquote(parsed.path)
        for raw in EMAIL_RE.findall(contact_content):
            email = _valid_email(raw)
            if email:
                emails.setdefault(email, [])
                if page.page_url not in emails[email]:
                    emails[email].append(page.page_url)
        for link in page.outgoing_links:
            parsed = urlparse(link.url)
            if parsed.scheme.lower() == "tel":
                found = _phone(unquote(parsed.path))
                if found:
                    original, normalized = found
                    key = _phone_key(normalized)
                    phones.setdefault(key, (original, normalized, []))
                    if page.page_url not in phones[key][2]:
                        phones[key][2].append(page.page_url)
        phone_text = contact_text
        if phone_source:
            phone_text += "\n" + content
        for raw in PHONE_RE.findall(phone_text):
            found = _phone(raw)
            if found:
                original, normalized = found
                key = _phone_key(normalized)
                phones.setdefault(key, (original, normalized, []))
                if page.page_url not in phones[key][2]:
                    phones[key][2].append(page.page_url)
        if person is None and person_source:
            match = PERSON_RE.search(content)
            if match:
                person = ContactValue(value=re.sub(r"\s+", " ", match.group("name")).strip(), source_urls=[page.page_url])

    record_source = business.source_url or business.source_ref or business.website
    if business.email and record_source:
        value = _valid_email(business.email)
        if value:
            emails.setdefault(value, [])
            if record_source not in emails[value]:
                emails[value].append(record_source)
    if business.phone and record_source:
        found = _phone(business.phone)
        if found:
            original, normalized = found
            key = _phone_key(normalized)
            phones.setdefault(key, (original, normalized, []))
            if record_source not in phones[key][2]:
                phones[key][2].append(record_source)

    email_records = [ContactEmail(email=value, source_urls=sources) for value, sources in emails.items()]
    phone_records = [ContactPhone(value=value, normalized=normalized, source_urls=sources) for value, normalized, sources in phones.values()]
    result = ContactExtraction(
        lead_id=business.lead_id, contact_person=person, contact_emails=email_records,
        phone_numbers=phone_records, contact_page_url=website.contact_page_url,
        email=email_records[0].email if email_records else None,
        phone=phone_records[0].normalized if phone_records else None,
    )
    result.field_sources = {
        "contact_person": person.source_urls if person else [],
        "contact_emails": list(dict.fromkeys(url for item in email_records for url in item.source_urls)),
        "phone_numbers": list(dict.fromkeys(url for item in phone_records for url in item.source_urls)),
        "contact_page_url": [website.contact_page_url] if website.contact_page_url else [],
    }
    return result

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
