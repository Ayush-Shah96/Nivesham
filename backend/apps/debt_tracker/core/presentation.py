"""JSON report contract for the Debt Tracker UI and quarterly Excel output."""
from dataclasses import asdict
from typing import Any, Mapping

from .engine import CATEGORY_LIMITS, special_category_display_groups, _movement_identity
from .models import ExposureResult
from .movements import group_movement_eligible, trend_status
from .normalization import normalize_text
from .policies import limit_bucket


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _security_key(row: Mapping[str, Any]) -> str:
    isin = str(row.get("isin") or "").strip().upper()
    if isin:
        return f"isin:{isin}"
    paper = normalize_text(row.get("paper"))
    issuer = normalize_text(row.get("issuer"))
    maturity = str(row.get("maturity") or "")[:10]
    return f"paper:{paper}|issuer:{issuer}|maturity:{maturity}"


def _quarterly_movements(result: ExposureResult) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str], list[str]]:
    """Compare previous BASE rows to current BASE rows at security level.

    ISIN is preferred. The June BASE supplied for this report has no ISIN column,
    so a paper + issuer + maturity key bridges an ISIN-less historical row to one
    unambiguous current ISIN. Ambiguous many-ISIN matches are deliberately not merged.
    """
    previous_rows = [dict(row) for row in result.opening_details]
    current_rows = [asdict(row) for row in result.rows]

    def fallback_key(row):
        paper = normalize_text(row.get("paper"))
        issuer = normalize_text(row.get("issuer"))
        maturity = str(row.get("maturity") or "")[:10]
        return f"paper:{paper}|issuer:{issuer}|maturity:{maturity}"

    previous_isins_by_fallback: dict[str, set[str]] = {}
    current_isins_by_fallback: dict[str, set[str]] = {}
    previous_missing: set[str] = set()
    current_missing: set[str] = set()
    for row in previous_rows:
        fallback = fallback_key(row)
        isin = str(row.get("isin") or "").strip().upper()
        if not isin:
            previous_missing.add(fallback)
        else:
            previous_isins_by_fallback.setdefault(fallback, set()).add(isin)
    for row in current_rows:
        fallback = fallback_key(row)
        isin = str(row.get("isin") or "").strip().upper()
        if not isin:
            current_missing.add(fallback)
        else:
            current_isins_by_fallback.setdefault(fallback, set()).add(isin)

    def identity_key(row, side):
        isin = str(row.get("isin") or "").strip().upper()
        fallback = fallback_key(row)
        if not isin:
            opposite_missing = current_missing if side == "previous" else previous_missing
            opposite_isins = current_isins_by_fallback if side == "previous" else previous_isins_by_fallback
            if fallback in opposite_missing or len(opposite_isins.get(fallback, set())) == 1:
                return fallback
            return fallback
        opposite_missing = current_missing if side == "previous" else previous_missing
        opposite_isins = current_isins_by_fallback if side == "previous" else previous_isins_by_fallback
        if isin in opposite_isins.get(fallback, set()):
            return f"isin:{isin}"
        # A missing-ISIN BASE entry and exactly one current ISIN with the same
        # paper/issuer/maturity are considered the same security, not an exit+add.
        if side == "current" and fallback in previous_missing and len(current_isins_by_fallback.get(fallback, set())) == 1:
            return fallback
        if side == "previous" and fallback in current_missing and len(previous_isins_by_fallback.get(fallback, set())) == 1:
            return fallback
        return f"isin:{isin}"

    previous: dict[str, dict[str, Any]] = {}
    current: dict[str, dict[str, Any]] = {}

    def add_position(target, key, data):
        entry = target.setdefault(key, {
            "paper": data.get("paper") or "", "isin": data.get("isin") or "",
            "issuer": data.get("issuer") or "", "group": data.get("group") or "Others",
            "exposure": 0.0, "funds": set(), "maturity": data.get("maturity") or "",
        })
        entry["exposure"] += float(data.get("exposure") or 0)
        if data.get("fund"):
            entry["funds"].add(data["fund"])
        if not entry.get("isin") and data.get("isin"):
            entry["isin"] = data["isin"]
        if not entry.get("group") and data.get("group"):
            entry["group"] = data["group"]

    for data in previous_rows:
        key = identity_key(data, "previous")
        if key not in {"paper:|issuer:|maturity:"}:
            add_position(previous, key, data)
    for data in current_rows:
        key = identity_key(data, "current")
        if key not in {"paper:|issuer:|maturity:"}:
            add_position(current, key, data)

    movements: list[dict[str, Any]] = []
    for key in sorted(previous.keys() | current.keys()):
        old = previous.get(key, {})
        new = current.get(key, {})
        opening = float(old.get("exposure") or 0)
        closing = float(new.get("exposure") or 0)
        movement = "Added" if opening <= 1e-12 and closing > 1e-12 else (
            "Exited" if opening > 1e-12 and closing <= 1e-12 else "Continuing"
        )
        source = new if closing > 1e-12 else old
        movements.append({
            "key": key,
            "paper": source.get("paper") or "",
            "isin": source.get("isin") or "",
            "issuer": source.get("issuer") or "",
            "group": source.get("group") or "Others",
            "maturity": source.get("maturity") or "",
            "openingExposure": opening,
            "closingExposure": closing,
            "change": closing - opening,
            "status": movement,
            "openingFunds": len(old.get("funds") or ()),
            "currentFunds": len(new.get("funds") or ()),
        })

    # The engine compares current parent groups with the previous Sheet8 summary.
    # That avoids treating each historical issuer name from BASE as its own parent group.
    changes = [dict(row) for row in result.group_changes]
    added = sorted(result.added_groups, key=str.casefold)
    exited = sorted(result.exited_groups, key=str.casefold)
    return movements, changes, added, exited


