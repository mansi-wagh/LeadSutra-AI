import asyncio
import sys

import httpx
import pytest

from scraper.discovery import BusinessDiscovery, _deduplicate_records
from scraper.discovery import BusinessRecord
from scraper.discovery import (
    GooglePlacesClient, PlacesApiError, PlacesConfig, PlacesConfigurationError,
    PLACES_FIELD_MASK, PLACES_SEARCH_URL, parse_place,
)


def place(place_id="ChIJ-test", name="Nashik Dental", **extra):
    return {"id": place_id, "displayName": {"text": name},
            "primaryType": "dentist", "primaryTypeDisplayName": {"text": "Dentist"},
            "formattedAddress": "Nashik, Maharashtra, India",
            "location": {"latitude": 19.99, "longitude": 73.78},
            "internationalPhoneNumber": "+91 98765 43210",
            "websiteUri": "https://clinic.example", "rating": 4.6,
            "userRatingCount": 84, "googleMapsUri": "https://maps.google.com/?cid=abc",
            **extra}


class Response:
    def __init__(self, data, status_code=200):
        self.data, self.status_code = data, status_code
    def json(self): return self.data


class FakeHttp:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception): raise response
        return response


class FakePlaces:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.last_records = result or [], error, []
        self.calls = []
    async def search(self, query, location, limit):
        self.calls.append((query, location, limit))
        if self.error: raise self.error
        return self.result[:limit]


class FakeBrowser:
    def __init__(self, records): self.records, self.calls = records, []
    async def search(self, query, location, limit):
        self.calls.append((query, location, limit))
        return self.records[:limit]


def test_places_text_search_request_field_mask_and_response_mapping():
    http = FakeHttp([Response({"places": [place()]})])
    client = GooglePlacesClient(PlacesConfig(api_key="test-key"), client=http)
    records = asyncio.run(client.search("Dental Clinics", "Nashik", 20))
    url, request = http.requests[0]
    assert url == PLACES_SEARCH_URL
    assert request["headers"]["X-Goog-Api-Key"] == "test-key"
    assert request["headers"]["X-Goog-FieldMask"] == PLACES_FIELD_MASK
    assert request["json"] == {"textQuery": "Dental Clinics in Nashik", "pageSize": 20}
    record = records[0]
    assert record.business_name == "Nashik Dental"
    assert record.category == "Dentist"
    assert record.address == "Nashik, Maharashtra, India"
    assert (record.latitude, record.longitude) == (19.99, 73.78)
    assert record.phone == "+91 98765 43210"
    assert record.review_count == 84
    assert record.place_id == "ChIJ-test"
    assert record.discovery_source == "google_places"


def test_places_pages_results_to_requested_limit_and_deduplicates():
    http = FakeHttp([
        Response({"places": [place("id-1"), place("id-2")], "nextPageToken": "next"}),
        Response({"places": [place("id-2"), place("id-3")] }),
    ])
    client = GooglePlacesClient(PlacesConfig(api_key="key"), client=http)
    records = asyncio.run(client.search("dentists", "Nashik", 3))
    assert len(records) == 3
    assert http.requests[0][1]["json"]["pageSize"] == 3
    assert http.requests[1][1]["json"]["pageToken"] == "next"


def test_places_source_cap_and_page_size_limit():
    pages = []
    for page_index in range(3):
        items = [place(f"id-{page_index}-{i}", f"Clinic {page_index}-{i}") for i in range(20)]
        data = {"places": items}
        if page_index < 2:
            data["nextPageToken"] = f"page-{page_index + 1}"
        pages.append(Response(data))
    http = FakeHttp(pages)
    records = asyncio.run(GooglePlacesClient(PlacesConfig(api_key="key"), client=http).search("clinic", "Nashik", 250))
    assert len(records) == 60
    assert len(http.requests) == 3
    assert all(request[1]["json"]["pageSize"] == 20 for request in http.requests)


def test_missing_optional_fields_do_not_cause_browser_fallback(monkeypatch):
    monkeypatch.setenv("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK", "true")
    monkeypatch.setenv("LEADSUTRA_MAPS_FALLBACK_ON_ZERO_RESULTS", "true")
    http = FakeHttp([Response({"places": [{"id": "id-1", "displayName": {"text": "Clinic"}}]})])
    places = GooglePlacesClient(PlacesConfig(api_key="key"), client=http)
    browser = FakeBrowser([])
    discovery = BusinessDiscovery(places_client=places, browser_discovery=browser)
    records = asyncio.run(discovery.search("clinic", "Nashik", 5))
    assert len(records) == 1
    assert records[0].website is None and records[0].rating is None
    assert browser.calls == []


def test_transient_failure_uses_fallback_only_when_enabled(monkeypatch):
    monkeypatch.setenv("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK", "true")
    places = FakePlaces(error=PlacesApiError("HTTP 503", retryable=True, status_code=503))
    record = BusinessRecord.from_extracted({"business_name": "Browser Dental", "address": "Nashik"})
    browser = FakeBrowser([record])
    discovery = BusinessDiscovery(places_client=places, browser_discovery=browser)
    results = asyncio.run(discovery.search("dentist", "Nashik", 2))
    assert len(results) == 1
    assert results[0].discovery_source == "google_maps_browser"
    assert results[0].fallback_used is True
    assert discovery.diagnostics["fallback_reason"] == "transient_api_failure:503"
    assert "HTTP 503" in discovery.diagnostics["original_api_failure"]


