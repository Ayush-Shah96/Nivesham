from __future__ import annotations

from collections import defaultdict
from copy import copy
from datetime import date
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

_ROW_STYLES = {}

def _clear_data(ws, start_row: int, start_col: int = 1, end_col: int | None = None) -> None:
    end_col = end_col or ws.max_column
    for row in ws.iter_rows(min_row=start_row, max_row=ws.max_row, min_col=start_col, max_col=end_col):
        for cell in row:
            cell.value = None


def _append_styled(ws, values, style_row: int = 2, start_col: int = 1) -> None:
    row_no = ws.max_row + 1
    ws.insert_rows(row_no)
    for offset, value in enumerate(values):
        cell = ws.cell(row_no, start_col + offset, value)
        if style_row <= ws.max_row:
            src = ws.cell(style_row, start_col + offset)
            if src.has_style:
                cell._style = copy(src._style)
            if src.number_format:
                cell.number_format = src.number_format
    return row_no


def _reset_sheet(ws, header_row: int = 1, start_col: int = 1, end_col: int | None = None) -> None:
    end_col = end_col or ws.max_column
    style_row = header_row + 1
    if ws.max_row >= style_row:
        _ROW_STYLES[ws.title] = [copy(ws.cell(style_row, col)._style) for col in range(1, end_col + 1)]
        _ROW_STYLES[f"{ws.title}:formats"] = [ws.cell(style_row, col).number_format for col in range(1, end_col + 1)]
    if ws.max_row > header_row:
        ws.delete_rows(header_row + 1, ws.max_row - header_row)
    # Remove pivot caches from the edited sheet: the generated values are the
    # authoritative refresh; openpyxl cannot recalculate Excel pivot caches.
    try:
        ws._pivots = []
    except Exception:
        pass


def _apply_cached_styles(ws, header_row: int, end_col: int) -> None:
    styles = _ROW_STYLES.get(ws.title)
    formats = _ROW_STYLES.get(f"{ws.title}:formats")
    if not styles:
        return
    for row_no in range(header_row + 1, ws.max_row + 1):
        for col in range(1, min(end_col, len(styles)) + 1):
            cell = ws.cell(row_no, col)
            if styles[col - 1] is not None:
                cell._style = copy(styles[col - 1])
            if formats and formats[col - 1]:
                cell.number_format = formats[col - 1]


def _instrument_label(item) -> str:
    # ICICI matrix exports are issuer-level, not individual security records;
    # do not mislabel their asset class as ZCB / MLD / NCD.
    if getattr(item, "position_level", "security") == "issuer":
        return ""
    text = f"{item.asset_type or ''} {item.paper or ''}".casefold()
    if "market linked debenture" in text or "mld" in text:
        return "MLD"
    if "zero coupon" in text or "zero-coupon" in text or "zcb" in text:
        return "ZCB"
    if "non convertible debenture" in text or "non-convertible debenture" in text or "ncd" in text:
        return "NCD"
    return item.asset_type or ""


def _paper_key(item) -> str:
    if item.isin and item.isin.strip():
        return f"isin:{item.isin.strip().upper()}"
    paper = str(item.paper or "").strip().casefold()
    issuer = str(item.issuer or "").strip().casefold()
    maturity = item.maturity.isoformat() if item.maturity else ""
    if getattr(item, "position_level", "security") == "issuer":
        return f"issuer:{issuer or paper}"
    return f"paper:{paper}|issuer:{issuer}|maturity:{maturity}"


