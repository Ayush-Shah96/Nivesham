from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import httpx

logger = logging.getLogger(__name__)

BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/getScripHeaderData/w"


def _number(value):
    if value in (None, "", "-", "NA", "N/A"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class BSEQuoteClient:
    """Worker-only BSE equity LTP client used as a CMP fallback.

    The dashboard never calls this client. The worker refreshes CMPs in the
    background and stores the resulting values in the dashboard snapshot.
    """

    def __init__(self, *, concurrency: int = 6):
        self.concurrency = max(1, min(int(concurrency), 10))
        self._client: httpx.AsyncClient | None = None
        self._blocked_until = 0.0

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154.0 Safari/537.36",
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://www.bseindia.com/",
            },
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    async def get_equity_quote(self, bse_code: str) -> dict[str, float | str | None]:
        code = str(bse_code or "").strip()
        if not code:
            return {"last_price": None, "change_percent": None, "source": "BSE"}
        if time.monotonic() < self._blocked_until:
            return {"last_price": None, "change_percent": None, "source": "BSE"}
        if self._client is None:
            raise RuntimeError("BSEQuoteClient must be used as an async context manager")

        try:
            response = await self._client.get(
                BSE_API_URL,
                params={"Debtflag": "", "scripcode": code, "seriesid": ""},
            )
            if response.status_code == 403:
                self._blocked_until = time.monotonic() + 300.0
                raise RuntimeError("BSE quote API returned HTTP 403; retry after cooldown")
            response.raise_for_status()
            payload = response.json()
            header = payload.get("Header") or {}
            current = _number(header.get("LTP") or header.get("ltp"))
            previous = _number(header.get("PrevClose") or header.get("prevClose"))
            change = None
            if current is not None and previous not in (None, 0):
                change = ((current - previous) / previous) * 100.0
            return {
                "last_price": current,
                "change_percent": change,
                "source": "BSE",
            }
        except Exception:
            logger.debug("BSE CMP lookup failed code=%s", code, exc_info=True)
            raise

    async def get_equity_quotes(self, codes: Iterable[str]) -> dict[str, dict[str, float | str | None]]:
        values = []
        seen = set()
        for code in codes:
            key = str(code or "").strip()
            if key and key not in seen:
                seen.add(key)
                values.append(key)
        semaphore = asyncio.Semaphore(self.concurrency)

        async def fetch(code: str):
            async with semaphore:
                try:
                    return code, await self.get_equity_quote(code)
                except Exception:
                    return code, {"last_price": None, "change_percent": None, "source": "BSE"}

        pairs = await asyncio.gather(*(fetch(code) for code in values))
        return dict(pairs)
