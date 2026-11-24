from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime, timezone
from math import isfinite
import calendar
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from openpyxl import load_workbook

from apps.debt_tracker.core.engine import CATEGORY_LIMITS, generate_exposure, match_funds
from apps.debt_tracker.core.policies import evaluate_limits
from apps.debt_tracker.core.consolidation import resolve_provider_positions
from apps.debt_tracker.core.normalization import group_identity, normalize_fund_name, normalize_text
from apps.debt_tracker.core.parsers import parse_brokers, parse_holdings, parse_master, parse_scheme_crosswalk
from apps.debt_tracker.core.presentation import _payload
from apps.debt_tracker.report_builder import build_template_report
from ii_platform.storage.factory import get_object_storage

LOGGER = logging.getLogger("debt-tracker")
MAX_FILE_BYTES = 30 * 1024 * 1024
MAX_TOTAL_BYTES = 76 * 1024 * 1024
MAX_BROKER_FILES = 12
ALLOWED_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".xltm"}



def detect_holdings_report_date(path: str | Path) -> date | None:
    """Read the MProfit 'as on' date from the portfolio summary header."""
    workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    try:
        pattern = re.compile(r"as\s+on\s+([0-3]?\d)[/-]([01]?\d)[/-](20\d{2})", re.IGNORECASE)
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 25),
                                       min_col=1, max_col=min(sheet.max_column, 12), values_only=True):
                for value in row:
                    if not isinstance(value, str):
                        continue
                    match = pattern.search(value.strip())
                    if match:
                        day, month, year = map(int, match.groups())
                        return date(year, month, day)
    finally:
        workbook.close()
    return None


def previous_quarter_end(value: date) -> date:
    """Return the immediately preceding calendar-quarter end."""
    quarter_month = ((value.month - 1) // 3) * 3 + 1
    prior_month = quarter_month - 1 or 12
    prior_year = value.year if quarter_month > 1 else value.year - 1
    return date(prior_year, prior_month, calendar.monthrange(prior_year, prior_month)[1])

def safe_filename(value: str) -> str:
    name = Path(value or "workbook.xlsx").name
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name[:160] or "workbook.xlsx"


def validate_upload(filename: str, content: bytes) -> None:
    if Path(filename).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError(f"{filename}: only Excel workbooks (.xlsx/.xlsm) are supported.")
    if not content:
        raise ValueError(f"{filename}: uploaded workbook is empty.")
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"{filename}: workbook exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.")
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True, keep_links=False)
        try:
            if not wb.sheetnames:
                raise ValueError("workbook has no worksheets")
        finally:
            wb.close()
    except Exception as exc:
        raise ValueError(f"{filename}: file is not a readable Excel workbook.") from exc


def _fund_suggestions(holdings, positions, approved_aliases):
    schemes = sorted({p.scheme for p in positions}, key=str.casefold)
    output = []
    for holding in holdings:
        holding_key = normalize_fund_name(holding.fund_name)
        candidates = []
        for scheme in schemes:
            scheme_key = normalize_fund_name(scheme)
            if not holding_key or not scheme_key:
                continue
            a, b = set(holding_key.split()), set(scheme_key.split())
            jaccard = len(a & b) / len(a | b) if a or b else 0
            score = 0.60 * jaccard + 0.40 * SequenceMatcher(None, holding_key, scheme_key).ratio()
            if holding_key == scheme_key:
                score = 1.0
            candidates.append((score, scheme))
        candidates.sort(reverse=True)
        approved_scheme = next((broker for broker, portfolio in approved_aliases.items()
                                if normalize_fund_name(portfolio) == holding_key), None)
        for score, scheme in candidates[:3]:
            is_approved = bool(approved_scheme and normalize_fund_name(approved_scheme) == normalize_fund_name(scheme))
            output.append({
                "portfolio_fund": holding.fund_name,
                "broker_scheme": scheme,
                "confidence": round(score, 4),
                "approved": is_approved,
                "suggestion": "approved" if is_approved else ("high" if score >= 0.88 else "review"),
            })
    return output


def _incomplete_allocations(positions):
    totals = defaultdict(float)
    counts = defaultdict(int)
    for position in positions:
        totals[position.scheme] += position.allocation
        counts[position.scheme] += 1
    return [
        {"scheme": name, "allocation_total_pct": round(total * 100, 4), "missing_pct": round(max(0, 1-total) * 100, 4), "positions": counts[name],
         "action": "Once reviewed, confirm the broker extract includes all holdings. The missing balance is kept separately as Not Disclosed; known paper allocations are not scaled up."}
        for name, total in sorted(totals.items()) if abs(total - 1) > 0.001
    ]


def _apply_fund_aliases(positions, aliases):
    from dataclasses import replace
    normalized = {normalize_fund_name(key): value for key, value in aliases.items()}
    return tuple(replace(item, scheme=normalized.get(normalize_fund_name(item.scheme), item.scheme)) for item in positions)


