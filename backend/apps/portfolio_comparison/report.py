from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

NAVY = "245A87"
WHITE = "FFFFFF"
BLACK = "111111"
GRID = "202020"
GREEN = "C6EFCE"
GREEN_TEXT = "006100"
RED = "FFC7CE"
RED_TEXT = "9C0006"
YELLOW = "FFF200"
LIGHT_GRAY = "F3F3F3"

THIN_DARK = Side(style="thin", color=GRID)
THIN_LIGHT = Side(style="thin", color="D8D8D8")


def _pct_ratio(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def _safe_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _apply_border(ws, min_row: int, max_row: int, min_col: int, max_col: int) -> None:
    if max_row < min_row or max_col < min_col:
        return
    border = Border(left=THIN_DARK, right=THIN_DARK, top=THIN_DARK, bottom=THIN_DARK)
    for row in ws.iter_rows(min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col):
        for cell in row:
            cell.border = border
            if cell.alignment is None:
                cell.alignment = Alignment(vertical="center")


def _summary_sheet(ws, data: dict) -> None:
    """Create the compact PMS bought/sold tracker with five columns only."""
    ws.sheet_view.showGridLines = False
    ws.freeze_panes = "B4"
    widths = {"A": 29, "B": 42, "C": 16, "D": 42, "E": 16}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width

    ws.merge_cells("A1:E1")
    ws["A1"] = "PMS SCRIP-WISE CHANGES TRACKER"
    ws["A1"].font = Font(bold=True, color=WHITE, size=12)
    ws["A1"].fill = PatternFill("solid", fgColor=NAVY)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    report_date = data.get("summary", {}).get("report_date", "")
    if isinstance(report_date, str):
        for date_format in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
            try:
                report_date = datetime.strptime(report_date, date_format).date()
                break
            except ValueError:
                continue
    ws["A2"] = report_date
    ws["A2"].number_format = "dd-mmm-yyyy"
    ws["A2"].font = Font(bold=True, italic=True, size=11, color=BLACK)
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")
    ws.merge_cells("B2:C2")
    ws.merge_cells("D2:E2")
    for cell, value in (("B2", "BOUGHT"), ("D2", "SOLD")):
        ws[cell] = value
        ws[cell].font = Font(bold=True, size=11)
        ws[cell].alignment = Alignment(horizontal="center", vertical="center")

    for cell, value in (
        ("A3", "PMS"),
        ("B3", "SCRIP"),
        ("C3", "% Change"),
        ("D3", "SCRIP"),
        ("E3", "% Change"),
    ):
        ws[cell] = value
        ws[cell].font = Font(bold=True, color=WHITE)
        ws[cell].fill = PatternFill("solid", fgColor=NAVY)
        ws[cell].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 20

    summary_sheet = next((item for item in data.get("sheets", []) if item.get("type") == "summary"), {})
    row_index = 4
    for row in summary_sheet.get("rows", []):
        if row.get("blank"):
            row_index += 1
            continue
        if row.get("section"):
            section_label = row.get("pms", "")
            if row.get("is_excluded_pms"):
                section_label = f"{section_label} — NOT RECEIVED (EXCLUDED)"
            elif row.get("is_new_pms"):
                section_label = f"{section_label} — NEW PMS (100% ADDED)"
            elif row.get("is_removed_pms"):
                section_label = f"{section_label} — NO REPORT (100% REMOVED)"
            ws.cell(row_index, 1, section_label)
            ws.cell(row_index, 1).font = Font(bold=True, size=10, color=BLACK)
            row_index += 1
            continue
        if row.get("review_notice"):
            ws.merge_cells(start_row=row_index, start_column=2, end_row=row_index, end_column=5)
            notice_text = str(row["review_notice"]).replace(
                "See the Review Required section below and this PMS worksheet.",
                "See this PMS worksheet and the Data Quality sheet.",
            )
            notice_cell = ws.cell(row_index, 2, notice_text)
            notice_cell.font = Font(italic=True, color=RED_TEXT, size=9)
            notice_cell.alignment = Alignment(wrap_text=True, vertical="center")
            ws.row_dimensions[row_index].height = 28
            row_index += 1
            continue

        ws.cell(row_index, 2, row.get("bought_security") or "")
        ws.cell(row_index, 3, _pct_ratio(row.get("bought_pct")))
        ws.cell(row_index, 4, row.get("sold_security") or "")
        ws.cell(row_index, 5, _pct_ratio(row.get("sold_pct")))
        ws.cell(row_index, 3).number_format = '0%;-0%;0%'
        ws.cell(row_index, 5).number_format = '0%;-0%;0%'
        if row.get("bought_security"):
            ws.cell(row_index, 3).fill = PatternFill("solid", fgColor=GREEN)
            ws.cell(row_index, 3).font = Font(color=GREEN_TEXT, bold=True)
        if row.get("sold_security"):
            ws.cell(row_index, 5).fill = PatternFill("solid", fgColor=RED)
            ws.cell(row_index, 5).font = Font(color=RED_TEXT, bold=True)
        row_index += 1

    end_row = max(3, row_index - 1)
    _apply_border(ws, 1, end_row, 1, 5)
    ws.auto_filter.ref = f"A3:E{end_row}"


def _history_sheet(ws, sheet: dict, *, summary: dict, consolidated: bool = False) -> None:
    """Write a monthly holdings matrix plus calculated DIFF and DIFF % columns."""
    ws.sheet_view.showGridLines = False
    columns = list(sheet.get("columns") or [])
    month_labels = list(sheet.get("month_keys") or columns[1:-2])
    rows = list(sheet.get("rows") or [])
    if not columns:
        columns = ["SECURITY", *month_labels, "DIFF", "DIFF %"]

    # Three fixed header rows reproduce the supplied tracker: PMS/portfolio name,
    # owner/comparison note, then SECURITY + reporting months + DIFF columns.
    ws.cell(1, 1, "ALL PMS - CONSOLIDATED HOLDINGS" if consolidated else sheet.get("display_name") or sheet.get("name", "PMS"))
    ws.cell(1, 1).font = Font(bold=True, size=11, color=BLACK)
    ws.cell(2, 1, "Combined reported holdings across PMS" if consolidated else sheet.get("comparison_note") or "Historical appraisal holdings")
    ws.cell(2, 1).font = Font(bold=True, size=9, color=BLACK)
    ws.cell(2, 1).alignment = Alignment(wrap_text=True, vertical="center")

    for column_index, label in enumerate(columns, start=1):
        cell = ws.cell(3, column_index, label)
        # The reference PMS tabs use a plain bold header, reserving the navy
        # banner for the Summary sheet and yellow for the latest month column.
        cell.font = Font(color=BLACK, bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 21

    month_column_by_label = {label: index + 2 for index, label in enumerate(month_labels)}
    latest_label = sheet.get("report_month")
    comparison_label = sheet.get("comparison_month")
    latest_col = month_column_by_label.get(latest_label)
    previous_col = month_column_by_label.get(comparison_label)
    diff_col = len(month_labels) + 2
    diff_pct_col = diff_col + 1

    row_metric_types: dict[int, str] = {}
    for row_index, row in enumerate(rows, start=4):
        ws.cell(row_index, 1, row.get("security_name", ""))
        row_metric = (row.get("measurement_type") or "QUANTITY").upper()
        row_metric_types[row_index] = row_metric
        values = row.get("values") or {}
        measurements = row.get("value_measurements") or {}
        for offset, label in enumerate(month_labels, start=2):
            value = _safe_number(values.get(label))
            metric = (measurements.get(label) or row_metric).upper()
            # Parser storage is percentage-points (e.g. 1.16 means 1.16%).
            # Excel percentage cells require the fractional representation 0.0116.
            workbook_value = value / 100.0 if value is not None and metric == "PERCENTAGE" else value
            cell = ws.cell(row_index, offset, workbook_value)
            if metric == "PERCENTAGE":
                cell.number_format = '0.00%;-0.00%;0.00%'
            else:
                cell.number_format = '#,##0;-#,##0;0'
            cell.alignment = Alignment(horizontal="right", vertical="center")
            if label == latest_label:
                cell.fill = PatternFill("solid", fgColor=YELLOW)

        # DIFF columns are formulas, not hard-coded values. The formula refers
        # to the most recent report for that PMS (or previous overall report for
        # the consolidated sheet), even when one or more months are absent.
        if latest_col and previous_col and latest_col <= len(month_labels) + 1 and previous_col <= len(month_labels) + 1:
            current_ref = f"{get_column_letter(latest_col)}{row_index}"
            previous_ref = f"{get_column_letter(previous_col)}{row_index}"
            comparable = row.get("diff") is not None and row_metric != "MIXED"
            if comparable:
                # A blank previous cell means the scrip had not yet appeared;
                # treat it as zero so its first buy is reflected in DIFF/DIFF %.
                diff_formula = f'=IF({current_ref}="","",{current_ref}-IF({previous_ref}="",0,{previous_ref}))'
                pct_formula = f'=IF({current_ref}="","",IF(IF({previous_ref}="",0,{previous_ref})=0,IF({current_ref}=0,0,1),{current_ref}/IF({previous_ref}="",0,{previous_ref})-1))'
                ws.cell(row_index, diff_col, diff_formula)
                ws.cell(row_index, diff_pct_col, pct_formula)
            else:
                ws.cell(row_index, diff_col, None)
                ws.cell(row_index, diff_pct_col, None)
        else:
            ws.cell(row_index, diff_col, None)
            ws.cell(row_index, diff_pct_col, None)

        if row_metric == "PERCENTAGE":
            # Month cells are written as true Excel percentages, so DIFF is
            # already a fractional percentage-point movement (e.g. 0.0081 = 0.81%).
            ws.cell(row_index, diff_col).number_format = '0.00%;-0.00%;0.00%'
        else:
            ws.cell(row_index, diff_col).number_format = '#,##0;-#,##0;0'
        ws.cell(row_index, diff_pct_col).number_format = '0%;-0%;0%'
        ws.cell(row_index, diff_col).alignment = Alignment(horizontal="right", vertical="center")
        ws.cell(row_index, diff_pct_col).alignment = Alignment(horizontal="right", vertical="center")

        ws.cell(row_index, 1).alignment = Alignment(vertical="center")
        for col_index in (diff_col, diff_pct_col):
            if col_index <= len(columns):
                ws.cell(row_index, col_index).border = Border(
                    left=THIN_DARK, right=THIN_DARK, top=THIN_DARK, bottom=THIN_DARK
                )

    last_row = max(3, 3 + len(rows))
    last_col = len(columns)
    ws.column_dimensions["A"].width = 43
    for column_index in range(2, last_col + 1):
        ws.column_dimensions[get_column_letter(column_index)].width = 14
    if diff_col <= last_col:
        ws.column_dimensions[get_column_letter(diff_col)].width = 14
        ws.column_dimensions[get_column_letter(diff_pct_col)].width = 13

    ws.freeze_panes = "B4"
    if last_row >= 3 and last_col >= 1:
        ws.auto_filter.ref = f"A3:{get_column_letter(last_col)}{last_row}"
        # Box the two calculated change columns like the reference workbook.
        for row_index in range(3, last_row + 1):
            for col_index in (diff_col, diff_pct_col):
                if col_index <= last_col:
                    ws.cell(row_index, col_index).border = Border(
                        left=THIN_DARK, right=THIN_DARK, top=THIN_DARK, bottom=THIN_DARK
                    )


def _quality_sheet(ws, data: dict) -> None:
    ws.sheet_view.showGridLines = False
    ws["A1"] = "DATA QUALITY / PROCESSING NOTES"
    ws["A1"].fill = PatternFill("solid", fgColor=NAVY)
    ws["A1"].font = Font(color=WHITE, bold=True)
    ws.column_dimensions["A"].width = 110
    for item in data.get("data_quality", []):
        ws.append([item])
    if not data.get("data_quality"):
        ws.append(["No data quality issues were reported for this run."])
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")


def _registry_sheet(ws, data: dict) -> None:
    ws.sheet_view.showGridLines = False
    headers = ["PMS", "Format", "Status", "First Seen", "Last Seen", "Last Processed", "Filename"]
    ws.append(headers)
    for item in data.get("registry", []):
        ws.append([
            item.get("pms_name", ""), item.get("format", ""), item.get("status", ""),
            item.get("first_seen_month", ""), item.get("last_seen_month", ""),
            item.get("last_processed_month", ""), item.get("last_filename", ""),
        ])
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.font = Font(color=WHITE, bold=True)
    widths = [32, 12, 16, 14, 14, 16, 52]
    for index, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(index)].width = width
    for row in ws.iter_rows(min_row=2):
        if row[2].value == "INACTIVE":
            row[2].fill = PatternFill("solid", fgColor=RED)
        elif row[2].value == "ACTIVE":
            row[2].fill = PatternFill("solid", fgColor=GREEN)
        elif row[2].value == "NOT_SEEN":
            row[2].fill = PatternFill("solid", fgColor=LIGHT_GRAY)
        elif row[2].value == "NOT_RECEIVED":
            row[2].fill = PatternFill("solid", fgColor="FCE4D6")
    _apply_border(ws, 1, ws.max_row, 1, ws.max_column)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"


def build_report_bytes(data: dict) -> bytes:
    workbook = Workbook()
    # Recalculate DIFF and DIFF % formulas when the user opens the download.
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.calculation.calcMode = "auto"
    summary_sheet = workbook.active
    summary_sheet.title = "Summary"
    _summary_sheet(summary_sheet, data)

    source_sheets = data.get("sheets", [])
    all_pms_data = next((sheet for sheet in source_sheets if sheet.get("type") == "all_pms"), None)
    if all_pms_data:
        all_pms_sheet = workbook.create_sheet("All PMS")
        _history_sheet(all_pms_sheet, all_pms_data, summary=data.get("summary", {}), consolidated=True)

    used_names = {"Summary", "All PMS"}
    for sheet in source_sheets:
        if sheet.get("type") != "pms":
            continue
        name = (sheet.get("name") or sheet.get("display_name") or "PMS")[:31]
        base = name
        suffix = 2
        while name in used_names:
            extra = f" {suffix}"
            name = f"{base[:31-len(extra)]}{extra}"
            suffix += 1
        used_names.add(name)
        ws = workbook.create_sheet(name)
        _history_sheet(ws, sheet, summary=data.get("summary", {}), consolidated=False)

    registry_data = next((sheet for sheet in source_sheets if sheet.get("type") == "registry"), None)
    if registry_data:
        registry_sheet = workbook.create_sheet("PMS Status")
        _registry_sheet(registry_sheet, data)
    quality_data = next((sheet for sheet in source_sheets if sheet.get("type") == "data_quality"), None)
    if quality_data:
        quality_sheet = workbook.create_sheet("Data Quality")
        _quality_sheet(quality_sheet, data)

    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()
