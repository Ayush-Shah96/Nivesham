from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import calendar
import re

from sqlalchemy import desc, select

from .models import PMSMonthlyFile, PMSMonthlyHolding, PMSRegistry
from .normalization import is_equity_stock
from .parsers import pms_identity_key


REPORT_CHANGE_THRESHOLD_PCT = 10.0

# Requested workbook order. Identity keys are shared with the PMS parser so
# capitalization and known spelling variants do not change the report order.
PMS_WORKBOOK_ORDER = (
    ("ndpms", "NDPMS"),
    ("2p2", "2Point2 Capital"),
    ("sageonecore", "Sageone CORE"),
    ("360pipe", "360 One Pipe Fund"),
    ("sageonesmallcap", "Sageone Small"),
    ("molecule", "Molecule"),
    ("niveshaay", "Noveshaay"),
    ("91capital", "91 Capital"),
    ("persistence", "Persistence"),
    ("xponent", "Xponent"),
    ("bastion", "Bastion"),
    ("emkay", "EMkay"),
)
_PMS_ORDER_BY_KEY = {key: index for index, (key, _) in enumerate(PMS_WORKBOOK_ORDER)}
_PMS_DISPLAY_BY_KEY = {key: label for key, label in PMS_WORKBOOK_ORDER}


def _pms_order_key(name: str) -> tuple[int, str]:
    key = pms_identity_key(name)
    return (_PMS_ORDER_BY_KEY.get(key, len(PMS_WORKBOOK_ORDER)), name.upper())


def _display_pms_name(name: str) -> str:
    return _PMS_DISPLAY_BY_KEY.get(pms_identity_key(name), name)


def previous_month(month: str) -> str:
    year, mon = [int(part) for part in month.split("-")]
    return f"{year - 1:04d}-12" if mon == 1 else f"{year:04d}-{mon - 1:02d}"