def run_generation(*, mapping_path: Path, holdings_path: Path, broker_paths: list[Path], report_date: date,
                   baseline_date: date | None, base_sheet: str | None, estimate_bharat_fof: bool,
                   limit_policy: str, fund_aliases: dict[str, str], group_mappings: list[dict],
                   output_path: Path):
    master = parse_master(mapping_path, base_sheet=base_sheet, source_name=mapping_path.name)
    if baseline_date is None:
        baseline_date = master.snapshot_date or previous_quarter_end(report_date)
    holdings = parse_holdings(holdings_path, source_name=holdings_path.name)
    raw_positions = parse_brokers(broker_paths, source_names=[p.name for p in broker_paths])
    scheme_crosswalk = parse_scheme_crosswalk(broker_paths)
    positions, source_selections, broker_reconciliation, source_summary = resolve_provider_positions(
        holdings, raw_positions, approved_aliases=fund_aliases, scheme_crosswalk=scheme_crosswalk
    )
    # Apply the historical fund maturity where a broker format does not carry it.
    # This is metadata enrichment only; it does not change the exposure calculation.
    from dataclasses import replace
    enriched_positions = []
    for position in positions:
        profile = master.fund_profiles.get(normalize_fund_name(position.scheme), ())
        historical_fund_maturity = next((item.fund_maturity for item in profile if item.fund_maturity), None)
        enriched_positions.append(replace(
            position, fund_maturity=position.fund_maturity or historical_fund_maturity
        ))
    positions = tuple(enriched_positions)
    allocation_exceptions = _incomplete_allocations(positions)
    # Only the selected provider feed for each canonical MProfit fund enters the
    # financial engine. Alternative provider files remain visible in reconciliation.
    direct_matches, direct_unmatched = match_funds(holdings, positions)
    LOGGER.info(
        "DEBT_TRACKER_FUND_RECONCILIATION portfolio_funds=%d broker_schemes=%d "
        "direct_matches=%d direct_unmatched=%d approved_aliases=%d",
        len(holdings), len({item.scheme for item in positions}), len(direct_matches),
        len(direct_unmatched), len(fund_aliases),
    )

    issuer_overrides = []
    for mapping in group_mappings:
        if mapping["mapping_type"] == "issuer_group":
            issuer_overrides.append({"issuer_key": mapping["source_key"], "group": mapping["target_key"]})
        elif mapping["mapping_type"] == "security_group":
            # Security decisions are scoped to this report date and ISIN.
            issuer_overrides.append({"issuer_key": f"isin {mapping['source_key']} {report_date.isoformat()}", "group": mapping["target_key"]})

    result = generate_exposure(
        master, holdings, positions,
        reference_date=report_date,
        baseline_date=baseline_date,
        estimate_bharat_fof=estimate_bharat_fof,
        limit_policy=limit_policy,
        issuer_overrides=issuer_overrides,
    )
    report = _payload(result)
    report.setdefault("metadata", {})["reportDate"] = report_date.isoformat()
    report["metadata"]["baselineDate"] = baseline_date.isoformat() if baseline_date else None
    report["metadata"]["mappingReportDate"] = master.snapshot_date.isoformat() if master.snapshot_date else None
    report["mappingSuggestions"] = _fund_suggestions(holdings, raw_positions, fund_aliases)
    report["brokerReconciliation"] = broker_reconciliation
    report["brokerReconciliationSummary"] = source_summary
    report["providerSelections"] = source_selections
    detail_rows = report.get("detailRows", [])
    classification_counts = defaultdict(int)
    unresolved_rows = []
    for item in detail_rows:
        classification_counts[str(item.get("mapping_source") or "unknown")] += 1
        if item.get("mapping_source") in {"review", "group-conflict"}:
            unresolved_rows.append(item)
    unresolved_issuer_keys = {
        normalize_text(item.get("issuer") or item.get("paper") or "")
        for item in unresolved_rows
        if item.get("issuer") or item.get("paper")
    }
    grouping_quality = {
        "uniqueIssuerCount": len({normalize_text(item.get("issuer") or "") for item in detail_rows if item.get("issuer")}),
        "classifiedPositionCount": len(detail_rows) - len(unresolved_rows),
        "unresolvedPositionCount": len(unresolved_rows),
        "unresolvedIssuerCount": len(unresolved_issuer_keys),
        "unresolvedExposureCr": sum(float(item.get("exposure") or 0) for item in unresolved_rows),
        "classificationSourceCounts": dict(classification_counts),
    }
    report["dataQuality"] = {
        "allocationExceptions": allocation_exceptions,
        "sourcePositionCount": len(raw_positions),
        "selectedPositionCount": len(positions),
        "portfolioFundCount": len(holdings),
        "brokerSchemeCount": len({p.scheme for p in raw_positions}),
        "directMatchedFundCount": len(direct_matches),
        "directUnmatchedFundCount": len(direct_unmatched),
        "directUnmatchedFunds": list(direct_unmatched),
        "providerSourceSummary": source_summary,
        "providerSelections": source_selections,
        "schemeCrosswalkCount": len(scheme_crosswalk),
        "alternativeProviderCount": sum(int(row.get("alternativeProviderCount") or 0) for row in source_selections),
        "brokerReconciliationRowCount": len(broker_reconciliation),
        "grouping": grouping_quality,
        "finalDetailRowCount": len(report.get("detailRows", [])),
        "finalUnmatchedFundCount": report.get("automation", {}).get("unmatchedFundCount", 0),
    }
    LOGGER.info(
        "DEBT_TRACKER_REPORT_RESULT detail_rows=%d sheet4_rows=%d groups=%d "
        "review_issues=%d final_unmatched_funds=%d",
        len(report.get("detailRows", [])), max(0, len(report.get("sheet4Preview", [])) - 1),
        len(report.get("topGroups", [])), len(report.get("reviewIssues", [])),
        report.get("automation", {}).get("unmatchedFundCount", 0),
    )
    if holdings and not report.get("detailRows"):
        raise ValueError(
            "Debt Tracker generated zero exposure rows despite non-empty portfolio and broker inputs. "
            "The report was not saved as complete. Check fund reconciliation and uploaded workbook roles."
        )
    report["automation"]["allocationExceptionCount"] = len(allocation_exceptions)
    report["dataQuality"]["allocationExceptionCount"] = len(allocation_exceptions)
    build_template_report(mapping_path, result, output_path, report_date=report_date, report=report)
    return report, allocation_exceptions, hashlib.sha256(output_path.read_bytes()).hexdigest()