def test_nontransient_credential_failure_never_falls_back(monkeypatch):
    monkeypatch.setenv("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK", "true")
    browser = FakeBrowser([])
    discovery = BusinessDiscovery(
        places_client=FakePlaces(error=PlacesConfigurationError("API key invalid")),
        browser_discovery=browser,
    )
    with pytest.raises(PlacesConfigurationError):
        asyncio.run(discovery.search("dentist", "Nashik", 5))
    assert browser.calls == []


def test_http_permission_error_never_falls_back(monkeypatch):
    monkeypatch.setenv("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK", "true")
    http = FakeHttp([Response({"error": {"status": "PERMISSION_DENIED", "message": "API key invalid"}}, 403)])
    browser = FakeBrowser([])
    discovery = BusinessDiscovery(
        places_client=GooglePlacesClient(PlacesConfig(api_key="bad-key", max_attempts=1), client=http),
        browser_discovery=browser,
    )
    with pytest.raises(PlacesConfigurationError):
        asyncio.run(discovery.search("clinic", "Nashik", 2))
    assert browser.calls == []


def test_missing_api_key_automatically_uses_browser_fallback(monkeypatch):
    monkeypatch.delenv("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK", raising=False)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)
    record = BusinessRecord.from_extracted({"business_name": "Fallback Dental", "address": "Pune"})
    browser = FakeBrowser([record])
    discovery = BusinessDiscovery(
        places_client=GooglePlacesClient(PlacesConfig(api_key=None)), browser_discovery=browser,
    )
    result = asyncio.run(discovery.search("clinic", "Nashik", 2))
    assert len(browser.calls) == 1
    assert result[0].business_name == "Fallback Dental"
    assert result[0].discovery_source == "google_maps_browser"
    assert result[0].fallback_used is True
    assert discovery.diagnostics["fallback_reason"] == "missing_places_api_key"
    assert discovery.diagnostics["source"] == "google_maps_browser"


def test_empty_not_implemented_fallback_error_has_playwright_hint(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_MAPS_API_KEY", raising=False)

    class UnsupportedBrowser:
        async def search(self, *_args):
            raise NotImplementedError()

    discovery = BusinessDiscovery(
        places_client=GooglePlacesClient(PlacesConfig(api_key=None)),
        browser_discovery=UnsupportedBrowser(),
    )
    with pytest.raises(NotImplementedError):
        asyncio.run(discovery.search("clinic", "Nashik", 1))
    assert "Playwright could not start its browser driver" in discovery.diagnostics["fallback_error"]
    if sys.platform == "win32":
        assert "without --reload" in discovery.diagnostics["fallback_error"]


def test_connection_timeout_retries_before_success():
    timeout = httpx.ReadTimeout("timeout", request=httpx.Request("POST", PLACES_SEARCH_URL))
    http = FakeHttp([timeout, Response({"places": [place()]})])
    records = asyncio.run(GooglePlacesClient(
        PlacesConfig(api_key="key", max_attempts=2, retry_delay_seconds=0), client=http,
    ).search("clinic", "Nashik", 1))
    assert len(records) == 1
    assert len(http.requests) == 2


@pytest.mark.parametrize("fallback_on_zero", [False, True])
def test_zero_results_policy_is_separate_from_api_failure(monkeypatch, fallback_on_zero):
    monkeypatch.setenv("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK", "true")
    monkeypatch.setenv("LEADSUTRA_MAPS_FALLBACK_ON_ZERO_RESULTS", str(fallback_on_zero).lower())
    record = BusinessRecord.from_extracted({"business_name": "Fallback Clinic"})
    browser = FakeBrowser([record])
    discovery = BusinessDiscovery(places_client=FakePlaces([]), browser_discovery=browser)
    results = asyncio.run(discovery.search("dentist", "Nashik", 5))
    assert bool(browser.calls) is fallback_on_zero
    assert len(results) == int(fallback_on_zero)


def test_fallback_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LEADSUTRA_ENABLE_MAPS_BROWSER_FALLBACK", raising=False)
    browser = FakeBrowser([])
    discovery = BusinessDiscovery(
        places_client=FakePlaces(error=PlacesApiError("timeout", retryable=True)), browser_discovery=browser,
    )
    with pytest.raises(PlacesApiError):
        asyncio.run(discovery.search("dentist", "Nashik", 5))
    assert browser.calls == []


def test_deduplicates_normalized_results_across_sources():
    api = BusinessRecord.from_extracted({"place_id": "id-a", "business_name": "Clinic", "address": "Nashik"})
    browser = BusinessRecord.from_extracted({"business_name": " clinic ", "address": "nashik"})
    assert len(_deduplicate_records([api, browser])) == 1


def test_parse_place_rejects_invalid_record_without_name():
    assert parse_place({"id": "missing-name"}) is None


def test_places_values_stay_authoritative_and_map_url_fills_missing_coordinates():
    uri = "https://www.google.com/maps/place/Clinic/data=!3d19.0965673!4d72.8422538"
    record = parse_place(place(location={"latitude": 19.9}, googleMapsUri=uri))
    assert record is not None
    assert (record.latitude, record.longitude) == (19.9, 72.8422538)
    assert record.review_count == 84


def test_place_url_coordinates_outside_india_are_not_used_as_fallback():
    record = parse_place(place(location={}, googleMapsUri="https://maps.google.com/?data=!3d45.0!4d100.0"))
    assert record is not None
    assert record.latitude is None and record.longitude is None
