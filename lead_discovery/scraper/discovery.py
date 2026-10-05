"""Business data models, Places API, Maps fallback, and discovery parsing."""
from __future__ import annotations

import asyncio
import hashlib
import httpx
import json
import logging
import os
import re
from bs4 import BeautifulSoup, Tag
from dataclasses import dataclass
from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright, async_playwright
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Any
from urllib.parse import quote_plus, urlparse

@dataclass(frozen=True)
class BrowserConfig:
    headless: bool = True
    navigation_timeout_ms: int = 30_000
    action_timeout_ms: int = 10_000
    slow_mo_ms: int = 0

class BrowserManager:
    """Owns Playwright, browser, context, and one reusable page."""

    def __init__(self, config: BrowserConfig | None = None) -> None:
        self.config = config or BrowserConfig()
        self._playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def start(self) -> Page:
        if self.page is not None:
            return self.page
        self._playwright = await async_playwright().start()
        try:
            self.browser = await self._playwright.chromium.launch(
                headless=self.config.headless, slow_mo=self.config.slow_mo_ms
            )
            self.context = await self.browser.new_context(service_workers="block")
            self.page = await self.context.new_page()
            self.page.set_default_navigation_timeout(self.config.navigation_timeout_ms)
            self.page.set_default_timeout(self.config.action_timeout_ms)
            return self.page
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self.context is not None:
            await self.context.close()
        if self.browser is not None:
            await self.browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self.page = self.context = self.browser = self._playwright = None

    async def __aenter__(self) -> "BrowserManager":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

