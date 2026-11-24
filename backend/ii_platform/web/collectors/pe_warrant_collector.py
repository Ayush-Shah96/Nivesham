from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterable

from .nse_corporate_announcements_collector import (
    CAPITAL_EVENT_TYPES,
    MAX_HISTORY_DAYS,
    NSECorporateAnnouncementsCollector,
)

logger = logging.getLogger(__name__)
SOURCE_NAME = "NSE Corporate Announcements"

BatchCallback = Callable[
    [list[tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]], int, int],
    Awaitable[None],
]


class PEWarrantCollector:
    """NSE-only capital-action wrapper.

    Discovery is performed exclusively through the NSE Corporate Announcements
    public JSON endpoint. This wrapper keeps the existing Regulatory route contract
    and forwards its per-company batch callback so successful companies are
    committed immediately.
    """

    async def collect(
        self,
        history_days: int = MAX_HISTORY_DAYS,
        allowed_companies: Iterable[dict[str, Any]] | None = None,
        on_source: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_batch: BatchCallback | None = None,
        use_broad_feed: bool = False,
    ) -> dict[str, Any]:
        # The broad endpoint is much cheaper than 2,000+ symbol-scoped requests
        # for a recent incremental window. Full-universe filtering and company-ID
        # reconciliation are handled by the route using the canonical NSE symbol.
        result = await NSECorporateAnnouncementsCollector().collect(
            history_days=history_days,
            allowed_companies=None if use_broad_feed else allowed_companies,
            on_batch=on_batch,
        )
        result["collection_mode"] = "nse_broad_feed" if use_broad_feed else "company_scoped"

        if on_source is not None:
            try:
                await on_source(SOURCE_NAME, result)
            except Exception as exc:
                logger.exception("NSE PE/Warrant source persistence callback failed")
                result.setdefault("source_errors", {})[
                    f"Persistence::{SOURCE_NAME}"
                ] = f"{type(exc).__name__}: {exc}"

        # Keep the result ordered and expose every event type supported by the
        # Regulatory Desk, including ordinary share acquisitions.
        event_types = (*CAPITAL_EVENT_TYPES, "REVIEW")
        buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in event_types}
        for row in result.get("rows") or []:
            event_type = str(row.get("event_type") or "")
            if event_type in buckets:
                buckets[event_type].append(row)

        rows: list[dict[str, Any]] = []
        for event_type in event_types:
            buckets[event_type].sort(
                key=lambda row: row.get("event_date")
                or row.get("transaction_date")
                or 0,
                reverse=True,
            )
            rows.extend(buckets[event_type])

        result.update(
            buckets,
            rows=rows,
            source=SOURCE_NAME,
            sources={"nse": len(rows)},
            event_types=list(event_types),
        )
        return result