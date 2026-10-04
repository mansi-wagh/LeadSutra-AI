from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote_plus

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserConfig, BrowserManager
from .models import BusinessRecord
from .parsing import parse_business_detail, parse_business_results
from .places import GooglePlacesClient, PlacesApiError, PlacesConfigurationError, PLACES_MAX_RESULTS_PER_QUERY

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
                return []

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
                    break
                current_count = len(parse_business_results(await page.content(), page.url))
                stagnant = stagnant + 1 if current_count <= last_count else 0
                last_count = current_count
            # Search cards frequently show a Call button without exposing the
            # number. Opening the Maps place URL reveals its labeled details.
            # Do this only for records missing basic fields and keep failures local.
            enriched: list[BusinessRecord] = []
            for record in records:
                if record.source_url and (not record.phone or not record.address or not record.website):
                    try:
                        await page.goto(record.source_url, wait_until="domcontentloaded")
                        await page.wait_for_timeout(min(self.scroll_pause_ms, 500))
                        record = parse_business_detail(await page.content(), record)
                    except PlaywrightError as exc:
                        logger.warning("Maps detail lookup failed for %s: %s", record.lead_id, exc)
                enriched.append(record)
            records = enriched
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
        self.diagnostics.update({"source": "google_maps_browser", "fallback_attempted": True,
                                 "fallback_reason": reason})
        try:
            records = await self.browser_discovery.search(query, location, limit)
        except Exception as exc:
            self.diagnostics["fallback_error"] = f"{type(exc).__name__}: {exc}"
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
