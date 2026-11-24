from __future__ import annotations

import asyncio
import random

import httpx


class BrowserClient:
    """
    Bounded HTTP client used by the NewsAlert collectors.

    The previous implementation used timeout=None and three retries. A blocked
    publisher could therefore occupy the whole NewsAlert stage until the master
    worker cancelled it. This client deliberately bounds connect/read/write/pool
    time and uses at most one retry.
    """

    RETRYABLE_STATUS = {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }

    def __init__(
        self,
        timeout: float | None = 20.0,
        retries: int = 1,
    ) -> None:
        self.timeout = float(timeout or 20.0)
        self.retries = max(0, min(int(retries), 1))

    async def fetch(self, url: str) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        timeout = httpx.Timeout(
            self.timeout,
            connect=min(5.0, self.timeout),
            read=self.timeout,
            write=min(10.0, self.timeout),
            pool=min(5.0, self.timeout),
        )

        limits = httpx.Limits(
            max_connections=12,
            max_keepalive_connections=6,
        )

        last_error: Exception | None = None

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
            http2=False,
            limits=limits,
        ) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = await client.get(url)

                    if response.status_code in self.RETRYABLE_STATUS:
                        if attempt < self.retries:
                            await asyncio.sleep(
                                0.5 + random.random() * 0.5
                            )
                            continue

                    response.raise_for_status()
                    return response.text

                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    status_code = (
                        exc.response.status_code
                        if exc.response is not None
                        else None
                    )

                    if (
                        status_code not in self.RETRYABLE_STATUS
                        or attempt >= self.retries
                    ):
                        break

                except (
                    httpx.TimeoutException,
                    httpx.TransportError,
                    OSError,
                ) as exc:
                    last_error = exc

                    if attempt >= self.retries:
                        break

                    await asyncio.sleep(
                        0.5 + random.random() * 0.5
                    )

        raise RuntimeError(
            f"Failed to fetch {url}: {last_error}"
        ) from last_error
