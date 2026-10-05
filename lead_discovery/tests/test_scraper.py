from pathlib import Path

import asyncio

import pytest
from pydantic import ValidationError

from scraper.discovery import BrowserConfig, BrowserManager
from scraper.discovery import GoogleMapsBrowserDiscovery
from scraper.discovery import BusinessRecord
from scraper.discovery import parse_business_results, parse_latlon, parse_reviews

FIXTURE = Path(__file__).parent / "fixtures" / "maps_results.html"


def test_result_parsing_and_duplicate_removal():
    records = parse_business_results(FIXTURE.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0].business_name == "Example Cafe"
    assert records[0].rating == 4.5
    assert records[0].review_count == 120
    assert records[0].website == "https://example.invalid"
    assert records[0].phone == "+911234567890"
    assert records[0].source_url.startswith("https://www.google.com/maps/place/")
    assert records[0].lead_id


def test_missing_fields_are_null():
    html = '<div role="article"><a href="/maps/place/Small+Shop" aria-label="Small Shop"></a></div>'
    record = parse_business_results(html)[0]
    assert record.email is None
    assert record.address is None
    assert record.description is None


@pytest.mark.parametrize(("text", "expected"), [
    ("4.3 stars 1,240 Reviews", 1240),
    ("4.3(1,240)", 1240),
    ("4.5 stars 25 Reviews", 25),
    ("No reviews shown", None),
    ("0 reviews", 0),
])
def test_review_count_text_parsing(text, expected):
    assert parse_reviews(text) == expected


def test_maps_url_coordinate_fallback_and_bounds():
    url = "https://www.google.com/maps/place/Clinic/data=!4m2!3m1!1s0!3d19.0965673!4d72.8422538"
    assert parse_latlon(url) == (19.0965673, 72.8422538)
    assert parse_latlon("https://google.com/maps/place/A!3d45.0!4d100.0") == (None, None)
    assert parse_latlon("https://google.com/maps/place/A") == (None, None)


def test_maps_result_reads_coordinates_and_review_count_from_actual_card_text():
    html = '''<div role="article">
      <a href="https://www.google.com/maps/place/Clinic/data=!3d19.0965673!4d72.8422538" aria-label="Clinic"></a>
      <span aria-label="4.3 stars 1,240 Reviews"></span>
    </div>'''
    record = parse_business_results(html)[0]
    assert (record.latitude, record.longitude) == (19.0965673, 72.8422538)
    assert record.rating == 4.3
    assert record.review_count == 1240


def test_invalid_or_incomplete_records():
    with pytest.raises(ValidationError):
        BusinessRecord.from_extracted({"business_name": "  "})
    assert parse_business_results('<div role="article"><span>anonymous</span></div>') == []


def test_stable_id_without_source_url():
    first = BusinessRecord.from_extracted({"business_name": "Shop", "address": "Main St"})
    second = BusinessRecord.from_extracted({"business_name": "Shop", "address": "Main St"})
    assert first.lead_id == second.lead_id


def test_browser_configuration_and_initialization(monkeypatch):
    class Page:
        def set_default_navigation_timeout(self, value): self.navigation_timeout = value
        def set_default_timeout(self, value): self.timeout = value
    class Context:
        async def new_page(self): return page
        async def close(self): pass
    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs["service_workers"] == "block"
            return context
        async def close(self): pass
    class Chromium:
        async def launch(self, **kwargs):
            assert kwargs["headless"] is True
            return browser
    class Playwright:
        chromium = Chromium()
        async def stop(self): pass
    class Starter:
        async def start(self): return playwright
    page, context, browser, playwright = Page(), Context(), Browser(), Playwright()
    monkeypatch.setattr("scraper.discovery.async_playwright", lambda: Starter())
    manager = BrowserManager(BrowserConfig(headless=True, navigation_timeout_ms=1234))
    result = asyncio.run(manager.start())
    assert result is page
    assert page.navigation_timeout == 1234
    asyncio.run(manager.close())


def test_configurable_limit_and_browser_failure(monkeypatch):
    class FakeManager:
        def __init__(self, config):
            self.closed = False
        async def start(self):
            raise RuntimeError("browser unavailable")
        async def close(self):
            self.closed = True

    monkeypatch.setattr("scraper.discovery.BrowserManager", FakeManager)
    discovery = GoogleMapsBrowserDiscovery()
    with pytest.raises(RuntimeError):
        asyncio.run(discovery.search("cafes", "Bengaluru", limit=3))
    with pytest.raises(ValueError):
        asyncio.run(discovery.search("cafes", "Bengaluru", limit=0))


def test_navigation_failure_is_reported(monkeypatch):
    from playwright.async_api import Error as PlaywrightError

    class FakePage:
        async def goto(self, *args, **kwargs):
            raise PlaywrightError("navigation failed")
    class FakeManager:
        def __init__(self, config): pass
        async def start(self): return FakePage()
        async def close(self): pass
    monkeypatch.setattr("scraper.discovery.BrowserManager", FakeManager)
    with pytest.raises(PlaywrightError, match='navigation failed'):
        asyncio.run(GoogleMapsBrowserDiscovery().search("cafes", "Bengaluru", limit=4))


def test_maps_details_overlap_and_preserve_failed_records(monkeypatch):
    from playwright.async_api import Error as PlaywrightError
    active = peak = closed = 0
    records = [BusinessRecord(lead_id=str(i), business_name=f'Business {i}',
                              source_url=f'https://www.google.com/maps/place/{i}') for i in range(5)]
    class Page:
        url = 'https://www.google.com/maps/'
        async def goto(self, *args, **kwargs): pass
        async def wait_for_timeout(self, *args): pass
        async def content(self): return ''
    class Detail(Page):
        async def goto(self, url, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            if url.endswith('/2'):
                raise PlaywrightError('detail failed')
        async def close(self):
            nonlocal active, closed
            active -= 1
            closed += 1
    class Manager:
        def __init__(self, config): self.context = self
        async def start(self): return Page()
        async def new_page(self): return Detail()
        async def close(self): pass
    monkeypatch.setattr('scraper.discovery.BrowserManager', Manager)
    monkeypatch.setattr('scraper.discovery.parse_business_results', lambda *args: records)
    result = asyncio.run(GoogleMapsBrowserDiscovery().search('Dentist', 'Nashik', 5))
    assert result == records
    assert peak == 3 and closed == 5 and active == 0