def previous_quarter_end_month(month: str) -> str:
    """Return the immediately preceding calendar quarter-end month."""
    year, mon = [int(part) for part in month.split("-")]
    quarter_start = ((mon - 1) // 3) * 3 + 1
    return f"{year - 1:04d}-12" if quarter_start == 1 else f"{year:04d}-{quarter_start - 1:02d}"


def _quarter_end_months_before(month: str, *, years_back: int = 30) -> list[str]:
    """All earlier quarter-ends, newest first, for a safe historical fallback."""
    year, _ = [int(part) for part in month.split("-")]
    candidates = [f"{candidate_year:04d}-{quarter_month:02d}"
                  for candidate_year in range(year, year - years_back - 1, -1)
                  for quarter_month in (12, 9, 6, 3)
                  if f"{candidate_year:04d}-{quarter_month:02d}" < month]
    return sorted(candidates, reverse=True)


def last_day_label(month: str) -> str:
    year, mon = [int(part) for part in month.split("-")]
    day = calendar.monthrange(year, mon)[1]
    return f"{day:02d}-{calendar.month_abbr[mon]}-{year}"


def _identity(row: PMSMonthlyHolding) -> tuple[str, str]:
    # Portfolio comparison is intentionally name-to-name. The normalized security
    # name is the primary identity; ISIN is only a fallback for malformed rows
    # where a usable name is genuinely unavailable.
    name = re.sub(r"[^a-z0-9]+", "", (row.normalized_security_name or row.security_name).lower())
    if name:
        return ("NAME", name)
    if row.isin:
        return ("ISIN", row.isin.upper().replace(" ", ""))
    return ("NAME", "UNKNOWN")


def _row_dict(row: PMSMonthlyHolding | None) -> dict:
    if row is None:
        return {}
    return {
        "isin": row.isin,
        "security_name": row.security_name,
        "normalized_security_name": row.normalized_security_name,
        "quantity": float(row.quantity or 0.0),
        "measurement_type": row.measurement_type or "QUANTITY",
        "current_value": float(row.current_value) if row.current_value is not None else None,
        "pnl": float(row.pnl) if row.pnl is not None else None,
        "pnl_pct": float(row.pnl_pct) if row.pnl_pct is not None else None,
    }


def _diff_pct(previous: float, current: float) -> float | None:
    if abs(previous) < 1e-12:
        return 100.0 if abs(current) >= 1e-12 else 0.0
    return ((current - previous) / abs(previous)) * 100.0


def _status(previous: float, current: float, had_previous_row: bool) -> str:
    eps = 1e-12
    if not had_previous_row:
        return "NEW" if abs(current) > eps else "HELD"
    if abs(previous) <= eps and abs(current) > eps:
        return "BOUGHT"
    if abs(previous) > eps and abs(current) <= eps:
        return "EXITED"
    if current > previous + eps:
        return "BOUGHT"
    if current < previous - eps:
        return "SOLD"
    return "HELD"


async def _latest_previous_file(db, pms_id: int, comparison_month: str) -> PMSMonthlyFile | None:
    """Return the most recent successfully processed appraisal before this month.

    PMS reports are not guaranteed to arrive every month.  The comparison
    baseline is therefore the latest earlier report available for that PMS,
    whether that report is one month, one quarter, or longer before the upload.
    """
    return await db.scalar(
        select(PMSMonthlyFile)
        .where(
            PMSMonthlyFile.pms_id == pms_id,
            PMSMonthlyFile.comparison_month < comparison_month,
            PMSMonthlyFile.status == "processed",
            PMSMonthlyFile.is_current.is_(True),
        )
        .order_by(desc(PMSMonthlyFile.comparison_month), desc(PMSMonthlyFile.id))
        .limit(1)
    )


async def _latest_prior_processed_file(db, pms_id: int, comparison_month: str) -> PMSMonthlyFile | None:
    """Return any latest earlier processed file for lifecycle/history status only."""
    return await db.scalar(
        select(PMSMonthlyFile)
        .where(
            PMSMonthlyFile.pms_id == pms_id,
            PMSMonthlyFile.comparison_month < comparison_month,
            PMSMonthlyFile.status == "processed",
            PMSMonthlyFile.is_current.is_(True),
        )
        .order_by(desc(PMSMonthlyFile.comparison_month), desc(PMSMonthlyFile.id))
        .limit(1)
    )


async def _current_files(db, comparison_month: str) -> list[PMSMonthlyFile]:
    """Return successfully processed current snapshots for the selected month."""
    result = await db.scalars(
        select(PMSMonthlyFile)
        .where(
            PMSMonthlyFile.comparison_month == comparison_month,
            PMSMonthlyFile.status == "processed",
            PMSMonthlyFile.is_current.is_(True),
        )
        .order_by(PMSMonthlyFile.pms_id)
    )
    return list(result.all())


def _aggregate_rows(rows: list[PMSMonthlyHolding]) -> dict[tuple[str, str], PMSMonthlyHolding]:
    grouped: dict[tuple[str, str], list[PMSMonthlyHolding]] = defaultdict(list)
    for row in rows:
        grouped[_identity(row)].append(row)
    output: dict[tuple[str, str], PMSMonthlyHolding] = {}
    for identity, group in grouped.items():
        base = group[0]
        # Multiple lines for the same security are legitimate in some statements.
        # Aggregate only when their semantic measurement is the same.
        same_metric = len({(item.measurement_type or "QUANTITY").upper() for item in group}) == 1
        if len(group) == 1 or not same_metric:
            output[identity] = base
            continue
        base.quantity = sum(float(item.quantity or 0.0) for item in group)
        output[identity] = base
    return output


def _measurement_types(rows: list[PMSMonthlyHolding]) -> set[str]:
    return {(row.measurement_type or "QUANTITY").upper() for row in rows}


def _measurement_basis_change(
    previous_rows: list[PMSMonthlyHolding], current_rows: list[PMSMonthlyHolding]
) -> tuple[set[str], set[str]] | None:
    """Return the differing measurement sets when two snapshots are incomparable."""
    if not previous_rows or not current_rows:
        return None
    previous_types = _measurement_types(previous_rows)
    current_types = _measurement_types(current_rows)
    if previous_types != current_types:
        return previous_types, current_types
    return None


def _detail_rows(previous_rows: list[PMSMonthlyHolding], current_rows: list[PMSMonthlyHolding]) -> list[dict]:
    previous_by_id = _aggregate_rows(previous_rows)
    current_by_id = _aggregate_rows(current_rows)
    identities = set(previous_by_id) | set(current_by_id)
    output: list[dict] = []
    for identity in identities:
        prev = previous_by_id.get(identity)
        curr = current_by_id.get(identity)
        source = curr or prev
        previous_value = float(prev.quantity) if prev is not None else 0.0
        current_value = float(curr.quantity) if curr is not None else 0.0
        previous_metric = (prev.measurement_type or "QUANTITY").upper() if prev else None
        current_metric = (curr.measurement_type or "QUANTITY").upper() if curr else None
        metric_type = current_metric or previous_metric or "QUANTITY"
        comparable = prev is None or curr is None or previous_metric == current_metric
        if not comparable:
            status = "MEASUREMENT_CHANGED"
            change = None
            diff_pct = None
        else:
            status = _status(previous_value, current_value, prev is not None)
            change = current_value - previous_value
            diff_pct = _diff_pct(previous_value, current_value)
        output.append(
            {
                "isin": source.isin,
                "security_name": source.security_name,
                "previous": previous_value if prev is not None else None,
                "current": current_value if curr is not None else None,
                "diff": change,
                "diff_pct": diff_pct,
                "status": status,
                "measurement_type": metric_type,
                "previous_measurement_type": previous_metric,
                "current_measurement_type": current_metric,
                "metric_label": "Holding %" if metric_type == "PERCENTAGE" else "Quantity",
                "percentage_point_change": change if metric_type == "PERCENTAGE" and change is not None else None,
            }
        )
    return sorted(output, key=lambda row: (row["status"], -abs(row["diff_pct"] or 0), row["security_name"].upper()))


def _should_report_row(row: dict) -> bool:
    """Only surface material changes in the report; keep measurement warnings."""
    if row.get("status") == "MEASUREMENT_CHANGED":
        return True
    change_pct = row.get("diff_pct")
    if change_pct is None:
        return False
    try:
        return abs(float(change_pct)) >= REPORT_CHANGE_THRESHOLD_PCT
    except (TypeError, ValueError):
        return False


def _should_include_pms_detail(row: dict) -> bool:
    """Include every changed holding on an individual PMS worksheet."""
    status = row.get("status")
    if status in {"BASELINE", "NEW", "BOUGHT", "SOLD", "EXITED", "MEASUREMENT_CHANGED"}:
        return True
    change = row.get("diff")
    try:
        return change is not None and abs(float(change)) > 1e-12
    except (TypeError, ValueError):
        return False


def _summary_side(details: list[dict], bought: bool, pms_name: str = "") -> list[dict]:
    statuses = {"NEW", "BOUGHT"} if bought else {"SOLD", "EXITED"}
    pms_key = re.sub(r"[^a-z0-9]+", "", (pms_name or "").lower())
    rows = []
    for row in details:
        if row.get("status") not in statuses or row.get("diff_pct") is None:
            continue
        if "niveshaay" in pms_key:
            threshold = 1.0
        elif (row.get("measurement_type") or "QUANTITY").upper() == "PERCENTAGE":
            threshold = 20.0
        else:
            threshold = REPORT_CHANGE_THRESHOLD_PCT
        if abs(float(row["diff_pct"])) >= threshold:
            rows.append(row)
    rows.sort(key=lambda row: (-abs(row["diff_pct"] or 0.0), row["security_name"].upper()))
    return rows


def _baseline_side(details: list[dict]) -> list[dict]:
    """Return every holding in a PMS's first available report.

    These are opening positions, not purchases.  They are shown separately on
    the Summary sheet so a baseline-month report is useful without inventing
    buy/sell activity when no earlier appraisal exists.
    """
    rows = [
        row for row in details
        if row.get("status") == "BASELINE" and row.get("current") is not None
    ]
    return sorted(rows, key=lambda row: row.get("security_name", "").upper())


def _change_profile(details: list[dict]) -> tuple[float | None, int]:
    """Average absolute relative change for positions present in both snapshots.

    New/exited positions and measurement-incompatible values are excluded because
    there is no meaningful two-period relative change to average for those rows.
    """
    changes = [
        abs(float(row["diff_pct"]))
        for row in details
        if row.get("previous") is not None
        and row.get("current") is not None
        and row.get("status") != "MEASUREMENT_CHANGED"
        and row.get("diff_pct") is not None
    ]
    if not changes:
        return None, 0
    return sum(changes) / len(changes), len(changes)


def _change_profile_summary(item: dict) -> dict:
    return {
        "pms_name": item["pms_name"],
        "average_abs_change_pct": item.get("average_abs_change_pct"),
        "sample_count": item.get("change_sample_count", 0),
    }

def _build_all_pms(
    current_by_pms: dict[str, list[PMSMonthlyHolding]],
    previous_by_pms: dict[str, list[PMSMonthlyHolding]],
    pms_names: list[str],
    pms_with_baseline: set[str] | None = None,
) -> list[dict]:
    # PMS without a prior successful snapshot still have current holdings, but
    # those values must not be interpreted as a 100% portfolio change.
    comparison_pms = set(pms_names) if pms_with_baseline is None else pms_with_baseline
    current_maps: dict[str, dict[tuple[str, str], PMSMonthlyHolding]] = {}
    previous_maps: dict[str, dict[tuple[str, str], PMSMonthlyHolding]] = {}
    identity_names: dict[tuple[str, str], str] = {}

    for pms in pms_names:
        current_maps[pms] = _aggregate_rows(current_by_pms.get(pms, []))
        previous_maps[pms] = _aggregate_rows(previous_by_pms.get(pms, []))
        for key, row in current_maps[pms].items():
            identity_names[key] = row.security_name
        for key, row in previous_maps[pms].items():
            identity_names.setdefault(key, row.security_name)

    output: list[dict] = []
    for identity in set(identity_names):
        values: dict[str, float | None] = {}
        value_measurements: dict[str, str | None] = {}
        metric_types: set[str] = set()
        total_prev = 0.0
        total_current = 0.0
        comparable = True
        for pms in pms_names:
            current = current_maps[pms].get(identity)
            previous = previous_maps[pms].get(identity)
            if current is not None:
                values[pms] = float(current.quantity or 0.0)
                value_measurements[pms] = (current.measurement_type or "QUANTITY").upper()
                if pms in comparison_pms:
                    metric_types.add((current.measurement_type or "QUANTITY").upper())
                    total_current += float(current.quantity or 0.0)
            else:
                values[pms] = None
                value_measurements[pms] = None
            if pms not in comparison_pms:
                continue
            if previous is not None:
                metric_types.add((previous.measurement_type or "QUANTITY").upper())
                total_prev += float(previous.quantity or 0.0)
            if current is not None and previous is not None and (current.measurement_type or "QUANTITY").upper() != (previous.measurement_type or "QUANTITY").upper():
                comparable = False
        metric_type = next(iter(metric_types)) if len(metric_types) == 1 else ("MIXED" if metric_types else "QUANTITY")
        diff = (total_current - total_prev) if comparable and metric_type != "MIXED" else None
        diff_pct = _diff_pct(total_prev, total_current) if diff is not None else None
        output.append({
            "security_name": identity_names[identity],
            "values": values,
            "value_measurements": value_measurements,
            "previous_total": total_prev if comparable and metric_type != "MIXED" else None,
            "current_total": total_current if comparable and metric_type != "MIXED" else None,
            "diff": diff,
            "diff_pct": diff_pct,
            "measurement_type": metric_type,
        })
    return sorted(output, key=lambda row: (-abs(row["diff_pct"] or 0), row["security_name"].upper()))


def _month_label(month: str) -> str:
    year, mon = [int(part) for part in month.split("-")]
    return f"{calendar.month_abbr[mon].upper()}'{str(year)[-2:]}"


def _history_rows_for_pms(
    history_months: list[str],
    snapshots: dict[str, list[PMSMonthlyHolding]],
    *,
    report_month: str,
    previous_month: str | None,
    month_labels: dict[str, str],
    comparison_compatible: bool = True,
    pms_added: bool = False,
    pms_removed: bool = False,
) -> list[dict]:
    """Build a complete security-by-month history for one PMS.

    A PMS first appearing after the platform already has history is shown as a
    100% addition against zero. A PMS absent from a month in which other PMS
    reports were uploaded is shown as a 100% removal against its latest report.
    These lifecycle classifications are explicit business rules; a missing
    report is still flagged as an assumption, not proof that the mandate closed.
    """
    month_maps: dict[str, dict[tuple[str, str], PMSMonthlyHolding]] = {}
    identity_rows: dict[tuple[str, str], list[PMSMonthlyHolding]] = defaultdict(list)
    for period in history_months:
        raw_rows = snapshots.get(period)
        if raw_rows is None:
            continue
        aggregated = _aggregate_rows(raw_rows)
        month_maps[period] = aggregated
        for identity, holding in aggregated.items():
            identity_rows[identity].append(holding)

    output: list[dict] = []
    if pms_added and previous_month:
        # The PMS did not exist in the prior portfolio set. Empty baseline makes
        # each current position a +100% addition without inventing a prior row.
        baseline_map: dict[tuple[str, str], PMSMonthlyHolding] | None = {}
    else:
        baseline_map = month_maps.get(previous_month, {}) if previous_month else None
    current_map = month_maps.get(report_month, {})
    can_compare = (
        previous_month is not None
        and (report_month in month_maps or pms_removed)
        and (baseline_map is not None)
    )

    for identity, observed_rows in identity_rows.items():
        latest_row = next(
            (month_maps[period][identity] for period in reversed(history_months)
             if period in month_maps and identity in month_maps[period]),
            observed_rows[-1],
        )
        metric_types = {(row.measurement_type or "QUANTITY").upper() for row in observed_rows}
        row_metric = next(iter(metric_types)) if len(metric_types) == 1 else "MIXED"
        values: dict[str, float | None] = {}
        value_measurements: dict[str, str | None] = {}
        held_before = False
        for period in history_months:
            label = month_labels[period]
            period_map = month_maps.get(period)
            if period_map is None:
                # A removed PMS is treated as zero in the selected reporting
                # month only. Missing historical months remain blank.
                if pms_removed and period == report_month:
                    values[label] = 0.0
                    value_measurements[label] = row_metric
                else:
                    values[label] = None
                    value_measurements[label] = None
            else:
                holding = period_map.get(identity)
                if holding is not None:
                    values[label] = float(holding.quantity or 0.0)
                    value_measurements[label] = (holding.measurement_type or "QUANTITY").upper()
                    held_before = True
                else:
                    # The reference tracker leaves pre-entry periods blank, but
                    # shows zero after a security has appeared and is later sold.
                    values[label] = 0.0 if held_before else None
                    value_measurements[label] = row_metric

        current_holding = current_map.get(identity)
        previous_holding = baseline_map.get(identity) if baseline_map is not None else None
        current_value = float(current_holding.quantity or 0.0) if current_holding else 0.0
        previous_value = float(previous_holding.quantity or 0.0) if previous_holding else 0.0
        current_metric = (current_holding.measurement_type or "QUANTITY").upper() if current_holding else row_metric
        previous_metric = (previous_holding.measurement_type or "QUANTITY").upper() if previous_holding else row_metric
        comparable = (
            can_compare
            and comparison_compatible
            and current_metric == previous_metric
            and current_metric != "MIXED"
        )
        diff = current_value - previous_value if comparable else None
        diff_pct = _diff_pct(previous_value, current_value) if comparable else None
        if comparable and pms_added and current_holding is not None and abs(current_value) > 1e-12:
            status = "NEW"
        elif comparable and pms_removed and previous_holding is not None and abs(previous_value) > 1e-12:
            status = "EXITED"
        elif comparable:
            status = _status(previous_value, current_value, True)
        elif current_holding and not can_compare:
            status = "BASELINE"
        elif can_compare and (
            not comparison_compatible
            or current_metric != previous_metric
            or current_metric == "MIXED"
        ):
            status = "MEASUREMENT_CHANGED"
        else:
            status = "HISTORY"
        output.append({
            "isin": latest_row.isin,
            "security_name": latest_row.security_name,
            "values": values,
            "value_measurements": value_measurements,
            "measurement_type": row_metric,
            "diff": diff,
            "diff_pct": diff_pct,
            "status": status,
            "previous": previous_value if can_compare else None,
            "current": current_value if can_compare else None,
            "diff_from_month": month_labels.get(previous_month) if previous_month else None,
            "diff_to_month": month_labels.get(report_month) if report_month else None,
        })

    return sorted(
        output,
        key=lambda row: (
            0 if row.get("status") in {"BOUGHT", "NEW"} else 1 if row.get("status") in {"SOLD", "EXITED"} else 2,
            -(abs(row.get("diff_pct") or 0.0)),
            row["security_name"].upper(),
        ),
    )


def _history_rows_all_pms(
    history_months: list[str],
    snapshots_by_pms: dict[str, dict[str, list[PMSMonthlyHolding]]],
    *,
    report_month: str,
    previous_month: str | None,
    month_labels: dict[str, str],
) -> list[dict]:
    """Create the consolidated security history by adding comparable PMS quantities.

    Percentage allocations from different PMS portfolios are not additive.  Such
    rows are retained in the history but marked MIXED and left blank in the
    consolidated sheet rather than presenting a misleading sum.
    """
    month_maps: dict[str, dict[tuple[str, str], tuple[float, str, str | None]]] = {}
    all_identities: set[tuple[str, str]] = set()
    names: dict[tuple[str, str], str] = {}
    for period in history_months:
        grouped: dict[tuple[str, str], list[PMSMonthlyHolding]] = defaultdict(list)
        has_report = False
        for pms_snapshots in snapshots_by_pms.values():
            raw_rows = pms_snapshots.get(period)
            if raw_rows is None:
                continue
            has_report = True
            for identity, holding in _aggregate_rows(raw_rows).items():
                grouped[identity].append(holding)
                names[identity] = holding.security_name
                all_identities.add(identity)
        if not has_report:
            continue
        period_values: dict[tuple[str, str], tuple[float, str, str | None]] = {}
        for identity, group in grouped.items():
            metric_types = {(item.measurement_type or "QUANTITY").upper() for item in group}
            metric = next(iter(metric_types)) if len(metric_types) == 1 else "MIXED"
            # Share quantities are additive across PMSs; allocation percentages
            # are portfolio-specific and must never be summed across managers.
            if metric == "PERCENTAGE" and len(group) > 1:
                metric = "MIXED"
            amount = sum(float(item.quantity or 0.0) for item in group) if metric != "MIXED" else 0.0
            isin = next((item.isin for item in group if item.isin), None)
            period_values[identity] = (amount, metric, isin)
        month_maps[period] = period_values

    baseline_map = month_maps.get(previous_month, {}) if previous_month else None
    current_map = month_maps.get(report_month, {})
    period_pms = {
        period: {pms_name for pms_name, pms_snapshots in snapshots_by_pms.items() if period in pms_snapshots}
        for period in (previous_month, report_month)
        if period is not None
    }
    # A consolidated delta is only meaningful when the same PMS set is present
    # in both periods.  Otherwise a missing file could masquerade as a sell-off.
    coverage_matches = (
        previous_month is not None
        and period_pms.get(previous_month, set()) == period_pms.get(report_month, set())
    )
    can_compare = (
        previous_month is not None
        and report_month in month_maps
        and baseline_map is not None
        and coverage_matches
    )
    output: list[dict] = []
    for identity in all_identities:
        metrics = {month_maps[period][identity][1] for period in history_months if period in month_maps and identity in month_maps[period]}
        row_metric = next(iter(metrics)) if len(metrics) == 1 else "MIXED"
        values: dict[str, float | None] = {}
        value_measurements: dict[str, str | None] = {}
        held_before = False
        for period in history_months:
            label = month_labels[period]
            if period not in month_maps:
                values[label] = None
                value_measurements[label] = None
                continue
            value = month_maps[period].get(identity)
            if value is not None:
                values[label] = value[0] if value[1] != "MIXED" else None
                value_measurements[label] = value[1]
                held_before = True
            else:
                values[label] = 0.0 if held_before else None
                value_measurements[label] = row_metric

        current = current_map.get(identity)
        previous = baseline_map.get(identity) if baseline_map is not None else None
        current_metric = current[1] if current else row_metric
        previous_metric = previous[1] if previous else row_metric
        comparable = (
            can_compare
            and current_metric == previous_metric
            and current_metric != "MIXED"
            and row_metric != "MIXED"
        )
        current_value = current[0] if current and current[1] != "MIXED" else 0.0
        previous_value = previous[0] if previous and previous[1] != "MIXED" else 0.0
        diff = current_value - previous_value if comparable else None
        diff_pct = _diff_pct(previous_value, current_value) if comparable else None
        isin = next(
            (month_maps[period][identity][2] for period in reversed(history_months)
             if period in month_maps and identity in month_maps[period] and month_maps[period][identity][2]),
            None,
        )
        output.append({
            "security_name": names[identity],
            "isin": isin,
            "values": values,
            "value_measurements": value_measurements,
            "measurement_type": row_metric,
            "diff": diff,
            "diff_pct": diff_pct,
            "previous": previous_value if comparable else None,
            "current": current_value if comparable else None,
            "diff_from_month": month_labels.get(previous_month) if previous_month else None,
            "diff_to_month": month_labels.get(report_month) if report_month else None,
        })
    return sorted(output, key=lambda row: row["security_name"].upper())


async def build_comparison(db, month: str, excluded_pms: list[str] | None = None) -> dict:
    # Load all current, successfully processed monthly files up to the selected
    # appraisal month.  This creates the historic columns shown in the reference
    # tracker, not just a previous/current two-column comparison.
    registry_rows = (await db.scalars(select(PMSRegistry).order_by(PMSRegistry.pms_name))).all()
    registry_map = {row.id: row for row in registry_rows}
    excluded_names = [str(name).strip() for name in (excluded_pms or []) if str(name).strip()]
    excluded_keys = {pms_identity_key(name) for name in excluded_names if pms_identity_key(name)}
    registry_name_by_key = {pms_identity_key(row.pms_name): row.pms_name for row in registry_rows}
    excluded_internal_by_key = {
        key: registry_name_by_key.get(key, next((name for name in excluded_names if pms_identity_key(name) == key), key))
        for key in excluded_keys
    }
    excluded_internal_names = set(excluded_internal_by_key.values())
    history_result = await db.scalars(
        select(PMSMonthlyFile)
        .where(
            PMSMonthlyFile.comparison_month <= month,
            PMSMonthlyFile.status == "processed",
            PMSMonthlyFile.is_current.is_(True),
        )
        .order_by(PMSMonthlyFile.comparison_month, PMSMonthlyFile.pms_id, PMSMonthlyFile.id)
    )
    history_files = list(history_result.all())
    current_files_all = [item for item in history_files if item.comparison_month == month]
    # Explicit user selections override files present in this ZIP or previously
    # uploaded for this same month. The historical records remain untouched.
    current_files = [
        item for item in current_files_all
        if item.pms_id in registry_map and pms_identity_key(registry_map[item.pms_id].pms_name) not in excluded_keys
    ]
    history_months = sorted({item.comparison_month for item in history_files})
    month_labels = {period: _month_label(period) for period in history_months}

    file_ids = [item.id for item in history_files]
    holdings_by_file: dict[int, list[PMSMonthlyHolding]] = defaultdict(list)
    if file_ids:
        holdings_result = await db.scalars(
            select(PMSMonthlyHolding).where(PMSMonthlyHolding.monthly_file_id.in_(file_ids))
        )
        for holding in holdings_result.all():
            if is_equity_stock(holding.security_name, holding.raw_row):
                holdings_by_file[holding.monthly_file_id].append(holding)

    snapshots_by_pms: dict[str, dict[str, list[PMSMonthlyHolding]]] = defaultdict(dict)
    files_by_pms: dict[str, dict[str, PMSMonthlyFile]] = defaultdict(dict)
    for monthly_file in history_files:
        pms = registry_map.get(monthly_file.pms_id)
        if pms is None or pms_identity_key(pms.pms_name) in excluded_keys:
            continue
        # The loaded holding map is already stock-only, so history tabs and
        # per-PMS bought/sold calculations use the same filtered records.
        snapshots_by_pms[pms.pms_name][monthly_file.comparison_month] = holdings_by_file.get(monthly_file.id, [])
        files_by_pms[pms.pms_name][monthly_file.comparison_month] = monthly_file

    previous_global = max((period for period in history_months if period < month), default=None)
    pms_names = sorted(set(snapshots_by_pms) | excluded_internal_names, key=_pms_order_key)
    registry_by_name = {row.pms_name: row for row in registry_rows}
    current_file_by_pms = {
        registry_map[file.pms_id].pms_name: file
        for file in current_files
        if file.pms_id in registry_map
    }

    history_previous_used: dict[str, str | None] = {}
    pms_added_by_pms: dict[str, bool] = {}
    pms_removed_by_pms: dict[str, bool] = {}
    comparison_compatible_by_pms: dict[str, bool] = {}
    measurement_change_notes: dict[str, str] = {}
    current_by_pms: dict[str, list[PMSMonthlyHolding]] = {}
    pms_summaries: list[dict] = []
    active_names: set[str] = set()
    review_rows: list[dict] = []
    has_prior_portfolio_period = previous_global is not None
    has_current_month_uploads = bool(current_files)

    for pms_name in pms_names:
        pms = registry_by_name.get(pms_name)
        if pms_identity_key(pms_name) in excluded_keys:
            history_previous_used[pms_name] = None
            pms_added_by_pms[pms_name] = False
            pms_removed_by_pms[pms_name] = False
            comparison_compatible_by_pms[pms_name] = True
            pms_summaries.append({
                "pms_name": _display_pms_name(pms_name),
                "format": pms.format if pms is not None else "",
                "holdings": 0,
                "average_abs_change_pct": None,
                "change_sample_count": 0,
                "bought_count": 0,
                "new_count": 0,
                "sold_count": 0,
                "exited_count": 0,
                "held_count": 0,
                "measurement_changed_count": 0,
                "previous_month_used": None,
                "comparison_month_used": None,
                "previous_quarter_used": None,
                "is_new": False,
                "is_removed": False,
                "is_excluded": True,
                "bought": [],
                "sold": [],
                "baseline": [],
                "baseline_count": 0,
            })
            continue
        if pms is None:
            continue
        monthly_file = current_file_by_pms.get(pms_name)
        pms_file_history = files_by_pms.get(pms_name, {})
        earlier_periods = [period for period in pms_file_history if period < month]
        previous_period = max(earlier_periods) if earlier_periods else None
        previous_file = pms_file_history.get(previous_period) if previous_period else None

        # When at least one report has been processed for this month, a previously
        # seen PMS with no file submitted for the month is treated as removed.
        # A PMS whose report was submitted but failed parsing (last_seen_month is
        # this month) is not treated as removed. If no files were uploaded for the
        # month at all, do not infer a mass removal from an empty upload.
        is_removed_pms = (
            monthly_file is None
            and has_current_month_uploads
            and previous_file is not None
            and pms.last_seen_month != month
        )
        is_new_pms = (
            monthly_file is not None
            and previous_file is None
            and has_prior_portfolio_period
        )
        if monthly_file is None and not is_removed_pms:
            # Keep the historical PMS sheet below, but do not create a current
            # month summary section when the month has no reports at all.
            pms_added_by_pms[pms_name] = False
            pms_removed_by_pms[pms_name] = False
            history_previous_used[pms_name] = previous_period
            continue

        current_rows = holdings_by_file.get(monthly_file.id, []) if monthly_file else []
        previous_rows = holdings_by_file.get(previous_file.id, []) if previous_file else []
        if monthly_file:
            active_names.add(pms_name)
        current_by_pms[pms_name] = current_rows
        # New PMSs compare against the immediately previous global report month
        # as a zero opening balance. Removed PMSs compare against their own last
        # available report, even when that is older than the previous global month.
        comparison_period = previous_period or (previous_global if is_new_pms else None)
        history_previous_used[pms_name] = comparison_period
        pms_added_by_pms[pms_name] = is_new_pms
        pms_removed_by_pms[pms_name] = is_removed_pms

        basis_change = _measurement_basis_change(previous_rows, current_rows) if previous_file and monthly_file else None
        comparison_compatible = basis_change is None
        comparison_compatible_by_pms[pms_name] = comparison_compatible
        details = _detail_rows(previous_rows, current_rows)
        if monthly_file and previous_file is None and not is_new_pms:
            # First-ever system month: show opening positions as baseline, not
            # as purchases, because no earlier portfolio history exists.
            for row in details:
                if row.get("current") is not None and row.get("previous") is None:
                    row["status"] = "BASELINE"
                    row["diff"] = None
                    row["diff_pct"] = None
        elif basis_change is not None:
            previous_types, current_types = basis_change
            previous_label = month_labels.get(previous_period, previous_period)
            current_label = month_labels.get(month, month)
            previous_text = "/".join(sorted(previous_types))
            current_text = "/".join(sorted(current_types))
            measurement_change_notes[pms_name] = (
                f"{pms_name}: measurement basis changed from {previous_text} in {previous_label} "
                f"to {current_text} in {current_label}. Buy/sell and DIFF calculations are suppressed; "
                "both monthly holding lists remain visible for review. Confirm that both reports represent "
                "the same underlying portfolio before comparing."
            )
            for row in details:
                row["status"] = "MEASUREMENT_CHANGED"
                row["diff"] = None
                row["diff_pct"] = None

        # Expose rows whose metrics cannot be compared instead of silently
        # leaving the PMS blank on the Summary sheet.
        for row in details:
            if row.get("status") == "MEASUREMENT_CHANGED":
                review_rows.append({
                    "pms_name": pms_name,
                    "security_name": row.get("security_name", ""),
                    "previous_value": row.get("previous"),
                    "previous_measurement_type": row.get("previous_measurement_type"),
                    "current_value": row.get("current"),
                    "current_measurement_type": row.get("current_measurement_type"),
                    "reason": measurement_change_notes.get(
                        pms_name,
                        "The security's measurement type differs between reporting periods; no buy/sell has been inferred.",
                    ),
                })

        bought = _summary_side(details, True, pms_name)
        sold = _summary_side(details, False, pms_name)
        baseline = _baseline_side(details)
        average_change_pct, change_sample_count = _change_profile(details)
        pms_summaries.append({
            "pms_name": _display_pms_name(pms_name),
            "format": monthly_file.format if monthly_file else previous_file.format,
            "holdings": len(_aggregate_rows(current_rows)),
            "average_abs_change_pct": average_change_pct,
            "change_sample_count": change_sample_count,
            "bought_count": sum(1 for row in details if row["status"] == "BOUGHT"),
            "new_count": sum(1 for row in details if row["status"] == "NEW"),
            "sold_count": sum(1 for row in details if row["status"] == "SOLD"),
            "exited_count": sum(1 for row in details if row["status"] == "EXITED"),
            "held_count": sum(1 for row in details if row["status"] == "HELD"),
            "measurement_changed_count": sum(1 for row in details if row["status"] == "MEASUREMENT_CHANGED"),
            "previous_month_used": previous_period,
            "comparison_month_used": comparison_period,
            "previous_quarter_used": previous_period,
            "is_new": is_new_pms,
            "is_removed": is_removed_pms,
            "bought": bought,
            "sold": sold,
            "baseline": baseline,
            "baseline_count": len(baseline),
        })

    all_pms_rows = _history_rows_all_pms(
        history_months,
        snapshots_by_pms,
        report_month=month,
        previous_month=previous_global,
        month_labels=month_labels,
    )

    summary_rows: list[dict] = []
    for pms in sorted(pms_summaries, key=lambda item: _pms_order_key(item["pms_name"])):
        max_rows = 0 if pms.get("is_excluded") else max(
            len(pms["bought"]), len(pms["sold"]), len(pms.get("baseline", [])), 1
        )
        summary_rows.append({
            "pms": pms["pms_name"],
            "section": True,
            "is_new_pms": bool(pms.get("is_new")),
            "is_removed_pms": bool(pms.get("is_removed")),
            "is_excluded_pms": bool(pms.get("is_excluded")),
        })
        if pms.get("measurement_changed_count"):
            summary_rows.append({
                "review_notice": (
                    f"{pms['measurement_changed_count']} holdings retained for review — "
                    "the source measurement basis changed, so no buy/sell is inferred. "
                    "See the Review Required section below and this PMS worksheet."
                )
            })
        for index in range(max_rows):
            bought = pms["bought"][index] if index < len(pms["bought"]) else None
            sold = pms["sold"][index] if index < len(pms["sold"]) else None
            baseline = pms.get("baseline", [])[index] if index < len(pms.get("baseline", [])) else None
            summary_rows.append({
                "pms": "",
                "section": False,
                "bought_security": bought["security_name"] if bought else "",
                "bought_pct": bought["diff_pct"] if bought else None,
                "sold_security": sold["security_name"] if sold else "",
                "sold_pct": -(abs(sold["diff_pct"])) if sold and sold["diff_pct"] is not None else None,
                "baseline_security": baseline["security_name"] if baseline else "",
                "baseline_value": baseline["current"] if baseline else None,
                "baseline_measurement_type": baseline["measurement_type"] if baseline else None,
            })
        summary_rows.append({"blank": True})

    # Keep status historical relative to the selected month; later uploads must
    # not rewrite the state shown by an older saved report.
    current_pms_ids = {file.pms_id for file in current_files}
    registry_history = []
    registry_keys_seen: set[str] = set()
    for registry_row in registry_rows:
        registry_key = pms_identity_key(registry_row.pms_name)
        registry_keys_seen.add(registry_key)
        if registry_key in excluded_keys:
            historical_status = "NOT_RECEIVED"
        elif registry_row.id in current_pms_ids:
            historical_status = "ACTIVE"
        else:
            has_prior = any(file.pms_id == registry_row.id and file.comparison_month < month for file in history_files)
            historical_status = "INACTIVE" if has_prior else "NOT_SEEN"
        registry_history.append({
            "pms_name": _display_pms_name(registry_row.pms_name),
            "format": next((file.format for file in current_files if file.pms_id == registry_row.id), registry_row.format),
            "status": historical_status,
            "first_seen_month": registry_row.first_seen_month,
            "last_seen_month": registry_row.last_seen_month,
            "last_processed_month": registry_row.last_processed_month,
            "last_filename": registry_row.last_filename,
        })

    # A user may mark a fund as not received before it has ever appeared in the
    # registry (including when its matching file was present but deliberately
    # ignored). Keep it visible in the workbook's PMS Status sheet as well.
    for key, internal_name in excluded_internal_by_key.items():
        if key in registry_keys_seen:
            continue
        registry_history.append({
            "pms_name": _display_pms_name(internal_name),
            "format": "",
            "status": "NOT_RECEIVED",
            "first_seen_month": "",
            "last_seen_month": "",
            "last_processed_month": "",
            "last_filename": "",
        })

    data_quality: list[str] = list(measurement_change_notes.values())
    if excluded_internal_names:
        excluded_display_names = sorted(
            (_display_pms_name(name) for name in excluded_internal_names),
            key=_pms_order_key,
        )
        data_quality.append(
            "Marked as NOT RECEIVED by the user and excluded from this report: "
            + ", ".join(excluded_display_names)
            + ". Any matching file in the uploaded ZIP was ignored; no holdings, buys, sells or exits are included for these PMSs."
        )
    errored = [
        row for row in registry_rows
        if row.status == "ERROR" and row.last_seen_month == month and pms_identity_key(row.pms_name) not in excluded_keys
    ]
    for row in errored:
        data_quality.append(f"{row.pms_name}: {row.error_message or 'Processing error.'}")
    inactive = [row["pms_name"] for row in registry_history if row["status"] == "INACTIVE"]
    if inactive:
        data_quality.append(f"No report was uploaded for this period for: {', '.join(sorted(inactive, key=str.upper))}.")
    new_pms_names = [item["pms_name"] for item in pms_summaries if item.get("is_new")]
    if new_pms_names:
        data_quality.append(
            "New PMS introduced in this reporting period: "
            + ", ".join(sorted(new_pms_names, key=str.upper))
            + ". Every reported holding is classified as 100% added against a zero opening balance."
        )
    no_prior = [
        item["pms_name"] for item in pms_summaries
        if not item.get("previous_month_used") and not item.get("is_new") and not item.get("is_excluded")
    ]
    if no_prior:
        data_quality.append(
            "No earlier system appraisal month was available for: "
            + ", ".join(sorted(no_prior, key=str.upper))
            + ". Their first upload is shown as baseline holdings, not as purchases."
        )
    removed_pms_names = [item["pms_name"] for item in pms_summaries if item.get("is_removed")]
    if removed_pms_names:
        data_quality.append(
            "No report was uploaded for these previously reported PMSs: "
            + ", ".join(sorted(removed_pms_names, key=str.upper))
            + ". Their last reported holdings are shown as 100% removed for tracker continuity. "
            "This is an absence-of-report assumption, not proof of liquidation or mandate termination; verify before acting on it."
        )
    if previous_global and current_files:
        earlier_pms = {name for name, periods in snapshots_by_pms.items() if previous_global in periods}
        current_period_pms = {name for name, periods in snapshots_by_pms.items() if month in periods}
        if earlier_pms != current_period_pms:
            data_quality.append(
                "The All PMS consolidated DIFF is suppressed because different PMS reports are present in the latest and previous periods. "
                "Individual PMS comparisons remain available."
            )
    changed_metrics = [
        f"{pms}: {count} holding row(s) are listed in Review Required because the report measurement basis changed; "
        "monthly values remain visible, but no buy/sell or DIFF has been inferred."
        for pms, count in ((item["pms_name"], item.get("measurement_changed_count", 0)) for item in pms_summaries)
        if count
    ]
    data_quality.extend(changed_metrics)
    if not current_files and not excluded_internal_names:
        data_quality.append("No successfully processed PMS files are available for this month.")

    ranked_pms = [item for item in pms_summaries if item.get("average_abs_change_pct") is not None]
    biggest_change = (
        _change_profile_summary(sorted(ranked_pms, key=lambda item: (-item["average_abs_change_pct"], item["pms_name"].upper()))[0])
        if ranked_pms else None
    )
    least_change = (
        _change_profile_summary(sorted(ranked_pms, key=lambda item: (item["average_abs_change_pct"], item["pms_name"].upper()))[0])
        if ranked_pms else None
    )
    total_holdings = sum(len(_aggregate_rows(rows)) for rows in current_by_pms.values())
    new_stock_entries = sum(item["new_count"] for item in pms_summaries)
    old_stock_removals = sum(item["exited_count"] for item in pms_summaries)
    summary = {
        "comparison_month": month,
        "previous_month": previous_global,
        "previous_quarter_end": previous_quarter_end_month(month),
        "comparison_basis": "latest earlier successfully processed appraisal report for each PMS",
        "report_date": last_day_label(month),
        "total_pms": len(current_files),
        "active_pms": len(active_names),
        "total_holdings": total_holdings,
        "holding_count": total_holdings,
        "new_stock_entries": new_stock_entries,
        "new_holdings": new_stock_entries,
        "old_stock_removals": old_stock_removals,
        "bought_holdings": sum(item["bought_count"] for item in pms_summaries),
        "sold_holdings": sum(item["sold_count"] for item in pms_summaries),
        "exited_holdings": old_stock_removals,
        "held_holdings": sum(item["held_count"] for item in pms_summaries),
        "measurement_changed_holdings": sum(item.get("measurement_changed_count", 0) for item in pms_summaries),
        "biggest_change_pms": biggest_change,
        "least_change_pms": least_change,
        "change_ranking_method": "Average absolute relative percentage change for comparable securities in the latest two available reports.",
        "history_months": history_months,
        "excluded_pms": sorted(
            (_display_pms_name(name) for name in excluded_internal_names),
            key=_pms_order_key,
        ),
    }

    sheets = [
        {
            "name": "Summary",
            "type": "summary",
            "columns": ["PMS", "SCRIP", "% Change", "SCRIP", "% Change"],
            "rows": summary_rows,
            "review_rows": review_rows,
            "pms_names": sorted((_display_pms_name(name) for name in active_names), key=_pms_order_key),
            "report_date": summary["report_date"],
        },
        {
            "name": "All PMS",
            "display_name": "All PMS",
            "type": "all_pms",
            "columns": ["SECURITY", *[month_labels[period] for period in history_months], "DIFF", "DIFF %"],
            "month_keys": [month_labels[period] for period in history_months],
            "report_month": month_labels.get(month),
            "comparison_month": month_labels.get(previous_global) if previous_global else None,
            "rows": all_pms_rows,
        },
    ]

    used_sheet_names: set[str] = {"Summary", "All PMS", "PMS Status", "Data Quality"}
    for pms_name in pms_names:
        pms_previous = history_previous_used.get(pms_name)
        is_excluded = pms_identity_key(pms_name) in excluded_keys
        rows = [] if is_excluded else _history_rows_for_pms(
            history_months,
            snapshots_by_pms[pms_name],
            report_month=month,
            previous_month=pms_previous,
            month_labels=month_labels,
            comparison_compatible=comparison_compatible_by_pms.get(pms_name, True),
            pms_added=pms_added_by_pms.get(pms_name, False),
            pms_removed=pms_removed_by_pms.get(pms_name, False),
        )
        display_name = _display_pms_name(pms_name)
        sheet_name = _sheet_name(display_name, used_sheet_names)
        used_sheet_names.add(sheet_name)
        note = (
            "Excluded by user — marked as not received. This worksheet is intentionally empty."
            if is_excluded
            else measurement_change_notes[pms_name]
            if pms_name in measurement_change_notes
            else f"{month_labels.get(month, month)} — new PMS; all holdings shown as 100% added against zero"
            if pms_added_by_pms.get(pms_name)
            else f"No {month_labels.get(month, month)} report; last reported holdings shown as 100% removed — verify discontinuation"
            if pms_removed_by_pms.get(pms_name)
            else f"{month_labels.get(month, month)} compared with {month_labels.get(pms_previous, pms_previous)}"
            if pms_previous and month in snapshots_by_pms[pms_name]
            else f"No report uploaded for {month_labels.get(month, month)}"
            if month not in snapshots_by_pms[pms_name]
            else "First available report · baseline"
        )
        sheets.append({
            "name": sheet_name,
            "display_name": display_name,
            "type": "pms",
            "comparison_note": note,
            "columns": ["SECURITY", *[month_labels[period] for period in history_months], "DIFF", "DIFF %"],
            "month_keys": [month_labels[period] for period in history_months],
            "report_month": month_labels.get(month),
            "comparison_month": month_labels.get(pms_previous) if pms_previous else None,
            "rows": rows,
        })

    sheets.append({
        "name": "PMS Status",
        "display_name": "PMS Status",
        "type": "registry",
        "columns": ["PMS", "Format", "Status", "First Seen", "Last Seen", "Last Processed", "Filename"],
        "rows": [
            {
                "PMS": item.get("pms_name", ""),
                "Format": item.get("format", ""),
                "Status": item.get("status", ""),
                "First Seen": item.get("first_seen_month", ""),
                "Last Seen": item.get("last_seen_month", ""),
                "Last Processed": item.get("last_processed_month", ""),
                "Filename": item.get("last_filename", ""),
            }
            for item in registry_history
        ],
    })
    sheets.append({"name": "Data Quality", "type": "data_quality", "columns": ["Issue"], "rows": [{"Issue": item} for item in data_quality]})
    return {
        "summary": summary,
        "pms_summaries": sorted(pms_summaries, key=lambda item: _pms_order_key(item["pms_name"])),
        "pms_details": {
            _display_pms_name(pms_name): (
                [] if pms_identity_key(pms_name) in excluded_keys else _history_rows_for_pms(
                    history_months,
                    snapshots_by_pms[pms_name],
                    report_month=month,
                    previous_month=history_previous_used.get(pms_name),
                    month_labels=month_labels,
                    comparison_compatible=comparison_compatible_by_pms.get(pms_name, True),
                    pms_added=pms_added_by_pms.get(pms_name, False),
                    pms_removed=pms_removed_by_pms.get(pms_name, False),
                )
            )
            for pms_name in pms_names
        },
        "all_pms": all_pms_rows,
        "registry": sorted(registry_history, key=lambda item: _pms_order_key(item["pms_name"])),
        "data_quality": data_quality,
        "sheets": sheets,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", " ", name).strip() or "PMS"
    base = base[:31]
    candidate = base
    counter = 2
    while candidate in used:
        suffix = f" {counter}"
        candidate = f"{base[:31 - len(suffix)]}{suffix}"
        counter += 1
    return candidate