def build_template_report(template_path: str | Path, result, output_path: str | Path, *, report_date: date, report: dict | None = None) -> Path:
    """Refresh the prior-quarter workbook's named sheets with calculated values.

    The existing workbook is retained as the visual template. Pivot output sheets
    are written as static calculated tables because openpyxl cannot refresh Excel
    pivot caches; no stale cached pivot values are left as the displayed result.
    """
    wb = load_workbook(template_path, data_only=False, keep_links=False)
    try:
        required = {"BASE", "Sheet2", "Sheet3", "Sheet4", "Sheet5", "Sheet6", "Sheet7", "Sheet8", "RULES", "Sheet1"}
        missing = required - set(wb.sheetnames)
        if missing:
            raise ValueError(f"Previous-quarter template is missing required sheets: {', '.join(sorted(missing))}")

        # BASE is the unified canonical ledger. Keep the exact 11-column contract
        # requested for quarterly reporting; additional identifiers remain in the
        # report JSON/source metadata so the next quarter can still reconcile them.
        ws = wb["BASE"]
        _reset_sheet(ws, 1, 1, max(ws.max_column, 12))
        if ws.max_column > 11:
            ws.delete_cols(12, ws.max_column - 11)
        headers = [
            "Fund Name as per Broker", "Paper Name", "GROUP EXPOSURE", "Paper Maturity",
            "Fund Maturity", "Paper Amount in Cr", "% of Holding (Fund Level)",
            "Broker", "ZCB / MLD / NCD", "Prop Mkt Value", "Category",
        ]
        for col, value in enumerate(headers, 1):
            ws.cell(1, col, value)
        for item in result.rows:
            ws.append([
                item.source_scheme or item.fund, item.paper, item.issuer or "Needs Mapping",
                item.maturity, item.fund_maturity, item.market_value_crore, item.allocation * 100,
                item.broker or "Historical BASE", _instrument_label(item), item.exposure,
                item.reported_rating or item.rating or item.category,
            ])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:K{ws.max_row}"
        ws.sheet_view.showGridLines = False
        for row in ws.iter_rows(min_row=2, min_col=4, max_col=5):
            for cell in row:
                cell.number_format = "dd-mmm-yyyy"
        for col in (6, 10):
            for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                row[0].number_format = '#,##0;[Red](#,##0);-'
        for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
            row[0].number_format = '0.0000'

        # Sheet4 is paper × fund detail. The cumulative column sums the same
        # canonical paper across distinct MProfit funds; same-fund duplicate
        # provider feeds were already excluded by the source-selection stage.
        ws = wb["Sheet4"]
        _reset_sheet(ws, 1, 1, max(ws.max_column, 11))
        headers = [
            "GROUP EXPOSURE (Issuer)", "Parent Group", "Paper Name", "ISIN",
            "Fund Name as per Broker", "Broker", "Paper Maturity",
            "% of Holding (Fund Level)", "Prop Mkt Value (Cr)",
            "Cumulative Paper Exposure (Cr)", "Fund Count", "Rating Change",
        ]
        for col, value in enumerate(headers, 1):
            ws.cell(1, col, value)
        paper_totals = defaultdict(float)
        paper_funds = defaultdict(set)
        for item in result.rows:
            key = _paper_key(item)
            paper_totals[key] += item.exposure
            paper_funds[key].add(item.fund)
        previous_ratings = defaultdict(set)
        for prior in result.opening_details:
            prior = dict(prior)
            prior_isin = str(prior.get("isin") or "").strip().upper()
            prior_key = (f"isin:{prior_isin}" if prior_isin else
                         f"paper:{str(prior.get('paper') or '').strip().casefold()}|issuer:{str(prior.get('issuer') or '').strip().casefold()}|maturity:{str(prior.get('maturity') or '')[:10]}")
            prior_rating = str(prior.get("reported_rating") or prior.get("rating") or prior.get("category") or "").strip()
            if prior_rating:
                previous_ratings[prior_key].add(prior_rating)
        sheet4_items = sorted(result.rows, key=lambda x: (
            (x.group or "Others").casefold(), (x.issuer or "Needs Mapping").casefold(),
            x.paper.casefold(), x.fund.casefold()
        ))
        for item in sheet4_items:
            key = _paper_key(item)
            ws.append([
                item.issuer or "Needs Mapping", item.group or "Others", item.paper, item.isin or "",
                item.source_scheme or item.fund, item.broker or "Historical BASE", item.maturity,
                item.allocation * 100, item.exposure, paper_totals[key], len(paper_funds[key]),
                (f"{next(iter(previous_ratings[key]))} -> {item.reported_rating or item.rating}"
                 if len(previous_ratings[key]) == 1 and (item.reported_rating or item.rating)
                 and next(iter(previous_ratings[key])).casefold() != str(item.reported_rating or item.rating).casefold() else "NO"),
            ])
        detail_last_row = ws.max_row
        ws.auto_filter.ref = f"A1:L{max(1, detail_last_row)}"
        ws.freeze_panes = "A2"
        summary_start = detail_last_row + 3
        ws.cell(summary_start, 1, "ISSUER EXPOSURE SUMMARY")
        ws.cell(summary_start + 1, 1, "GROUP EXPOSURE (Issuer)")
        ws.cell(summary_start + 1, 2, "Total Exposure (Cr)")
        issuer_totals = defaultdict(float)
        for item in result.rows:
            issuer_totals[item.issuer or "Needs Mapping"] += item.exposure
        for offset, (issuer, amount) in enumerate(sorted(issuer_totals.items(), key=lambda pair: pair[0].casefold()), 2):
            ws.cell(summary_start + offset, 1, issuer)
            ws.cell(summary_start + offset, 2, amount).number_format = '#,##0;[Red](#,##0);-'
        ws.cell(summary_start + len(issuer_totals) + 2, 1, "Grand Total")
        ws.cell(summary_start + len(issuer_totals) + 2, 2, result.total_exposure).number_format = '#,##0;[Red](#,##0);-'
        for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
            row[0].number_format = "dd-mmm-yyyy"
        for col in (9, 10):
            for row in ws.iter_rows(min_row=2, max_col=col, min_col=col):
                row[0].number_format = '#,##0;[Red](#,##0);-'
        for row in ws.iter_rows(min_row=2, min_col=8, max_col=8):
            row[0].number_format = '0.0000'
        for row in ws.iter_rows(min_row=2, min_col=7, max_col=7):
            row[0].number_format = "dd-mmm-yyyy"

        # Sheet3: security detail, grouped by security and rating (same column contract).
        ws = wb["Sheet3"]
        _reset_sheet(ws, 2, 1, 7)
        ws.cell(1, 1, "Sum of Prop Mkt Value")
        headers = ["GROUP EXPOSURE", "Paper Name", "Fund Name as per Broker", "Paper Maturity", "% of Holding (Fund Level)", "Rating", "Total"]
        for col, value in enumerate(headers, 1): ws.cell(2, col, value)
        for item in sorted(result.rows, key=lambda x: (x.group.casefold(), x.paper.casefold(), x.fund.casefold())):
            ws.append([item.issuer or "Needs Mapping", item.paper, item.fund, item.maturity, item.allocation * 100, item.rating, item.exposure])

        # Sheet2: fund reconciliation; broker market values are compared to portfolio value.
        ws = wb["Sheet2"]
        _reset_sheet(ws, 2, 1, 10)
        ws.cell(1, 1, sum((x.market_value_crore or 0) for x in result.rows))
        ws.cell(1, 2, result.total_exposure)
        headers = ["Broker Scheme / Portfolio Fund", "Broker Paper Amount (Cr)", "Portfolio Fund", "Broker Paper Amount (Cr)", "Current Value (Cr)", "Portfolio Fund", "Broker Paper Amount (Cr)", "Invested Value (Cr)"]
        for col, value in enumerate(headers, 1): ws.cell(2, col, value)
        broker_by_fund = defaultdict(float)
        portfolio_by_fund = {}
        for item in result.rows:
            broker_by_fund[item.fund] += item.market_value_crore or 0
        for fund, value in result.fund_allocations.items(): portfolio_by_fund[fund] = value
        for fund in sorted(set(broker_by_fund) | set(portfolio_by_fund), key=str.casefold):
            ws.append([fund, broker_by_fund.get(fund, 0), fund, broker_by_fund.get(fund, 0), portfolio_by_fund.get(fund, 0), fund, broker_by_fund.get(fund, 0), None])

        # Sheet5: group totals and rating split.
        ws = wb["Sheet5"]
        _reset_sheet(ws, 1, 1, 6)
        headers = ["Row Labels", "Sum of Total", "", "GROUP EXPOSURE", "Rating", "Total"]
        for col, value in enumerate(headers, 1): ws.cell(1, col, value)
        group_total = defaultdict(float); group_rating = defaultdict(lambda: defaultdict(float))
        for item in result.rows:
            group_total[item.group] += item.exposure
            group_rating[item.group][item.rating or "Unrated"] += item.exposure
        row_no = 2
        for group in sorted(group_total, key=str.casefold):
            ws.cell(row_no, 1, group); ws.cell(row_no, 2, group_total[group]); row_no += 1
            for rating, amount in sorted(group_rating[group].items(), key=lambda x: x[1], reverse=True):
                ws.cell(row_no, 4, group); ws.cell(row_no, 5, rating); ws.cell(row_no, 6, amount); row_no += 1

        # Sheet6: issuer/borrower-group mapping summary.
        ws = wb["Sheet6"]
        _reset_sheet(ws, 1, 1, 7)
        headers = ["Group", "Group as per MF", "Sum of Total", "Percentage", "Rating", "Bank / Non-Bank / Govt Owned", "Concat"]
        for col, value in enumerate(headers, 1): ws.cell(1, col, value)
        issuer_totals = defaultdict(float); issuer_group = {}; issuer_rating = {}
        for item in result.rows:
            # Do not write the generic Others bucket as if it were an approved
            # parent-group mapping. A blank Sheet6 Group remains a Review item
            # and can be mapped explicitly in a later quarter.
            parent_group = "" if item.mapping_source in {"review", "group-conflict"} else (item.group or "Others")
            key = (parent_group, item.issuer or "Needs Mapping")
            issuer_totals[key] += item.exposure
            issuer_group[key] = parent_group
            issuer_rating[key] = item.rating or "Unrated"
        denom = result.total_exposure or 1
        for (group, issuer), exposure in sorted(issuer_totals.items(), key=lambda x: (x[0][0].casefold(), x[0][1].casefold())):
            rating = issuer_rating[(group, issuer)]
            kind = "Bank" if "bank" in rating.lower() else ("Govt" if rating in {"SOV", "SDL", "Govt"} else "Non-Bank")
            ws.append([group, issuer, exposure, exposure / denom, rating, kind, f"{rating} {kind}"])

        # Sheet7: category exposure summary in both source-template blocks.
        ws = wb["Sheet7"]
        _reset_sheet(ws, 1, 1, 7)
        headers = ["Row Labels", "Sum of Percentage", "Sum of Exposure", "Exposure", "Percentage", "", ""]
        for col, value in enumerate(headers, 1): ws.cell(1, col, value)
        categories = sorted(result.categories, key=lambda x: float(x.get("exposure", 0)), reverse=True)
        for idx, item in enumerate(categories, 2):
            ws.cell(idx, 1, item.get("category")); ws.cell(idx, 2, item.get("percentage", 0)); ws.cell(idx, 3, item.get("exposure", 0))
            ws.cell(idx, 4, item.get("category")); ws.cell(idx, 5, item.get("exposure", 0)); ws.cell(idx, 6, item.get("percentage", 0))

        # Sheet8: management summary, with the June-style main group table,
        # explicit Added/Exited columns, and category limits below the total.
        ws = wb["Sheet8"]
        try:
            ws._pivots = []
        except Exception:
            pass
        _reset_sheet(ws, 1, 1, max(ws.max_column, 8))
        sheet8_rows = (report or {}).get("sheet8Preview")
        if not sheet8_rows:
            sheet8_rows = [["Group", "Exposure", "Percentage", "Rating", "Paper Count", "Fund Count", "Added", "Exited"]]
            for group in result.groups:
                sheet8_rows.append([group.group, group.exposure, group.percentage, group.rating,
                                    group.paper_count, group.fund_count, None, None])
            sheet8_rows.append(["Grand Total", result.total_exposure, 1 if result.total_exposure else 0, None, None, None, None, None])
        if sheet8_rows:
            for col_no, value in enumerate(sheet8_rows[0], 1):
                ws.cell(1, col_no, value)
            for row in sheet8_rows[1:]:
                ws.append(list(row))
        ws.freeze_panes = "A2"
        # Filter only the consolidated parent-group table. Residual category
        # rows, grand total and category-limit block are presentation sections,
        # not part of the group filter range.
        ws.auto_filter.ref = f"A1:H{max(1, len(result.groups) + 1)}"
        ws.sheet_view.showGridLines = False
        for row_no in range(2, len(sheet8_rows) + 1):
            ws.cell(row_no, 2).number_format = '#,##0.000000'
            ws.cell(row_no, 3).number_format = '0.0000%'
        # Category limits are included below the principal group table so this
        # sheet can be printed as one quarterly summary.
        limit_rows = (report or {}).get("sheet8LimitPreview") or []
        if limit_rows:
            row_no = ws.max_row + 2
            for row in limit_rows:
                for col_no, value in enumerate(row, 1):
                    ws.cell(row_no, col_no, value)
                row_no += 1
        # Broker reconciliation compares published broker-to-broker paper amounts.
        ws = wb["Broker Reconciliation"] if "Broker Reconciliation" in wb.sheetnames else wb.create_sheet("Broker Reconciliation")
        _reset_sheet(ws, 1, 1, max(ws.max_column, 14))
        ws.cell(1, 1, "BROKER-TO-BROKER PAPER AMOUNT DIFFERENCES")
        recon_summary = (report or {}).get("brokerReconciliationSummary", {})
        tolerance_cr = float(recon_summary.get("differenceToleranceCr", 2.0) or 2.0)
        ws.cell(2, 1, f"Same fund + same ISIN; normalized paper + issuer only when ISIN is unavailable. Amounts use one common basis per pair: client Amount when both providers publish it, otherwise market value when both publish it, otherwise MProfit fund value × allocation. Differences of ₹{tolerance_cr:.2f} Cr or less are ignored. Issuer-only feeds are excluded from security-level comparison.")
        recon_headers = ["Fund", "Paper", "Issuer", "ISIN", "Broker A", "Paper Amount A (Cr)", "Broker B", "Paper Amount B (Cr)", "Difference (Cr)", "Higher Amount Reported By", "Difference (%)", "Status", "Amount Basis", "Source Files"]
        for col, value in enumerate(recon_headers, 1): ws.cell(4, col, value)
        for item in (report or {}).get("brokerReconciliation", []):
            ws.append([item.get("fund", ""), item.get("paper", ""), item.get("issuer", ""), item.get("isin", ""), item.get("brokerA", ""), item.get("amountA", 0), item.get("brokerB", ""), item.get("amountB", 0), item.get("differenceCr", 0), item.get("higherBroker", ""), item.get("differencePct", 0), item.get("status", ""), item.get("amountBasis", ""), f'{item.get("sourceA", "")} | {item.get("sourceB", "")}'])
        ws.freeze_panes = "A5"
        if ws.max_row >= 5: ws.auto_filter.ref = f"A4:N{ws.max_row}"
        ws.sheet_view.showGridLines = False
        for col in (6, 8, 9):
            for row in ws.iter_rows(min_row=5, min_col=col, max_col=col): row[0].number_format = '#,##0.00;[Red](#,##0.00);-'
        for row in ws.iter_rows(min_row=5, min_col=11, max_col=11): row[0].number_format = '0.00'
        for col, width in {1:38, 2:44, 3:36, 4:18, 5:18, 7:18, 10:26, 13:55, 14:55}.items(): ws.column_dimensions[get_column_letter(col)].width = width

        # Reapply the source template's first detail-row styles to refreshed values.
        for sheet_name, header_row, end_col in (("BASE", 1, 11), ("Sheet4", 1, 12), ("Sheet3", 2, 7),
                                                ("Sheet2", 2, 8), ("Sheet5", 1, 6), ("Sheet6", 1, 7), ("Sheet7", 1, 7),
                                                ("Sheet8", 1, 8)):
            _apply_cached_styles(wb[sheet_name], header_row, end_col)

        # Report Info is the machine-readable quarter marker used by the next run.
        if "Report Info" in wb.sheetnames:
            info = wb["Report Info"]
            _reset_sheet(info, 1, 1, max(info.max_column, 2))
        else:
            info = wb.create_sheet("Report Info")
        info.cell(1, 1, "Report As Of")
        info.cell(1, 2, report_date)
        info.cell(2, 1, "Baseline As Of")
        baseline_value = (report or {}).get("metadata", {}).get("baselineDate")
        if isinstance(baseline_value, str):
            try:
                baseline_value = date.fromisoformat(baseline_value[:10])
            except ValueError:
                pass
        info.cell(2, 2, baseline_value)
        info.sheet_view.showGridLines = False

        # Preserve RULES and empty Sheet1, but mark the generated report date.
        rules = wb["RULES"]
        rules.cell(rules.max_row + 2, 1, f"Automated report generated for {report_date.isoformat()}.")
        rules.cell(rules.max_row, 2, "Pivot-style summaries are refreshed as calculated values; use the application for interactive analytics.")
        # Explicit widths keep the appended identifiers and long group names usable
        # in Excel; the June template's original widths were too narrow for the new
        # canonical fields.
        column_widths = {
            "BASE": {"A": 42, "B": 44, "C": 36, "D": 16, "E": 16, "F": 18, "G": 22, "H": 16, "I": 22, "J": 18, "K": 22},
            "Sheet4": {"A": 36, "B": 34, "C": 44, "D": 18, "E": 42, "F": 16, "G": 16, "H": 22, "I": 20, "J": 25, "K": 14, "L": 20},
            "Sheet6": {"A": 28, "B": 38, "C": 18, "D": 16, "E": 18, "F": 28, "G": 24},
            "Sheet8": {"A": 34, "B": 17, "C": 15, "D": 38, "E": 15, "F": 15, "G": 34, "H": 34},
        }
        for sheet_name, widths in column_widths.items():
            for column, width in widths.items():
                wb[sheet_name].column_dimensions[column].width = width

        for sheet in wb.worksheets:
            sheet.sheet_view.showGridLines = False
            for row in sheet.iter_rows():
                for cell in row:
                    if cell.value is not None and cell.data_type == "f":
                        # Avoid stale formulas in pivot/template workbooks. All refreshed
                        # output values are calculated in Python before writing.
                        cell.value = None

        # Reapply presentation precision after copying the template's styles.
        # Monetary values display as whole crore; calculations remain full precision.
        for sheet_name, columns in (("BASE", (6, 10)), ("Sheet4", (9, 10)), ("Sheet8", (2,))):
            for col in columns:
                for row in wb[sheet_name].iter_rows(min_row=2, min_col=col, max_col=col):
                    row[0].number_format = '#,##0;[Red](#,##0);-'
        for row in wb["BASE"].iter_rows(min_row=2, min_col=7, max_col=7):
            row[0].number_format = '0.0000'
        for row in wb["Sheet4"].iter_rows(min_row=2, min_col=5, max_col=5):
            row[0].number_format = '0.0000'
        for row in wb["Sheet4"].iter_rows(min_row=2, min_col=6, max_col=6):
            row[0].number_format = '#,##0.000000;[Red](#,##0.000000);-'
        for row in wb["Sheet8"].iter_rows(min_row=2, min_col=2, max_col=2):
            row[0].number_format = '#,##0;[Red](#,##0);-'
        # Reassert dates and whole-crore formats after template styles have been
        # copied: several historic pivot sheets carry General formats for dates.
        for row in wb["Sheet4"].iter_rows(min_row=2, min_col=7, max_col=7):
            row[0].number_format = "dd-mmm-yyyy"
        for row in wb["Sheet4"].iter_rows(min_row=2, min_col=8, max_col=8):
            row[0].number_format = '0.0000'
        for col in (9, 10):
            for row in wb["Sheet4"].iter_rows(min_row=2, min_col=col, max_col=col):
                row[0].number_format = '#,##0;[Red](#,##0);-'
        for row in wb["Sheet8"].iter_rows(min_row=2, min_col=2, max_col=2):
            row[0].number_format = '#,##0;[Red](#,##0);-'
        for row in wb["Sheet8"].iter_rows(min_row=2, min_col=3, max_col=3):
            row[0].number_format = '0.00%'
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
        dest = Path(output_path); dest.parent.mkdir(parents=True, exist_ok=True)
        wb.save(dest)
        return dest
    finally:
        wb.close()
