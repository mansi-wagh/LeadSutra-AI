from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from .models import BusinessRecord
from .parsing import parse_latlon

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