def serialize_job(job):
    return {
        "id": job.id, "status": job.status, "input_files": job.input_files or {},
        "error": job.error_message, "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _edited_number(value, field_name: str, *, allow_blank: bool = False):
    if value is None or value == "":
        if allow_blank:
            return None
        raise ValueError(f"{field_name} is required and must be numeric.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric.") from exc
    if not isfinite(result):
        raise ValueError(f"{field_name} must be a finite number.")
    return result


def _edited_date(value, field_name: str):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD.") from exc


def _group_by_category(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[str(row.get("category") or "Others").strip() or "Others"].append(row)
    return grouped


def _refresh_edited_report(report: dict) -> None:
    """Rebuild BASE-derived report summaries after user edits to the canonical ledger."""
    rows = report.get("detailRows") or []
    total = sum(float(row.get("exposure") or 0) for row in rows)
    group_members = defaultdict(list)
    category_totals = defaultdict(float)
    rating_totals = defaultdict(float)
    for row in rows:
        group = str(row.get("group") or "Others").strip() or "Others"
        category = str(row.get("category") or "Others").strip() or "Others"
        group_members[group].append(row)
        category_totals[category] += float(row.get("exposure") or 0)
        rating = str(row.get("reported_rating") or row.get("rating") or category).strip() or "Unrated / Special"
        rating_totals[rating] += float(row.get("exposure") or 0)

    group_rows = []
    special_rows = []
    for group, members in group_members.items():
        exposure = sum(float(row.get("exposure") or 0) for row in members)
        categories = defaultdict(float)
        ratings = set()
        papers, funds = set(), set()
        for row in members:
            categories[str(row.get("category") or "Others").strip() or "Others"] += float(row.get("exposure") or 0)
            rating = str(row.get("reported_rating") or row.get("rating") or "").strip()
            if rating:
                ratings.add(rating)
            paper_key = str(row.get("isin") or "").strip().upper() or (
                normalize_text(row.get("paper")) + "|" + normalize_text(row.get("issuer"))
                + "|" + str(row.get("maturity") or "")[:10]
            )
            papers.add(paper_key)
            funds.add(normalize_fund_name(row.get("fund") or row.get("source_scheme") or ""))
        primary_category = max(categories, key=categories.get) if categories else "Others"
        record = {
            "group": group, "exposure": exposure, "percentage": exposure / total if total else 0.0,
            "rating": " / ".join(sorted(ratings, key=str.casefold)), "category": primary_category,
            "paperCount": len(papers), "fundCount": len({fund for fund in funds if fund}),
        }
        if group.casefold() == "others":
            for category, members_in_category in _group_by_category(members).items():
                category_exposure = sum(float(row.get("exposure") or 0) for row in members_in_category)
                special_rows.append({
                    "group": category, "exposure": category_exposure,
                    "percentage": category_exposure / total if total else 0.0,
                    "rating": category, "category": category,
                    "paperCount": len({str(row.get("isin") or row.get("paper") or "").casefold() for row in members_in_category}),
                    "fundCount": len({normalize_fund_name(row.get("fund") or row.get("source_scheme") or "") for row in members_in_category}),
                    "special": True,
                })
        else:
            group_rows.append(record)

    group_rows.sort(key=lambda row: (-row["exposure"], row["group"].casefold()))
    special_rows.sort(key=lambda row: (-row["exposure"], row["group"].casefold()))
    category_rows = [
        {"category": category, "exposure": exposure,
         "percentage": exposure / total if total else 0.0,
         "limit": CATEGORY_LIMITS.get(category),
         "breached": bool(total and CATEGORY_LIMITS.get(category) is not None
                          and exposure / total > CATEGORY_LIMITS[category] + 1e-12)}
        for category, exposure in sorted(category_totals.items(), key=lambda item: -item[1])
    ]
    limit_policy = (report.get("metadata") or {}).get("limitPolicy", "standard")
    limit_rows = [dict(item) for item in evaluate_limits(category_rows, limit_policy, CATEGORY_LIMITS)]

    # Movement calculations are anchored to the report's saved opening snapshot.
    previous_group_totals = defaultdict(float)
    current_group_totals = defaultdict(float)
    previous_displays, current_displays = {}, {}
    for row in report.get("openingDetails") or []:
        if row.get("movement_eligible"):
            group = str(row.get("group") or "").strip()
            key = group_identity(group)
            if key:
                previous_group_totals[key] += float(row.get("exposure") or 0)
                previous_displays.setdefault(key, group)
    for row in rows:
        eligible = row.get("movement_eligible")
        if eligible is None:
            eligible = str(row.get("group") or "").casefold() not in {"", "others"} and not str(row.get("category") or "").casefold().startswith("cash")
        if eligible:
            group = str(row.get("group") or "").strip()
            key = group_identity(group)
            if key:
                current_group_totals[key] += float(row.get("exposure") or 0)
                current_displays.setdefault(key, group)
    parent_changes = []
    for key in sorted(previous_displays.keys() | current_displays.keys()) if report.get("openingDetails") else []:
        opening = previous_group_totals[key]
        closing = current_group_totals[key]
        delta = closing - opening
        membership = "Added" if opening <= 1e-12 < closing else ("Exited" if closing <= 1e-12 < opening else "Continuing")
        trend = "Increased" if delta > 1e-9 else ("Reduced" if delta < -1e-9 else "Unchanged")
        parent_changes.append({
            "key": key, "group": current_displays.get(key) or previous_displays.get(key),
            "opening": opening, "closing": closing, "change": delta,
            "membershipStatus": membership, "status": trend, "trend": trend,
        })
    added_groups = sorted((row["group"] for row in parent_changes if row["membershipStatus"] == "Added"), key=str.casefold)
    exited_groups = sorted((row["group"] for row in parent_changes if row["membershipStatus"] == "Exited"), key=str.casefold)

    # Issuer level movements are refreshed too, so a changed issuer cell is reflected
    # consistently in the Overview movement table.
    def issuer_totals(source_rows):
        result = {}
        for row in source_rows:
            issuer = str(row.get("issuer") or "Needs Mapping").strip()
            issuer_key = normalize_text(issuer)
            if not issuer_key or issuer_key in {"needs mapping", "not disclosed", "cash", "treps", "reverse repo"}:
                continue
            item = result.setdefault(issuer_key, {
                "groupExposure": issuer, "parentGroup": row.get("group") or "Others",
                "exposure": 0.0, "papers": set(), "funds": set(),
            })
            item["exposure"] += float(row.get("exposure") or 0)
            item["papers"].add(str(row.get("isin") or row.get("paper") or "").casefold())
            item["funds"].add(str(row.get("fund") or row.get("source_scheme") or ""))
        return result
    previous_issuers = issuer_totals(report.get("openingDetails") or [])
    current_issuers = issuer_totals(rows)
    issuer_changes = []
    for key in sorted(previous_issuers.keys() | current_issuers.keys()):
        old, new = previous_issuers.get(key, {}), current_issuers.get(key, {})
        opening, closing = float(old.get("exposure") or 0), float(new.get("exposure") or 0)
        delta = closing - opening
        membership = "Added" if opening <= 1e-12 < closing else ("Exited" if closing <= 1e-12 < opening else "Continuing")
        issuer_changes.append({
            "groupExposure": (new or old).get("groupExposure", ""),
            "parentGroup": (new or old).get("parentGroup", "Others"),
            "opening": opening, "closing": closing, "change": delta,
            "status": membership,
            "trend": "Increased" if delta > 1e-9 else ("Reduced" if delta < -1e-9 else "Unchanged"),
            "paperCount": len((new or old).get("papers") or ()),
            "fundCount": len((new or old).get("funds") or ()),
        })

    headers = ["Group", "Exposure", "Percentage", "Rating", "Paper Count", "Fund Count", "Added", "Exited"]
    sheet8_rows = [headers]
    for index in range(max(len(group_rows), len(added_groups), len(exited_groups))):
        group = group_rows[index] if index < len(group_rows) else {}
        sheet8_rows.append([group.get("group"), group.get("exposure"), group.get("percentage"),
                            group.get("rating"), group.get("paperCount"), group.get("fundCount"),
                            added_groups[index] if index < len(added_groups) else None,
                            exited_groups[index] if index < len(exited_groups) else None])
    for item in special_rows:
        sheet8_rows.append([None, item["exposure"], item["percentage"], item["rating"],
                            item["paperCount"], item["fundCount"], None, None])
    sheet8_rows.append(["Grand Total", total, 1 if total else 0, None, None, None, None, None])
    sheet8_limits = [["Category", "Exposure", "Percentage", "Limit", "Status"]]
    sheet8_limits.extend([[item["category"], item["exposure"], item["percentage"], item["limit"],
                           "Breach" if item["breached"] else ("Within" if item["limit"] is not None else "No limit")]
                          for item in limit_rows])

    # Refresh detailed BASE-to-Sheet4 projection and cumulative security totals.
    cumulative, funds_by_security = defaultdict(float), defaultdict(set)
    for row in rows:
        key = str(row.get("isin") or "").strip().upper() or (
            f"{normalize_text(row.get('paper'))}|{normalize_text(row.get('issuer'))}|{str(row.get('maturity') or '')[:10]}"
        )
        cumulative[key] += float(row.get("exposure") or 0)
        funds_by_security[key].add(str(row.get("fund") or row.get("source_scheme") or ""))
    sheet4 = [["GROUP EXPOSURE (Issuer)", "Parent Group", "Paper Name", "ISIN",
               "Fund Name as per Broker", "Broker", "Paper Maturity", "% of Holding (Fund Level)",
               "Prop Mkt Value (Cr)", "Cumulative Paper Exposure (Cr)", "Fund Count", "Rating Change"]]
    ordered_details = sorted(rows, key=lambda row: (str(row.get("group") or "").casefold(),
                              str(row.get("issuer") or "").casefold(),
                              str(row.get("paper") or "").casefold(),
                              str(row.get("fund") or "").casefold()))
    for row in ordered_details:
        key = str(row.get("isin") or "").strip().upper() or (
            f"{normalize_text(row.get('paper'))}|{normalize_text(row.get('issuer'))}|{str(row.get('maturity') or '')[:10]}"
        )
        sheet4.append([row.get("issuer") or "Needs Mapping", row.get("group") or "Others",
                       row.get("paper") or "", row.get("isin") or "", row.get("source_scheme") or row.get("fund") or "",
                       row.get("broker") or "", row.get("maturity") or "",
                       float(row.get("allocation") or 0) * 100, float(row.get("exposure") or 0),
                       cumulative[key], len(funds_by_security[key]), row.get("rating_change") or "NO"])

    report["topGroups"] = group_rows + special_rows
    report["categories"] = category_rows
    report["limitCategories"] = limit_rows
    report["sheet8Preview"] = sheet8_rows
    report["sheet8LimitPreview"] = sheet8_limits
    report["sheet4Preview"] = sheet4
    report["basePreview"] = rows
    report["groupChanges"] = parent_changes
    report["addedGroups"] = added_groups
    report["exitedGroups"] = exited_groups
    report["issuerGroupChanges"] = issuer_changes
    report["addedIssuerGroups"] = sorted((row["groupExposure"] for row in issuer_changes if row["status"] == "Added"), key=str.casefold)
    report["exitedIssuerGroups"] = sorted((row["groupExposure"] for row in issuer_changes if row["status"] == "Exited"), key=str.casefold)
    report["quarterlySummary"] = {
        "parentGroups": {
            "rows": parent_changes, "addedExposure": sum(row["closing"] for row in parent_changes if row["membershipStatus"] == "Added"),
            "exitedExposure": sum(row["opening"] for row in parent_changes if row["membershipStatus"] == "Exited"),
            "grossReduction": sum(abs(row["change"]) for row in parent_changes if row["membershipStatus"] == "Continuing" and row["status"] == "Reduced"),
            "currentCount": sum(1 for row in parent_changes if row["closing"] > 1e-12),
            "continuingCount": sum(1 for row in parent_changes if row["membershipStatus"] == "Continuing"),
            "increasedCount": sum(1 for row in parent_changes if row["status"] == "Increased"),
            "reducedCount": sum(1 for row in parent_changes if row["status"] == "Reduced"),
        },
        "issuerGroups": {"rows": issuer_changes},
    }
    kpis = report.setdefault("kpis", {})
    kpis.update({
        "totalExposure": total, "debtExposure": sum(float(row.get("exposure") or 0) for row in rows if row.get("category") != "Cash & Equivalent"),
        "cashExposure": sum(float(row.get("exposure") or 0) for row in rows if row.get("category") == "Cash & Equivalent"),
        "totalParentGroups": len(group_rows), "totalGroups": len({normalize_text(row.get("issuer")) for row in rows if row.get("issuer")}),
        "totalPapers": len({str(row.get("isin") or "").strip().upper() or normalize_text(row.get("paper")) for row in rows if row.get("exposure")}),
        "totalFunds": len({normalize_fund_name(row.get("fund") or row.get("source_scheme") or "") for row in rows if row.get("fund") or row.get("source_scheme")}),
        "baseRows": len(rows),
    })
    metrics = report.setdefault("metrics", {})
    base_metrics = metrics.setdefault("base", {})
    base_metrics["generated_rows"] = len(rows)
    base_metrics["generated_prop_mkt_value_total"] = total
    base_metrics["difference"] = total - float(base_metrics.get("reference_prop_mkt_value_total", total) or total)
    report["metadata"] = {**(report.get("metadata") or {}), "baseEditedAt": datetime.now(timezone.utc).isoformat()}


def save_edited_base(report: dict, workbook_bytes: bytes, submitted_rows: list[dict]) -> tuple[dict, bytes]:
    """Validate cell edits, refresh report summaries and update the stored workbook."""
    current = report.get("detailRows") or []
    if not current:
        raise ValueError("This report has no BASE rows to edit.")
    if len(submitted_rows) != len(current):
        raise ValueError("The BASE row count changed. Reload the report before saving edits.")
    if len(submitted_rows) > 100_000:
        raise ValueError("BASE contains too many rows to edit in one request.")

    allowed_text = ("fund", "source_scheme", "paper", "issuer", "broker", "asset_type", "rating", "reported_rating")
    updated = []
    for index, (original, submitted) in enumerate(zip(current, submitted_rows), 1):
        if not isinstance(submitted, dict):
            raise ValueError(f"BASE row {index} is invalid.")
        row = dict(original)
        for field_name in allowed_text:
            if field_name not in submitted:
                continue
            value = submitted[field_name]
            if value is None:
                value = ""
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError(f"BASE row {index}: {field_name} must be text.")
            value = str(value).strip()
            if len(value) > 512:
                raise ValueError(f"BASE row {index}: {field_name} exceeds 512 characters.")
            row[field_name] = value
        if not str(row.get("fund") or row.get("source_scheme") or "").strip():
            raise ValueError(f"BASE row {index}: Fund Name is required.")
        if not str(row.get("paper") or "").strip():
            raise ValueError(f"BASE row {index}: Paper Name is required.")
        row["fund"] = str(row.get("fund") or row.get("source_scheme") or "").strip()
        row["source_scheme"] = str(row.get("source_scheme") or row["fund"]).strip()
        row["maturity"] = _edited_date(submitted.get("maturity", row.get("maturity")), f"BASE row {index} Paper Maturity")
        row["fund_maturity"] = _edited_date(submitted.get("fund_maturity", row.get("fund_maturity")), f"BASE row {index} Fund Maturity")
        row["market_value_crore"] = _edited_number(submitted.get("market_value_crore", row.get("market_value_crore")), f"BASE row {index} Paper Amount", allow_blank=True)
        allocation = _edited_number(submitted.get("allocation", row.get("allocation")), f"BASE row {index} Holding")
        if not -1.005 <= allocation <= 1.005:
            raise ValueError(f"BASE row {index}: Holding must be between -100.5% and 100.5%.")
        row["allocation"] = allocation
        row["exposure"] = _edited_number(submitted.get("exposure", row.get("exposure")), f"BASE row {index} Exposure")
        classification = str(row.get("reported_rating") or row.get("rating") or row.get("category") or "").strip()
        if "category" in submitted:
            # BASE's Category column is the editable credit-rating label. Keep
            # detailRows.category (the engine's concentration-limit bucket) intact.
            category_value = submitted.get("category")
            if category_value is None:
                category_value = ""
            if not isinstance(category_value, (str, int, float)) or isinstance(category_value, bool):
                raise ValueError(f"BASE row {index}: category must be text.")
            classification = str(category_value).strip()
            if len(classification) > 512:
                raise ValueError(f"BASE row {index}: category exceeds 512 characters.")
            row["reported_rating"] = classification
            row["rating"] = classification
            row["category"] = str(original.get("category") or "Others")
        else:
            row.setdefault("category", classification or "Others")
        updated.append(row)

    report = dict(report)
    report["detailRows"] = updated
    _refresh_edited_report(report)
    edited_total = sum(float(item.get("exposure") or 0) for item in updated)

    from copy import copy
    from openpyxl import load_workbook
    stream = io.BytesIO(workbook_bytes)
    workbook = load_workbook(stream, data_only=False, keep_links=False)
    try:
        if "BASE" not in workbook.sheetnames:
            raise ValueError("The saved Excel workbook has no BASE sheet.")
        base = workbook["BASE"]
        headers = ["Fund Name as per Broker", "Paper Name", "GROUP EXPOSURE", "Paper Maturity",
                   "Fund Maturity", "Paper Amount in Cr", "% of Holding (Fund Level)",
                   "Broker", "ZCB / MLD / NCD", "Prop Mkt Value", "Category"]
        for col, value in enumerate(headers, 1):
            base.cell(1, col, value)
        # The editor does not add/delete rows, so cell styles and workbook formatting remain intact.
        if base.max_row - 1 != len(updated):
            raise ValueError("The BASE sheet row count does not match the saved report. Please regenerate the report before editing.")
        for row_index, item in enumerate(updated, 2):
            values = [item.get("source_scheme") or item.get("fund"), item.get("paper"), item.get("issuer") or "Needs Mapping",
                      date.fromisoformat(item["maturity"]) if item.get("maturity") else None,
                      date.fromisoformat(item["fund_maturity"]) if item.get("fund_maturity") else None,
                      item.get("market_value_crore"), float(item.get("allocation") or 0) * 100,
                      item.get("broker") or "", item.get("asset_type") or "", item.get("exposure"),
                      item.get("reported_rating") or item.get("rating") or item.get("category") or ""]
            for col, value in enumerate(values, 1):
                base.cell(row_index, col, value)
        base.auto_filter.ref = f"A1:K{base.max_row}"
        base.freeze_panes = "A2"

        # Refresh every downstream worksheet whose values are derived from BASE.
        # Preserve the workbook's first detail-row styles where possible.
        def reset_detail_sheet(sheet, header_row=1, first_data_row=2):
            style_row = first_data_row
            styles = []
            formats = []
            if sheet.max_row >= style_row:
                styles = [copy(sheet.cell(style_row, col)._style) for col in range(1, sheet.max_column + 1)]
                formats = [sheet.cell(style_row, col).number_format for col in range(1, sheet.max_column + 1)]
            if sheet.max_row >= first_data_row:
                sheet.delete_rows(first_data_row, sheet.max_row - first_data_row + 1)
            return styles, formats

        def append_styled(sheet, values, styles=None, formats=None):
            sheet.append(list(values))
            row_no = sheet.max_row
            if styles:
                for col, style in enumerate(styles, 1):
                    cell = sheet.cell(row_no, col)
                    if style is not None:
                        cell._style = copy(style)
                    if formats and col <= len(formats) and formats[col - 1]:
                        cell.number_format = formats[col - 1]

        # Sheet4 includes the paper × fund ledger followed by an issuer summary.
        if "Sheet4" in workbook.sheetnames:
            sheet4 = workbook["Sheet4"]
            style4, formats4 = reset_detail_sheet(sheet4, 1, 2)
            headers4 = report["sheet4Preview"][0]
            for col, value in enumerate(headers4, 1):
                sheet4.cell(1, col, value)
            for values in report["sheet4Preview"][1:]:
                append_styled(sheet4, values, style4, formats4)
            detail_last_row = sheet4.max_row
            sheet4.cell(detail_last_row + 3, 1, "ISSUER EXPOSURE SUMMARY")
            sheet4.cell(detail_last_row + 4, 1, "GROUP EXPOSURE (Issuer)")
            sheet4.cell(detail_last_row + 4, 2, "Total Exposure (Cr)")
            issuer_totals = defaultdict(float)
            for item in updated:
                issuer_totals[item.get("issuer") or "Needs Mapping"] += float(item.get("exposure") or 0)
            for offset, (issuer, amount) in enumerate(sorted(issuer_totals.items(), key=lambda pair: pair[0].casefold()), 5):
                sheet4.cell(detail_last_row + offset, 1, issuer)
                sheet4.cell(detail_last_row + offset, 2, amount).number_format = '#,##0;[Red](#,##0);-'
            total_row = detail_last_row + len(issuer_totals) + 5
            sheet4.cell(total_row, 1, "Grand Total")
            sheet4.cell(total_row, 2, edited_total).number_format = '#,##0;[Red](#,##0);-'
            sheet4.freeze_panes = "A2"
            sheet4.auto_filter.ref = f"A1:L{max(1, detail_last_row)}"

        # Sheet3: paper-level detail and rating values.
        if "Sheet3" in workbook.sheetnames:
            sheet3 = workbook["Sheet3"]
            style3, formats3 = reset_detail_sheet(sheet3, 2, 3)
            sheet3.cell(1, 1, "Sum of Prop Mkt Value")
            sheet3.cell(1, 2, edited_total)
            headers3 = ["GROUP EXPOSURE", "Paper Name", "Fund Name as per Broker", "Paper Maturity", "% of Holding (Fund Level)", "Rating", "Total"]
            for col, value in enumerate(headers3, 1):
                sheet3.cell(2, col, value)
            ordered = sorted(updated, key=lambda item: (
                str(item.get("group") or "Others").casefold(),
                str(item.get("paper") or "").casefold(),
                str(item.get("fund") or item.get("source_scheme") or "").casefold(),
            ))
            for item in ordered:
                append_styled(sheet3, [item.get("issuer") or "Needs Mapping", item.get("paper"),
                    item.get("source_scheme") or item.get("fund"),
                    date.fromisoformat(item["maturity"]) if item.get("maturity") else None,
                    float(item.get("allocation") or 0) * 100,
                    item.get("rating") or item.get("reported_rating") or "Unrated",
                    float(item.get("exposure") or 0)], style3, formats3)
            sheet3.freeze_panes = "A3"

        # Sheet2: fund-level broker amount and portfolio value reconciliation.
        if "Sheet2" in workbook.sheetnames:
            sheet2 = workbook["Sheet2"]
            style2, formats2 = reset_detail_sheet(sheet2, 2, 3)
            broker_by_fund = defaultdict(float)
            for item in updated:
                broker_by_fund[str(item.get("fund") or "Unmapped fund")] += float(item.get("market_value_crore") or 0)
            portfolio_by_fund = report.get("fundAllocations") or {}
            sheet2.cell(1, 1, sum(float(item.get("market_value_crore") or 0) for item in updated))
            sheet2.cell(1, 2, edited_total)
            headers2 = ["Broker Scheme / Portfolio Fund", "Broker Paper Amount (Cr)", "Portfolio Fund", "Broker Paper Amount (Cr)", "Current Value (Cr)", "Portfolio Fund", "Broker Paper Amount (Cr)", "Invested Value (Cr)"]
            for col, value in enumerate(headers2, 1):
                sheet2.cell(2, col, value)
            for fund in sorted(set(broker_by_fund) | set(portfolio_by_fund), key=str.casefold):
                broker_amount = broker_by_fund.get(fund, 0.0)
                portfolio_amount = float(portfolio_by_fund.get(fund) or 0)
                append_styled(sheet2, [fund, broker_amount, fund, broker_amount, portfolio_amount, fund, broker_amount, None], style2, formats2)
            sheet2.freeze_panes = "A3"

        # Sheet5: parent-group total and rating split.
        if "Sheet5" in workbook.sheetnames:
            sheet5 = workbook["Sheet5"]
            style5, formats5 = reset_detail_sheet(sheet5, 1, 2)
            headers5 = ["Row Labels", "Sum of Total", "", "GROUP EXPOSURE", "Rating", "Total"]
            for col, value in enumerate(headers5, 1):
                sheet5.cell(1, col, value)
            group_total = defaultdict(float)
            group_rating = defaultdict(lambda: defaultdict(float))
            for item in updated:
                group = str(item.get("group") or "Others")
                rating = str(item.get("rating") or item.get("reported_rating") or "Unrated")
                exposure = float(item.get("exposure") or 0)
                group_total[group] += exposure
                group_rating[group][rating] += exposure
            for group in sorted(group_total, key=str.casefold):
                append_styled(sheet5, [group, group_total[group], None, None, None, None], style5, formats5)
                for rating, amount in sorted(group_rating[group].items(), key=lambda pair: -pair[1]):
                    append_styled(sheet5, [None, None, None, group, rating, amount], style5, formats5)

        # Sheet6: issuer to parent-group mapping summary.
        if "Sheet6" in workbook.sheetnames:
            sheet6 = workbook["Sheet6"]
            style6, formats6 = reset_detail_sheet(sheet6, 1, 2)
            headers6 = ["Group", "Group as per MF", "Sum of Total", "Percentage", "Rating", "Bank / Non-Bank / Govt Owned", "Concat"]
            for col, value in enumerate(headers6, 1):
                sheet6.cell(1, col, value)
            issuer_totals = defaultdict(float)
            issuer_rating = {}
            for item in updated:
                unresolved = item.get("mapping_source") in {"review", "group-conflict"}
                group = "" if unresolved else str(item.get("group") or "Others")
                issuer = str(item.get("issuer") or "Needs Mapping")
                key = (group, issuer)
                issuer_totals[key] += float(item.get("exposure") or 0)
                issuer_rating[key] = item.get("rating") or item.get("reported_rating") or "Unrated"
            denominator = edited_total or 1.0
            for (group, issuer), exposure in sorted(issuer_totals.items(), key=lambda pair: (pair[0][0].casefold(), pair[0][1].casefold())):
                rating = issuer_rating[(group, issuer)]
                kind = "Bank" if "bank" in str(rating).lower() else ("Govt" if rating in {"SOV", "SDL", "Govt"} else "Non-Bank")
                append_styled(sheet6, [group, issuer, exposure, exposure / denominator, rating, kind, f"{rating} {kind}"], style6, formats6)

        # Sheet7: credit-category totals and portfolio shares.
        if "Sheet7" in workbook.sheetnames:
            sheet7 = workbook["Sheet7"]
            style7, formats7 = reset_detail_sheet(sheet7, 1, 2)
            headers7 = ["Row Labels", "Sum of Percentage", "Sum of Exposure", "Exposure", "Percentage", "", ""]
            for col, value in enumerate(headers7, 1):
                sheet7.cell(1, col, value)
            for item in report.get("categories") or []:
                values = [item.get("category"), item.get("percentage"), item.get("exposure"),
                          item.get("category"), item.get("exposure"), item.get("percentage"), None]
                append_styled(sheet7, values, style7, formats7)
        if "Sheet8" in workbook.sheetnames:
            sheet8 = workbook["Sheet8"]
            style_template = [copy(sheet8.cell(2, col)._style) for col in range(1, 9)] if sheet8.max_row >= 2 else []
            sheet8.delete_rows(2, max(0, sheet8.max_row - 1))
            for values in report["sheet8Preview"][1:]:
                sheet8.append(list(values))
                if style_template:
                    for col, style in enumerate(style_template, 1):
                        if style is not None:
                            sheet8.cell(sheet8.max_row, col)._style = copy(style)
            if report.get("sheet8LimitPreview"):
                sheet8.append([None] * 8)
                for values in report["sheet8LimitPreview"]:
                    sheet8.append(list(values))
            sheet8.auto_filter.ref = f"A1:H{max(1, len(report['sheet8Preview']))}"
        if "Sheet7" in workbook.sheetnames:
            sheet7 = workbook["Sheet7"]
            for row_index, item in enumerate(report.get("categories") or [], 2):
                values = [item.get("category"), item.get("percentage"), item.get("exposure"),
                          item.get("category"), item.get("exposure"), item.get("percentage")]
                for col, value in enumerate(values, 1):
                    sheet7.cell(row_index, col, value)
            if sheet7.max_row > len(report.get("categories") or []) + 1:
                for row in sheet7.iter_rows(min_row=len(report.get("categories") or []) + 2):
                    for cell in row:
                        cell.value = None
        workbook.calculation.fullCalcOnLoad = True
        output = io.BytesIO()
        workbook.save(output)
        updated_workbook = output.getvalue()
    finally:
        workbook.close()
    return report, updated_workbook
