from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


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
            self.context = await self.browser.new_context()
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