class BusinessRecord(BaseModel):
    """Verified, basic business listing data returned by discovery."""

    model_config = ConfigDict(extra="ignore")

    lead_id: str
    place_id: str | None = None
    primary_type: str | None = None
    discovery_source: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    source_attributions: list[dict[str, Any]] = Field(default_factory=list)
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
    source_url: str | None = None
    source_ref: str | None = None

    @field_validator("business_name")
    @classmethod
    def name_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("business_name must not be empty")
        return value

    @field_validator("email")
    @classmethod
    def valid_email_only(cls, value: str | None) -> str | None:
        """Reject URLs and malformed values in the email slot."""
        if value is None:
            return None
        value = value.strip()
        if (not value or "://" in value or value.casefold().startswith("www.")
                or not re.fullmatch(r"[^\s@<>]+@[^\s@<>.]+(?:\.[^\s@<>.]+)+", value)):
            return None
        return value.casefold()

    @field_validator("rating")
    @classmethod
    def valid_rating(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 5:
            raise ValueError("rating must be between 0 and 5")
        return value

    @field_validator("review_count")
    @classmethod
    def valid_review_count(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("review_count cannot be negative")
        return value

    @classmethod
    def stable_id(cls, source_url: str | None, name: str, address: str | None, place_id: str | None = None) -> str:
        """Derive a repeatable ID from a listing URL, or name/address if absent."""
        identity = (f"places:{place_id}" if place_id else source_url) or json.dumps(
            [name.casefold().strip(), (address or "").casefold().strip()]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    @classmethod
    def from_extracted(cls, data: dict[str, Any]) -> "BusinessRecord":
        values = dict(data)
        values.setdefault("lead_id", cls.stable_id(values.get("source_url"), values.get("business_name", ""),
                                                    values.get("address"), values.get("place_id")))
        return cls.model_validate(values)

_NUMBER = re.compile(r"[\d,.]+")
_LL = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_PHONE_LIKE = re.compile(r"(?:\+?\d[\d\s().-]{5,}\d)")
_RATING_LIKE = re.compile(r"^\s*\d(?:[.,]\d)?\s*(?:\(.*\))?\s*$")
_NOISE_LINE = re.compile(r"\b(?:open|closed|closes|opens|hours|directions|website|reviews?|\$+|km|mi)\b", re.I)

def parse_latlon(url: str | None) -> tuple[float | None, float | None]:
    """Recover validated India coordinates from a Google Maps place URL."""
    match = _LL.search(url or "")
    if not match:
        return None, None
    latitude, longitude = map(float, match.groups())
    if 6 <= latitude <= 38 and 68 <= longitude <= 98:
        return latitude, longitude
    return None, None

def parse_reviews(text: str | None) -> int | None:
    """Parse an explicit review count without mistaking the rating for the count."""
    text = text or ""
    match = re.search(r"([\d,]+)\s+reviews?\b", text, re.I)
    if match:
        return int(match.group(1).replace(",", ""))
    # Some Maps views render the compact form `4.3(1,240)`.
    match = re.search(r"\b[0-5](?:\.\d+)?\s*(?:stars?\s*)?\(\s*([\d,]+)\s*\)", text, re.I)
    return int(match.group(1).replace(",", "")) if match else None

def _attribute_value(card: Tag, *, item_ids: tuple[str, ...], labels: tuple[str, ...]) -> str | None:
    """Read a value only from Maps' explicit accessible/data-item annotations."""
    for node in card.find_all(True):
        if not isinstance(node, Tag):
            continue
        item_id = node.get("data-item-id")
        if isinstance(item_id, str) and any(item_id.casefold().startswith(item.casefold()) for item in item_ids):
            value = _text(node)
            if value:
                return value
        for attr in ("aria-label", "title", "data-tooltip"):
            label = node.get(attr)
            if not isinstance(label, str):
                continue
            match = re.match(r"\s*(?:" + "|".join(re.escape(item) for item in labels) + r")\s*:\s*(.+?)\s*$", label, re.I)
            if match:
                return match.group(1).strip()
    return None

def _candidate_category(card: Tag, name: str) -> str | None:
    explicit = _attribute_value(card, item_ids=("category",), labels=("category", "business category"))
    if explicit:
        return explicit
    # Maps cards commonly render category as a short standalone text fragment.
    # Only accept a fragment when it is distinguishable from listing metadata.
    for fragment in card.stripped_strings:
        value = re.sub(r"\s+", " ", str(fragment)).strip(" ,|")
        value = re.sub(r"^[^\w]+|[^\w]+$", "", value)
        if (not value or value.casefold() == name.casefold() or len(value) > 65
                or _RATING_LIKE.fullmatch(value) or _PHONE_LIKE.search(value)
                or "@" in value or "http" in value or _NOISE_LINE.search(value)
                or re.search(r"\d{1,2}:\d{2}|\b\d+\s+\w+\s+(?:street|road|rd|ave|avenue|lane|highway)\b", value, re.I)):
            continue
        if re.search(r"\b(?:store|shop|clinic|restaurant|cafe|hotel|agency|school|dentist|salon|hospital|service|contractor|market|company|center|centre|marketing|consultant|builder|repair|hardware|pharmacy|lawyer|accountant)\b", value, re.I):
            return value
    return None

def _candidate_address(card: Tag) -> str | None:
    explicit = _attribute_value(card, item_ids=("address",), labels=("address",))
    if explicit:
        return explicit
    # Address fragments in Maps result cards are often separate text nodes;
    # require a street/locality indicator or multiple comma-separated parts.
    for fragment in card.stripped_strings:
        value = re.sub(r"\s+", " ", str(fragment)).strip(" ,|")
        value = re.sub(r"^[^\w]+|[^\w]+$", "", value)
        if (not value or len(value) > 180 or "@" in value or "http" in value
                or _PHONE_LIKE.search(value) or _RATING_LIKE.fullmatch(value)
                or _NOISE_LINE.search(value)):
            continue
        if (re.search(r"\b(?:street|road|rd\.?|avenue|ave\.?|lane|highway|sector|nagar|colony|building|floor|plot|opp(?:osite)?|near)\b", value, re.I)
                or value.count(",") >= 2):
            return value
    return None

def _text(node: Tag | None) -> str | None:
    if node is None:
        return None
    value = " ".join(node.stripped_strings)
    return value or None

def _parse_listing(card: Tag, page_url: str) -> BusinessRecord | None:
    if not isinstance(card, Tag):
        return None
    link = card.select_one('a[href*="/maps/place/"]')
    source_url = link.get("href") if isinstance(link, Tag) else None
    source_url = source_url if isinstance(source_url, str) else None
    if source_url and source_url.startswith("/"):
        source_url = "https://www.google.com" + source_url
    name = link.get("aria-label") if isinstance(link, Tag) else None
    name = name if isinstance(name, str) else None
    name = (name or _text(card.select_one(".fontHeadlineSmall")) or "").strip()
    if not name:
        return None

    rating = None
    rating_node = card.select_one('[aria-label*="star"], [aria-label*="rating"]')
    rating_text = rating_node.get("aria-label") if isinstance(rating_node, Tag) else ""
    rating_text = rating_text if isinstance(rating_text, str) else ""
    match = _NUMBER.search(rating_text)
    if match:
        try:
            rating = float(match.group().replace(",", ""))
        except ValueError:
            pass

    review_texts = []
    for node in card.find_all(True):
        for attr in ("aria-label", "title"):
            value = node.get(attr)
            if isinstance(value, str) and re.search(r"review|star|rating|\([\d,]+\)", value, re.I):
                review_texts.append(value)
    review_texts.extend(str(fragment) for fragment in card.stripped_strings)
    review_count = next((count for text in review_texts if (count := parse_reviews(text)) is not None), None)

    website_node = card.select_one('a[data-value="Website"], a[aria-label*="Website"], a[data-item-id="authority"]')
    website = website_node.get("href") if isinstance(website_node, Tag) else None
    website = website if isinstance(website, str) else None
    tel_node = card.select_one('a[href^="tel:"]')
    phone_value = tel_node.get("href") if isinstance(tel_node, Tag) else None
    phone = phone_value.removeprefix("tel:") if isinstance(phone_value, str) else None
    phone = phone or _attribute_value(card, item_ids=("phone",), labels=("phone", "telephone"))
    address = _candidate_address(card)
    category = _candidate_category(card, name)

    latitude = longitude = None
    try:
        parsed = urlparse(source_url or page_url)
        coords = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", parsed.path + parsed.query)
        if coords:
            candidate = tuple(map(float, coords.groups()))
            if 6 <= candidate[0] <= 38 and 68 <= candidate[1] <= 98:
                latitude, longitude = candidate
    except (ValueError, TypeError):
        pass
    if latitude is None or longitude is None:
        latitude, longitude = parse_latlon(source_url)

    # Listing card layouts vary; retain source traceability and leave uncertain fields empty.
    return BusinessRecord.from_extracted({
        "discovery_source": "google_maps_browser",
        "business_name": name, "category": category, "address": address,
        "phone": phone, "website": website, "rating": rating,
        "review_count": review_count, "latitude": latitude, "longitude": longitude,
        "source_url": source_url, "source_ref": source_url or page_url,
    })

def parse_business_results(html: str, page_url: str = "https://www.google.com/maps") -> list[BusinessRecord]:
    """Parse visible Maps search cards; malformed or nameless cards are skipped."""
    if not isinstance(html, str) or not html.strip():
        return []
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('div[role="article"]')
    results: list[BusinessRecord] = []
    seen: set[str] = set()
    for card in cards:
        try:
            record = _parse_listing(card, page_url)
        except (ValueError, TypeError):
            continue
        if record is not None and record.lead_id not in seen:
            seen.add(record.lead_id)
            results.append(record)
    return results

def parse_business_detail(html: str, business: BusinessRecord) -> BusinessRecord:
    """Supplement a Maps result from its opened place panel, when fields are labeled."""
    if not isinstance(html, str) or not html.strip():
        return business
    soup = BeautifulSoup(html, "html.parser")
    updates: dict[str, str | None] = {}
    for field, item_ids, labels in (
        ("address", ("address",), ("address",)),
        ("phone", ("phone",), ("phone", "telephone")),
    ):
        value = _attribute_value(soup, item_ids=item_ids, labels=labels)
        if field == "phone":
            for node in soup.select('a[href^="tel:"], [data-item-id^="phone:tel:"]'):
                href = node.get("href")
                item_id = node.get("data-item-id")
                candidate = href.removeprefix("tel:") if isinstance(href, str) else None
                if not candidate and isinstance(item_id, str) and "tel:" in item_id:
                    candidate = item_id.split("tel:", 1)[1]
                if candidate and 7 <= len(re.sub(r"\D", "", candidate)) <= 15:
                    value = candidate
                    break
            if value:
                # Labels can describe a call action; retain only a plausible number.
                match = _PHONE_LIKE.search(value)
                value = match.group(0) if match else None
        if value and not getattr(business, field):
            updates[field] = value

    authority = soup.select_one('a[data-item-id="authority"], a[data-value="Website"], a[aria-label^="Website:"]')
    if authority is not None and not business.website:
        href = authority.get("href")
        if isinstance(href, str) and href.startswith(("http://", "https://")):
            updates["website"] = href
    return business.model_copy(update=updates) if updates else business

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_MAX_RESULTS_PER_QUERY = 60
PLACES_FIELD_MASK = ",".join((
    "places.id", "places.attributions", "places.displayName", "places.primaryType", "places.primaryTypeDisplayName",
    "places.formattedAddress", "places.location", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber", "places.websiteUri", "places.rating",
    "places.userRatingCount", "places.googleMapsUri", "nextPageToken",
))

class PlacesConfigurationError(RuntimeError):
    """Missing or rejected Places API credentials/configuration."""

class PlacesApiError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None,
                 api_status: str | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.api_status = api_status

@dataclass(frozen=True)
class PlacesConfig:
    api_key: str | None = None
    timeout_seconds: float = 15.0
    max_attempts: int = 2
    retry_delay_seconds: float = 0.15

    @classmethod
    def from_env(cls) -> "PlacesConfig":
        attempts = int(os.getenv("LEADSUTRA_PLACES_MAX_ATTEMPTS", "2"))
        timeout = float(os.getenv("LEADSUTRA_PLACES_TIMEOUT_SECONDS", "15"))
        delay = float(os.getenv("LEADSUTRA_PLACES_RETRY_DELAY_SECONDS", "0.15"))
        if attempts < 1 or timeout <= 0 or delay < 0:
            raise ValueError("Invalid Places API retry or timeout configuration")
        return cls(
            api_key=(os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_MAPS_API_KEY") or "").strip() or None,
            timeout_seconds=timeout, max_attempts=attempts, retry_delay_seconds=delay,
        )

def parse_place(place: Any) -> BusinessRecord | None:
    """Normalize one Places API response object; ignore malformed entries."""
    if not isinstance(place, dict):
        return None
    display = place.get("displayName")
    name = display.get("text") if isinstance(display, dict) else None
    if not isinstance(name, str) or not name.strip():
        return None
    primary_display = place.get("primaryTypeDisplayName")
    category = primary_display.get("text") if isinstance(primary_display, dict) else None
    primary_type = place.get("primaryType")
    if not category and isinstance(primary_type, str):
        category = primary_type.replace("_", " ").strip().title() or None
    location = place.get("location")
    location = location if isinstance(location, dict) else {}
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    url_latitude, url_longitude = parse_latlon(place.get("googleMapsUri"))
    if latitude is None:
        latitude = url_latitude
    if longitude is None:
        longitude = url_longitude
    phone = place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber")
    place_id = place.get("id")
    maps_uri = place.get("googleMapsUri")
    source_url = maps_uri if isinstance(maps_uri, str) else None
    source_ref = f"places/{place_id}" if isinstance(place_id, str) and place_id else source_url
    try:
        return BusinessRecord.from_extracted({
            "place_id": place_id if isinstance(place_id, str) else None,
            "primary_type": primary_type if isinstance(primary_type, str) else None,
            "source_attributions": [item for item in place.get("attributions", []) if isinstance(item, dict)]
            if isinstance(place.get("attributions", []), list) else [],
            "discovery_source": "google_places",
            "business_name": name.strip(), "category": category,
            "address": place.get("formattedAddress"),
            "phone": phone if isinstance(phone, str) else None,
            "website": place.get("websiteUri"),
            "latitude": latitude, "longitude": longitude,
            "rating": place.get("rating"), "review_count": place.get("userRatingCount"),
            "source_url": source_url, "source_ref": source_ref,
        })
    except (TypeError, ValueError):
        return None

class GooglePlacesClient:
    def __init__(self, config: PlacesConfig | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config or PlacesConfig.from_env()
        self.client = client
        self.last_records: list[BusinessRecord] = []

    async def search(self, query: str, location: str, limit: int) -> list[BusinessRecord]:
        self.last_records = []
        if not self.config.api_key or not self.config.api_key.strip():
            raise PlacesConfigurationError("Missing Places API key; set GOOGLE_PLACES_API_KEY")
        if not query.strip() or not location.strip() or limit < 1:
            raise ValueError("query, location, and a positive result limit are required")
        own_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=httpx.Timeout(self.config.timeout_seconds))
        records: list[BusinessRecord] = []
        seen: set[str] = set()
        effective_limit = min(limit, PLACES_MAX_RESULTS_PER_QUERY)
        page_token: str | None = None
        try:
            while len(records) < effective_limit:
                body: dict[str, Any] = {"textQuery": f"{query.strip()} in {location.strip()}",
                                        "pageSize": min(20, effective_limit - len(records))}
                if page_token:
                    body["pageToken"] = page_token
                payload = await self._request_page(client, body)
                places = payload.get("places", [])
                if not isinstance(places, list):
                    raise PlacesApiError("Places API returned an invalid places field", retryable=False)
                for raw in places:
                    record = parse_place(raw)
                    if record and record.lead_id not in seen:
                        seen.add(record.lead_id)
                        records.append(record)
                        self.last_records = list(records)
                        if len(records) >= effective_limit:
                            break
                page_token = payload.get("nextPageToken")
                if not page_token or not places:
                    break
            return records[:effective_limit]
        finally:
            if own_client:
                await client.aclose()

    async def _request_page(self, client: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
        last_error: PlacesApiError | None = None
        for attempt in range(self.config.max_attempts):
            try:
                response = await client.post(
                    PLACES_SEARCH_URL, json=body,
                    headers={"Content-Type": "application/json", "X-Goog-Api-Key": self.config.api_key or "",
                             "X-Goog-FieldMask": PLACES_FIELD_MASK},
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = PlacesApiError(f"Places API connection error: {type(exc).__name__}: {exc}", retryable=True)
                if attempt + 1 < self.config.max_attempts:
                    await asyncio.sleep(self.config.retry_delay_seconds)
                    continue
                raise last_error from exc
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            api_error = payload.get("error", {}) if isinstance(payload, dict) else {}
            api_status = api_error.get("status") if isinstance(api_error, dict) else None
            api_message = api_error.get("message") if isinstance(api_error, dict) else None
            if response.status_code >= 400:
                message = f"Places API HTTP {response.status_code}"
                if api_status:
                    message += f" {api_status}"
                if api_message:
                    message += f": {api_message}"
                if (response.status_code in {401, 403} or
                        api_status in {"PERMISSION_DENIED", "UNAUTHENTICATED", "API_KEY_INVALID"} or
                        "api key not valid" in message.casefold()):
                    raise PlacesConfigurationError(message)
                retryable = response.status_code == 429 or response.status_code in {500, 502, 503, 504}
                last_error = PlacesApiError(message, retryable=retryable, status_code=response.status_code,
                                            api_status=api_status)
                if retryable and attempt + 1 < self.config.max_attempts:
                    await asyncio.sleep(self.config.retry_delay_seconds)
                    continue
                raise last_error
            if not isinstance(payload, dict):
                raise PlacesApiError("Places API returned a non-object response", retryable=False)
            return payload
        raise last_error or PlacesApiError("Places API request failed", retryable=False)

logger = logging.getLogger(__name__)

class GoogleMapsBrowserDiscovery:
    """Bounded Google Maps search discovery. Does not crawl business websites."""

    def __init__(self, browser_config: BrowserConfig | None = None, scroll_pause_ms: int = 1_200) -> None:
        self.browser_config = browser_config or BrowserConfig()
        self.scroll_pause_ms = max(0, scroll_pause_ms)

    async def search(self, query: str, location: str, limit: int = 50) -> list[BusinessRecord]:
        if not query.strip() or not location.strip():
            raise ValueError("query and location are required")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        manager = BrowserManager(self.browser_config)
        records: list[BusinessRecord] = []
        seen: set[str] = set()
        try:
            page = await manager.start()
            url = "https://www.google.com/maps/search/" + quote_plus(f"{query} in {location}")
            try:
                await page.goto(url, wait_until="domcontentloaded")
                await page.wait_for_timeout(self.scroll_pause_ms)
            except PlaywrightError as exc:
                logger.warning("Maps navigation failed: %s", exc)
                raise

            last_count = -1
            stagnant = 0
            while len(records) < limit and stagnant < 3:
                for record in parse_business_results(await page.content(), page.url):
                    if record.lead_id not in seen:
                        seen.add(record.lead_id)
                        records.append(record)
                        if len(records) >= limit:
                            break
                if len(records) >= limit:
                    break
                try:
                    feed = page.locator('div[role="feed"]').first
                    if await feed.count() == 0:
                        break
                    await feed.evaluate("el => el.scrollBy(0, el.clientHeight)")
                    await page.wait_for_timeout(self.scroll_pause_ms)
                except PlaywrightError as exc:
                    logger.warning("Maps result scrolling failed: %s", exc)
                    if not records:
                        raise
                    break
                current_count = len(parse_business_results(await page.content(), page.url))
                stagnant = stagnant + 1 if current_count <= last_count else 0
                last_count = current_count
            # Search cards frequently show a Call button without exposing the
            # number. Opening the Maps place URL reveals its labeled details.
            # Do this only for records missing basic fields and keep failures local.
            detail_slots = asyncio.Semaphore(3)

            async def enrich(record):
                if record.source_url and (not record.phone or not record.address or not record.website):
                    async with detail_slots:
                        detail = None
                        try:
                            detail = await manager.context.new_page()
                            await detail.goto(record.source_url, wait_until="domcontentloaded",
                                              timeout=self.browser_config.navigation_timeout_ms)
                            await detail.wait_for_timeout(min(self.scroll_pause_ms, 500))
                            record = parse_business_detail(await detail.content(), record)
                        except PlaywrightError as exc:
                            logger.warning("Maps detail lookup failed for %s: %s", record.lead_id, exc)
                        finally:
                            if detail is not None:
                                await detail.close()
                return record

            records = await asyncio.gather(*(enrich(record) for record in records))
        finally:
            await manager.close()
        return records[:limit]

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

class BusinessDiscovery:
    """Places API primary discovery with explicitly opted-in Maps fallback."""

    def __init__(self, browser_config: BrowserConfig | None = None, scroll_pause_ms: int = 1_200,
                 *, places_client: GooglePlacesClient | None = None,
                 browser_discovery: GoogleMapsBrowserDiscovery | None = None) -> None:
        self.places_client = places_client or GooglePlacesClient()
        self.browser_discovery = browser_discovery or GoogleMapsBrowserDiscovery(browser_config, scroll_pause_ms)
        self.allow_browser_fallback = _env_bool("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK")
        self.fallback_on_zero_results = _env_bool("LEADSUTRA_MAPS_FALLBACK_ON_ZERO_RESULTS")
        self.diagnostics: dict[str, object] = {
            "source": "google_places", "fallback_used": False, "fallback_reason": None,
            "fallback_attempted": False, "fallback_enabled": self.allow_browser_fallback,
            "missing_key_fallback_enabled": True,
            "zero_result_fallback_enabled": self.fallback_on_zero_results,
            "original_api_failure": None,
        }

    async def search(self, query: str, location: str, limit: int = 50) -> list[BusinessRecord]:
        self.diagnostics = {"source": "google_places", "fallback_used": False,
                            "fallback_reason": None, "fallback_attempted": False,
                            "fallback_enabled": self.allow_browser_fallback,
                            "missing_key_fallback_enabled": True,
                            "zero_result_fallback_enabled": self.fallback_on_zero_results,
                            "provider_result_cap_per_query": PLACES_MAX_RESULTS_PER_QUERY,
                            "requested_limit": limit,
                            "original_api_failure": None}
        try:
            records = await self.places_client.search(query, location, limit)
        except PlacesConfigurationError as exc:
            self.diagnostics["error"] = str(exc)
            if "missing places api key" in str(exc).casefold():
                self.diagnostics["original_api_failure"] = str(exc)
                return await self._browser_fallback(query, location, limit, "missing_places_api_key")
            raise
        except PlacesApiError as exc:
            partial_records = list(getattr(self.places_client, "last_records", []))
            self.diagnostics["original_api_failure"] = str(exc)
            if partial_records:
                self.diagnostics["partial_results_preserved"] = True
                self.diagnostics["result_count"] = len(partial_records)
                return _deduplicate_records(partial_records)[:limit]
            if not (self.allow_browser_fallback and exc.retryable):
                raise
            reason = f"transient_api_failure:{exc.status_code or exc.api_status or type(exc).__name__}"
            return await self._browser_fallback(query, location, limit, reason)
        self.diagnostics["result_count"] = len(records)
        self.diagnostics["limit_capped_by_provider"] = limit > PLACES_MAX_RESULTS_PER_QUERY and len(records) >= PLACES_MAX_RESULTS_PER_QUERY
        if not records and self.allow_browser_fallback and self.fallback_on_zero_results:
            return await self._browser_fallback(query, location, limit, "places_api_zero_results")
        return _deduplicate_records(records)[:limit]

    async def _browser_fallback(self, query: str, location: str, limit: int, reason: str) -> list[BusinessRecord]:
        logger.warning("Places discovery unavailable (%s); falling back to Maps browser: %s",
                       reason, self.diagnostics.get("original_api_failure"))
        self.diagnostics.update({"source": "google_maps_browser", "fallback_attempted": True,
                                 "fallback_reason": reason})
        try:
            records = await self.browser_discovery.search(query, location, limit)
        except Exception as exc:
            detail = str(exc).strip()
            if isinstance(exc, NotImplementedError):
                loop_name = type(asyncio.get_running_loop()).__name__
                detail = detail or f"Playwright could not start its browser driver (event loop: {loop_name})"
                if os.name == "nt":
                    detail += "; on Windows run Uvicorn without --reload so Playwright can use a subprocess-capable event loop"
            self.diagnostics["fallback_error"] = f"{type(exc).__name__}: {detail}"
            raise
        normalized = [record.model_copy(update={"discovery_source": "google_maps_browser",
                                                 "fallback_used": True, "fallback_reason": reason})
                      for record in records]
        self.diagnostics.update({"source": "google_maps_browser", "fallback_used": True,
                                 "fallback_attempted": True,
                                 "fallback_reason": reason, "result_count": len(normalized)})
        return _deduplicate_records(normalized)[:limit]

def _deduplicate_records(records: list[BusinessRecord]) -> list[BusinessRecord]:
    unique: list[BusinessRecord] = []
    seen: set[str] = set()
    for record in records:
        normalized_name = re.sub(r"\s+", " ", record.business_name.casefold()).strip()
        name_address = (f"name-address:{normalized_name}|{record.address.casefold().strip()}"
                        if record.address else None)
        identities = [f"place:{record.place_id}" if record.place_id else f"lead:{record.lead_id}"]
        if name_address:
            identities.append(name_address)
        if not any(identity in seen for identity in identities):
            seen.update(identities)
            unique.append(record)
    return unique
