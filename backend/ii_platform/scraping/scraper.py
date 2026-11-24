from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from ii_platform.browser.browser import BrowserClient

@dataclass
class ScrapedPage:
    url: str
    content: str
    status: int = 200

class WebScraper:
    """HTTP collector for public, domain-scoped pages; intentionally not an NSE/BSE UI scraper."""
    def __init__(self, browser=None): self.browser=browser or BrowserClient()
    async def fetch(self,url: str) -> ScrapedPage:
        parsed=urlparse(url)
        if parsed.scheme not in {"http","https"} or not parsed.netloc:
            raise ValueError("Only absolute HTTP(S) URLs are supported")
        return ScrapedPage(url=url, content=await self.browser.fetch(url))