def _issuer_group_movements(result: ExposureResult) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Compare issuer-level GROUP EXPOSURE labels from previous and current BASE."""
    previous: dict[str, dict[str, Any]] = {}
    current: dict[str, dict[str, Any]] = {}

    def add(target: dict[str, dict[str, Any]], row: Mapping[str, Any], *, current_period: bool) -> None:
        issuer = str(row.get("issuer") or "Needs Mapping").strip()
        key = normalize_text(issuer)
        category_key = normalize_text(row.get("category") or "")
        # Cash / portfolio-residual labels are not borrower groups and must not
        # be reported as added or exited issuers (e.g. Reverse Repo, TREPS).
        non_borrower_labels = {
            "cash", "cash equivalent", "cash and cash equivalent", "cash margin",
            "net current asset", "reverse repo", "tri party repo treps", "treps",
            "other assets", "other derivaties", "gold", "silver", "not disclosed",
        }
        if (not key or key in {"needs mapping", "not disclosed"}
                or key in non_borrower_labels
                or category_key in {"cash equivalent", "cash and cash equivalent", "not disclosed for papers"}):
            return
        item = target.setdefault(key, {
            "groupExposure": issuer, "parentGroup": row.get("group") or "Others",
            "exposure": 0.0, "papers": set(), "funds": set(),
        })
        amount = float(row.get("exposure") or 0)
        item["exposure"] += amount
        paper = str(row.get("paper") or "").strip()
        isin = str(row.get("isin") or "").strip().upper()
        if paper or isin:
            item["papers"].add(isin or paper.casefold())
        if row.get("fund"):
            item["funds"].add(str(row["fund"]))
        if current_period and row.get("group"):
            item["parentGroup"] = row.get("group")

    for row in result.opening_details:
        add(previous, row, current_period=False)
    for row in result.rows:
        add(current, asdict(row), current_period=True)

    movements: list[dict[str, Any]] = []
    for key in sorted(previous.keys() | current.keys()):
        old, new = previous.get(key, {}), current.get(key, {})
        opening = float(old.get("exposure") or 0)
        closing = float(new.get("exposure") or 0)
        if opening <= 1e-12 and closing <= 1e-12:
            continue
        status = "Added" if opening <= 1e-12 < closing else ("Exited" if closing <= 1e-12 < opening else "Continuing")
        delta = closing - opening
        trend = "Increased" if delta > 1e-9 else ("Reduced" if delta < -1e-9 else "Unchanged")
        source = new if closing > 1e-12 else old
        movements.append({
            "groupExposure": source.get("groupExposure") or "",
            "parentGroup": source.get("parentGroup") or "Others",
            "opening": opening, "closing": closing, "change": delta,
            "status": status, "trend": trend,
            "paperCount": len(source.get("papers") or ()),
            "fundCount": len(source.get("funds") or ()),
        })
    added = sorted((row["groupExposure"] for row in movements if row["status"] == "Added"), key=str.casefold)
    exited = sorted((row["groupExposure"] for row in movements if row["status"] == "Exited"), key=str.casefold)
    return movements, added, exited



def _rating_change_lookup(result: ExposureResult) -> dict[str, str]:
    """Map current security identity to prior-quarter -> current-quarter rating change."""
    def identity(row):
        isin = str(row.get("isin") or "").strip().upper()
        if isin:
            return f"isin:{isin}"
        return "paper:{}|issuer:{}|maturity:{}".format(
            normalize_text(row.get("paper")), normalize_text(row.get("issuer")),
            str(row.get("maturity") or "")[:10],
        )

    previous = {}
    for raw in result.opening_details:
        row = dict(raw)
        rating = str(row.get("reported_rating") or row.get("rating") or row.get("category") or "").strip()
        if rating:
            previous.setdefault(identity(row), set()).add(rating)

    changes = {}
    for item in result.rows:
        row = asdict(item)
        key = identity(row)
        old_ratings = previous.get(key, set())
        old = sorted(old_ratings, key=str.casefold)
        new = str(row.get("reported_rating") or row.get("rating") or row.get("category") or "").strip()
        changes[key] = f"{old[0]} -> {new}" if len(old) == 1 and new and old[0].casefold() != new.casefold() else "NO"
    return changes


def _sheet4_preview(result: ExposureResult) -> list[list[object]]:
    """Paper × fund details with the cumulative exposure for each paper.

    ISIN is the preferred security identity. When absent, security records use
    paper + issuer + maturity; issuer-only matrix records are grouped by issuer
    and remain explicitly marked as issuer-level in the detail payload.
    """
    output: list[list[object]] = [[
        "GROUP EXPOSURE (Issuer)", "Parent Group", "Paper Name", "ISIN",
        "Fund Name as per Broker", "Broker", "Paper Maturity",
        "% of Holding (Fund Level)", "Prop Mkt Value (Cr)",
        "Cumulative Paper Exposure (Cr)", "Fund Count", "Rating Change",
    ]]

    def identity(row):
        if row.isin and row.isin.strip():
            return f"isin:{row.isin.strip().upper()}"
        paper = normalize_text(row.paper)
        issuer = normalize_text(row.issuer)
        maturity = row.maturity.isoformat() if row.maturity else ""
        if row.position_level == "issuer":
            return f"issuer:{issuer or paper}"
        return f"paper:{paper}|issuer:{issuer}|maturity:{maturity}"

    totals: dict[str, float] = {}
    funds: dict[str, set[str]] = {}
    for row in result.rows:
        key = identity(row)
        totals[key] = totals.get(key, 0.0) + float(row.exposure or 0)
        funds.setdefault(key, set()).add(row.fund)

    rating_changes = _rating_change_lookup(result)
    for row in sorted(result.rows, key=lambda item: (
        (item.group or "Others").casefold(), (item.issuer or "Needs Mapping").casefold(),
        item.paper.casefold(), item.fund.casefold()
    )):
        key = identity(row)
        output.append([
            row.issuer or "Needs Mapping", row.group or "Others", row.paper, row.isin or "",
            row.source_scheme or row.fund, row.broker or "",
            row.maturity.isoformat() if row.maturity else "", row.allocation * 100,
            row.exposure, totals[key], len(funds[key]), rating_changes.get(key, "NO"),
        ])
    return output


def _sheet8_preview(result: ExposureResult, added_groups: list[str], exited_groups: list[str]) -> list[list[object]]:
    rows: list[list[object]] = [[
        "Group", "Exposure", "Percentage", "Rating", "Paper Count", "Fund Count", "Added", "Exited"
    ]]
    groups = list(result.groups)
    specials = list(special_category_display_groups(result))
    count = max(len(groups), len(added_groups), len(exited_groups))
    for index in range(count):
        group = groups[index] if index < len(groups) else None
        rows.append([
            group.group if group else None,
            group.exposure if group else None,
            group.percentage if group else None,
            group.rating if group else None,
            group.paper_count if group else None,
            group.fund_count if group else None,
            added_groups[index] if index < len(added_groups) else None,
            exited_groups[index] if index < len(exited_groups) else None,
        ])
    for item in specials:
        rows.append([None, item["exposure"], item["percentage"], item["rating"],
                     item["paper_count"], item["fund_count"], None, None])
    rows.append(["Grand Total", result.total_exposure, 1 if result.total_exposure else 0, None, None, None, None, None])
    return rows


def _payload(result: ExposureResult) -> dict[str, object]:
    paper_mappings: dict[str, dict[str, str]] = {}
    for row in result.rows:
        if row.exposure <= 0:
            continue
        key = row.isin or row.paper.casefold()
        paper_mappings.setdefault(key, {"paper": row.paper, "group": row.group})

    public_kpis = dict(result.kpis)
    public_kpis["mappingGapCount"] = result.kpis["reviewActionCount"]
    public_gaps = [asdict(row) for row in result.mapping_gaps]
    public_gaps.extend(
        {
            "paper": f"Fund: {fund}", "issuer": "", "maturity": "", "rating": "",
            "suggested_group": "",
            "reason": (
                "This mutual fund holding could not be linked to a matching fund in the broker's "
                "portfolio file, so its underlying bonds are unknown. The value is excluded until "
                "current broker portfolio data is supplied."
            ),
        }
        for fund in result.unmatched_funds
    )
    paper_movements, group_changes, added_groups, exited_groups = _quarterly_movements(result)
    issuer_group_changes, added_issuer_groups, exited_issuer_groups = _issuer_group_movements(result)
    rating_changes = _rating_change_lookup(result)
    detail_rows = [{
        **asdict(row),
        "rating_change": rating_changes.get((f"isin:{str(row.isin).strip().upper()}" if row.isin else "paper:{}|issuer:{}|maturity:{}".format(normalize_text(row.paper), normalize_text(row.issuer), str(row.maturity or "")[:10])), "NO"),
        "movement_key": _movement_identity(row.group),
        "limit_category": limit_bucket(row.category, result.metadata.get("limitPolicy", "standard")),
    } for row in result.rows]
    detail_rows = _jsonable(detail_rows)
    payload = {
        "metadata": _jsonable(result.metadata),
        "categoryLimits": dict(CATEGORY_LIMITS),
        "policyLimits": _jsonable(result.metadata.get("effectiveCategoryLimits", CATEGORY_LIMITS)),
        "limitCategories": [_jsonable(row) for row in (result.limit_categories or result.categories)],
        "metrics": _jsonable(result.metrics),
        "kpis": _jsonable(public_kpis),
        "automation": {
            "fundAllocationCount": len(result.fund_allocations),
            "paperGroupMappingCount": len(paper_mappings),
            "previousGroupCount": result.metadata.get("baselineGroupCount", 0),
            "issuerMappingCount": len({row.issuer for row in result.rows}),
            "categoryLimitCount": len(result.metadata.get("effectiveCategoryLimits", CATEGORY_LIMITS)),
            "autoClassifiedCount": sum(1 for row in result.rows if row.mapping_source not in {"review", "allocation-residual"}),
            "mappingGapCount": len(public_gaps),
            "unmatchedFundCount": len(result.unmatched_funds),
            "unmatchedExposure": result.kpis["unmatchedExposure"],
            "unmatchedFunds": list(result.unmatched_funds),
            "fallbackFundCount": result.kpis["fallbackFundCount"],
            "fallbackExposure": result.kpis["fallbackExposure"],
            "staleProfileFundCount": result.kpis["staleProfileFundCount"],
            "configMode": "previous-quarter-memory-plus-approved-review",
        },
        "fundAllocations": dict(result.fund_allocations),
        "paperGroupMappings": list(paper_mappings.values()),
        "mappingGaps": public_gaps,
        "reviewIssues": [asdict(issue) for issue in result.review_issues],
        "topGroups": [
            {
                "group": row.group, "exposure": row.exposure, "percentage": row.percentage,
                "rating": row.rating, "category": row.category,
                "paperCount": row.paper_count, "fundCount": row.fund_count,
            }
            for row in result.groups
        ] + [
            {
                "group": item["group"], "exposure": item["exposure"], "percentage": item["percentage"],
                "rating": item["rating"], "paperCount": item["paper_count"],
                "fundCount": item["fund_count"], "category": item.get("category") or item["rating"],
                "special": True,
            }
            for item in special_category_display_groups(result)
        ],
        "categories": [_jsonable(row) for row in result.categories],
        "detailRows": detail_rows,
        "basePreview": detail_rows,
        "breaches": [_jsonable(row) for row in result.breaches],
        "sheet4Preview": _sheet4_preview(result),
        "sheet8Preview": _sheet8_preview(result, added_groups, exited_groups),
        "sheet8DetailPreview": [[row["category"], row["exposure"], row["percentage"]] for row in result.categories],
        "sheet8LimitPreview": [["Category", "Exposure", "Percentage", "Limit", "Status"]] + [
            [row.get("category"), row.get("exposure"), row.get("percentage"), row.get("limit"),
             "Breach" if row.get("breached") else ("Within" if row.get("limit") is not None else "No limit")]
            for row in result.limit_categories
        ],
        "groupChanges": group_changes,
        "paperMovements": paper_movements,
        "openingDetails": _jsonable(result.opening_details),
        "brokerComparison": _jsonable(result.broker_comparison),
        "addedGroups": added_groups,
        "exitedGroups": exited_groups,
        "issuerGroupChanges": issuer_group_changes,
        "addedIssuerGroups": added_issuer_groups,
        "exitedIssuerGroups": exited_issuer_groups,
    }
    return _jsonable(payload)
