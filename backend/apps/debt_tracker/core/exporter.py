from __future__ import annotations

import os
import tempfile
from math import ceil
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .errors import DebtTrackerError, WorkbookOutputLimitError
from .engine import special_category_display_groups
from .models import ExposureResult
from .normalization import safe_excel_text


NAVY = "18212F"
WHITE = "FFFFFF"
TOTAL_BLUE = "EAF2F8"
CATEGORY_TEAL = "DDEFEA"
GRID = "D5DCE5"
# Normal-mode openpyxl cells have substantial Python-object overhead. Keeping
# the four styled sheets below 250k cells bounds workbook construction to a
# practical server memory envelope before ZIP-byte enforcement begins.
MAX_GENERATED_CELLS = 250_000


class _CappedWriter:
    """A seekable file wrapper that rejects writes beyond a hard byte ceiling."""

    def __init__(self, stream: BinaryIO, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("Workbook output size limit must be positive.")
        self._stream = stream
        self._max_bytes = max_bytes
        self._virtual_position = stream.tell()
        self._virtual_size = self._virtual_position
        self.exceeded = False

    def write(self, data: bytes) -> int:
        end = self._virtual_position + len(data)
        if self.exceeded or end > self._max_bytes:
            self.exceeded = True
            self._virtual_position = end
            self._virtual_size = max(self._virtual_size, end)
            return len(data)
        written = self._stream.write(data)
        self._virtual_position = self._stream.tell()
        self._virtual_size = max(self._virtual_size, self._virtual_position)
        return written

    def tell(self) -> int:
        return self._virtual_position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if not self.exceeded:
            position = self._stream.seek(offset, whence)
            self._virtual_position = position
            self._virtual_size = max(self._virtual_size, position)
            return position
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._virtual_position + offset
        elif whence == os.SEEK_END:
            position = self._virtual_size + offset
        else:
            raise ValueError("Unsupported seek mode.")
        if position < 0:
            raise ValueError("Negative seek position.")
        self._virtual_position = position
        return position

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def _style_sheet(sheet, widths: tuple[float, ...]) -> None:
    sheet.freeze_panes = "A2"
    sheet.sheet_view.showGridLines = False
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.font = Font(color=WHITE, bold=True)
        cell.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 24
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    thin = Side(style="thin", color=GRID)
    for row in sheet.iter_rows():
        for cell in row:
            cell.border = Border(bottom=thin)
            cell.alignment = Alignment(vertical="top", wrap_text=False)


def _mark_total(row) -> None:
    for cell in row:
        cell.fill = PatternFill("solid", fgColor=TOTAL_BLUE)
        cell.font = Font(bold=True)


def _source_text(reference):
    return f"{reference.file} · {reference.sheet}!{reference.cells}" if reference else "Not recorded"


def _holding_source_text(references):
    text = "; ".join(f"{_source_text(ref)} = {ref.value} × {ref.scale}" for ref in references)
    return text if len(text) <= 32000 else text[:31900] + " … Full source list is available in the dashboard breakdown."


def _base_sheet(workbook: Workbook, result: ExposureResult) -> None:
    sheet = workbook.active
    sheet.title = "BASE"
    sheet.append(
        [
            "Fund Name as per Broker",
            "Paper Name",
            "GROUP EXPOSURE",
            "Paper Maturity",
            "Fund Maturity",
            "Paper Amount in Cr",
            "% of Holding (Fund Level)",
            "Broker",
            "ZCB / MLD / NCD",
            "Prop Mkt Value",
            "Rating",
            "Issuer",
            "ISIN",
            "Category",
            "Portfolio allocation source",
            "Source allocation (%)",
            "Fund allocation total (%)",
            "Family fund value (Cr)",
            "Broker rating source cell",
            "Client classification source",
            "Grouping rule",
            "Holding value sources (rupees × scale)",
            "Asset",
            "Asset Type",
            "Verified security source",
            "Included in group movements",
        ]
    )
    for row in result.rows:
        sheet.append(
            [
                safe_excel_text(row.fund),
                safe_excel_text(row.paper),
                safe_excel_text(row.group),
                row.maturity,
                None,
                row.market_value_crore,
                row.allocation * 100,
                None,
                None,
                row.exposure,
                safe_excel_text(row.rating),
                safe_excel_text(row.issuer),
                safe_excel_text(row.isin),
                safe_excel_text(row.category),
                safe_excel_text(_source_text(row.source)),
                row.source.value if row.source else None,
                row.allocation_total * 100,
                row.holding_value,
                safe_excel_text(row.rating_cell),
                safe_excel_text(_source_text(row.classification_source)),
                safe_excel_text(row.mapping_source),
                safe_excel_text(_holding_source_text(row.holding_sources)),
                safe_excel_text(row.asset_class),
                safe_excel_text(row.asset_type),
                safe_excel_text(row.classification_evidence),
                "Yes" if row.movement_eligible else "No",
            ]
        )
    _style_sheet(sheet, (55, 65, 32, 15, 15, 18, 24, 16, 18, 18, 20, 55, 20, 25, 70, 22, 22, 22, 25, 65, 28, 100, 18, 28, 70, 22))
    for cell in sheet["D"][1:]:
        cell.number_format = "yyyy-mm-dd"
    for column in ("F", "J"):
        for cell in sheet[column][1:]:
            cell.number_format = "#,##0.000000"
    for cell in sheet["G"][1:]:
        cell.number_format = "0.000000"


def _sheet4(workbook: Workbook, result: ExposureResult) -> None:
    sheet = workbook.create_sheet("Sheet4")
    headers = ["GROUP EXPOSURE", "Category", "Paper Name", "Fund Name as per Broker",
               "Paper Maturity", "% of Holding (Fund Level)", "Total", "Rating"]
    sheet.append(headers)
    ordered = sorted(result.rows, key=lambda item: (item.group.casefold(), item.paper.casefold(), item.fund.casefold()))
    for row in ordered:
        display_group = row.group
        sheet.append([
            safe_excel_text(display_group), safe_excel_text(row.category), safe_excel_text(row.paper),
            safe_excel_text(row.fund), row.maturity, row.allocation * 100, row.exposure,
            safe_excel_text(row.reported_rating or row.rating),
        ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:H{sheet.max_row}"
    _style_sheet(sheet, (32, 26, 58, 42, 16, 22, 18, 20))
    for cell in sheet["E"][1:]:
        cell.number_format = "yyyy-mm-dd"
    for cell in sheet["F"][1:]:
        cell.number_format = "0.000000"
    for cell in sheet["G"][1:]:
        cell.number_format = "#,##0.000000"


def _sheet8(workbook: Workbook, result: ExposureResult) -> None:
    sheet = workbook.create_sheet("Sheet8")
    sheet.append(["Group", "Exposure", "Percentage", "Rating", "Paper Count", "Fund Count", "Added", "Deducted / Exited"])
    movement_rows = max(len(result.groups), len(result.added_groups), len(result.exited_groups))
    for index in range(movement_rows):
        group = result.groups[index] if index < len(result.groups) else None
        added = result.added_groups[index] if index < len(result.added_groups) else ""
        exited = result.exited_groups[index] if index < len(result.exited_groups) else ""
        sheet.append(
            [
                safe_excel_text(group.group) if group else None,
                group.exposure if group else None,
                group.percentage if group else None,
                safe_excel_text(group.rating) if group else None,
                group.paper_count if group else None,
                group.fund_count if group else None,
                safe_excel_text(added),
                safe_excel_text(exited),
            ]
        )
    for item in special_category_display_groups(result):
        sheet.append(
            [
                None,
                item["exposure"],
                item["percentage"],
                safe_excel_text(item["rating"]),
                None,
                None,
                None,
                None,
            ]
        )
    sheet.append(["Grand Total", result.total_exposure, 1 if result.total_exposure else 0, None, result.kpis["totalPapers"], result.kpis["totalFunds"], None, None])
    _mark_total(sheet[sheet.max_row])
    sheet.append([])
    sheet.append(["Category", "Exposure", "Percentage", "Limit", "Status", "Limit policy", None, None])
    category_header = sheet.max_row
    for cell in sheet[category_header]:
        cell.fill = PatternFill("solid", fgColor=CATEGORY_TEAL)
        cell.font = Font(bold=True)
    for item in result.limit_categories or result.categories:
        sheet.append(
            [
                item["category"],
                item["exposure"],
                item["percentage"],
                item["limit"],
                "BREACH" if item["breached"] else "OK",
                item.get("limitSource", ""),
                None,
                None,
            ]
        )
    _style_sheet(sheet, (48, 20, 16, 30, 14, 14, 52, 52))
    for row_index in range(2, movement_rows + 2):
        movement_cells = (sheet.cell(row_index, 1), sheet.cell(row_index, 7), sheet.cell(row_index, 8))
        for cell in movement_cells:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        if any(len(str(cell.value or "")) > 44 for cell in movement_cells):
            sheet.row_dimensions[row_index].height = 30
    for cell in sheet["B"][1:]:
        cell.number_format = "#,##0.000000"
    for column in ("C", "D"):
        for cell in sheet[column][1:]:
            cell.number_format = "0.0000%"


def _validation(workbook: Workbook, result: ExposureResult) -> None:
    sheet = workbook.create_sheet("Validation")
    sheet.append(["Check", "Generated", "Reference", "Difference", "Status", "Notes"])
    special_display_rows = special_category_display_groups(result)
    special_exposure = sum(item["exposure"] for item in special_display_rows)
    special_percentage = sum(item["percentage"] for item in special_display_rows)
    review_action_count = sum(issue.severity == "REVIEW" for issue in result.review_issues)
    review_info_count = sum(issue.severity == "INFO" for issue in result.review_issues)
    # Each check carries two notes: what OK means, and — when the check is flagged —
    # what actually happened and what the reader should do about it.
    send_back = "This points to a problem in the report itself. Please send it back to us before using these figures."
    checks = (
        (
            "BASE exposure total",
            result.total_exposure,
            sum(row.exposure for row in result.rows),
            "Every individual bond row adds up to the reported total.",
            f"The individual bond rows do not add up to the reported total. {send_back}",
        ),
        (
            "Holdings coverage",
            result.total_exposure,
            result.kpis["totalInvestment"],
            "Every debt fund you hold is included in the exposure figure.",
            "Part of your debt-fund value is not included in the exposure figure. The Difference column shows how much, so the exposure total understates your real holdings by that amount. See the Review sheet for the funds involved.",
        ),
        (
            "Unmatched fund exposure",
            result.kpis["unmatchedExposure"],
            0,
            "No holding was left out of the calculation.",
            "This much fund value was left out because we could not see which bonds those funds hold. Send us the broker portfolio statement for the funds named on the Review sheet, then ask for the report to be regenerated.",
        ),
        (
            "Group exposure total",
            sum(row.exposure for row in result.groups) + special_exposure,
            result.total_exposure,
            "The group summary matches the detailed bond rows.",
            f"The group summary does not match the detailed bond rows. {send_back}",
        ),
        (
            "Group percentage",
            sum(row.percentage for row in result.groups) + special_percentage,
            1 if result.total_exposure else 0,
            "The group shares add up to 100%.",
            f"The group shares do not add up to 100%. {send_back}",
        ),
        (
            "Distinct funds",
            result.kpis["totalFunds"],
            len(result.fund_allocations),
            "Each fund was counted exactly once.",
            f"A fund may have been counted more than once, which would overstate exposure. {send_back}",
        ),
        (
            "Review queue",
            result.kpis["reviewActionCount"],
            review_action_count,
            "The Review sheet contains every recorded action item.",
            f"The Review sheet may not be showing every item that needs action. {send_back}",
        ),
        (
            "Information notes",
            result.kpis["reviewIssueCount"] - result.kpis["reviewActionCount"],
            review_info_count,
            "Any estimates used are disclosed on the Review sheet.",
            f"The Review sheet may not be listing every estimate that was used. {send_back}",
        ),
        (
            "Unmatched funds",
            result.kpis["unmatchedFundCount"],
            0,
            "Every fund was matched to its broker portfolio data.",
            "This many funds could not be matched to broker portfolio data, so their bonds are not reflected in the exposure figures. They are named on the Review sheet with the action needed.",
        ),
    )
    for label, generated, reference, ok_notes, review_notes in checks:
        difference = float(generated) - float(reference)
        status = "OK" if abs(difference) <= 1e-9 else "REVIEW"
        sheet.append([label, generated, reference, difference, status, ok_notes if status == "OK" else review_notes])
    _style_sheet(sheet, (28, 20, 20, 18, 14, 52))
    for row in sheet.iter_rows(min_row=2):
        if row[4].value == "OK":
            row[4].fill = PatternFill("solid", fgColor="D9EAD3")
        elif row[4].value == "REVIEW":
            row[4].fill = PatternFill("solid", fgColor="FFF2CC")


def _review(workbook: Workbook, result: ExposureResult) -> None:
    sheet = workbook.create_sheet("Review")
    sheet.append(["Severity", "Issue Type", "Fund", "Paper", "Exposure (Cr)", "Source / Candidate", "Reason", "Required Action"])
    for issue in result.review_issues:
        sheet.append(
            [
                safe_excel_text(issue.severity),
                safe_excel_text(issue.issue_type),
                safe_excel_text(issue.fund),
                safe_excel_text(issue.paper),
                issue.exposure,
                safe_excel_text(issue.source),
                safe_excel_text(issue.reason),
                safe_excel_text(issue.action),
            ]
        )
    widths = (12, 28, 48, 52, 18, 38, 68, 68)
    _style_sheet(sheet, widths)
    for row in sheet.iter_rows(min_row=2):
        row[4].number_format = "#,##0.000000"
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        # The reason and action columns carry full sentences, so a fixed height clips them.
        # Size each row to whichever cell needs the most wrapped lines.
        wrapped_lines = max(ceil(len(str(cell.value or "")) / width) for cell, width in zip(row, widths))
        sheet.row_dimensions[row[0].row].height = max(42, wrapped_lines * 14)
        if row[0].value == "REVIEW":
            row[0].fill = PatternFill("solid", fgColor="FFF2CC")
        elif row[0].value == "INFO":
            row[0].fill = PatternFill("solid", fgColor="DDEBF7")


def _report_info_rows(result: ExposureResult):
    meta = result.metadata
    return (
        ("Report as of", meta.get("reportDate", "Not recorded")),
        ("Opening as of", meta.get("baselineDate") or "Unknown / not labelled"),
        ("Movement meaning", "Added/Exited compare group presence between the mapping snapshot and current report. Exposure changes are not bought/sold amounts; transaction data is required for trades."),
        ("Movement scope", meta.get("movementScope", "Not recorded")),
        ("Opening group exposure (Cr)", meta.get("movementOpeningExposure", 0)),
        ("Closing group exposure (Cr)", meta.get("movementClosingExposure", 0)),
        ("Exposure formula", "Family fund value × source allocation / fund allocation total. BASE includes portfolio, holding and classification source references."),
        ("Opening sheet", meta.get("baselineSheet") or "Unavailable"),
        ("Opening comparison", "Opening papers regrouped using the current mapping rules"
         if meta.get("baselineComparison") == "reclassified-details"
         else "Opening summary only; group changes can include classification changes"),
        ("Mapping base sheet", meta.get("baseSheet", "")),
        ("Opening exposure (Cr)", meta.get("baselineExposure", 0)),
        ("Opening securities needing group confirmation", meta.get("openingMappingGapCount", 0)),
        ("Opening exposure needing group confirmation (Cr)", meta.get("openingUnmappedExposure", 0)),
        ("Scope", "Selected debt mutual fund holdings; excludes direct bonds and other asset sections."),
        ("Category limit policy", meta.get("limitPolicy", "standard")),
        ("Category limit policy version", meta.get("limitPolicyVersion", 1)),
        ("Limit assessment", "Uses covered exposure. Missing portfolios can change concentration and alerts."),
        ("Effective category limits", "; ".join(f"{key}: {value:.2%}" for key, value in
                                              meta.get("effectiveCategoryLimits", {}).items())),
        ("Holdings value scales", ", ".join(str(v) for v in meta.get("holdingsScales", ()))),
        ("Validation basis", meta.get("validationBasis", "Internal totals only")),
        *(("ETF estimate", f"{item['fund']} uses {item['etf']} ({item['source']}); wrapper cash and expenses are unknown.")
          for item in meta.get("estimatedEtfPortfolios", ())),
        *(("Correction setting", f"{item['issuer_key']}: group={item['group'] or 'unchanged'}; category={item['rating_bucket'] or 'per paper'}")
          for item in meta.get("appliedOverrides", ())),
        *(("Automatic mapping", f"{item['isin']} ({item['reportDate'] or 'undated opening'}): {item['status']}; {item.get('group', '')}; {item['reason']}")
          for item in meta.get('aiGeneration', {}).get('outcomes', ())),
        *(("Automatic mapping evidence", f"{item['isin']} | {item.get('legalEntity', '')} | {item.get('role', '')} | {item.get('resolverVersion', '')} | {source.get('document_date', '')} | {source.get('url', '')} | {source.get('quote', '')}")
          for item in meta.get('aiGeneration', {}).get('outcomes', ()) for source in item.get('evidence', ())),
    )


def _report_info(workbook: Workbook, result: ExposureResult) -> None:
    sheet = workbook.create_sheet("Report Info")
    sheet.append(["Setting", "Value"])
    for label, value in _report_info_rows(result):
        sheet.append([label, value if isinstance(value, (int, float)) else safe_excel_text(value)])
    _style_sheet(sheet, (30, 110))
    for row in sheet.iter_rows(min_row=2):
        row[1].alignment = Alignment(vertical="top", wrap_text=True)
        sheet.row_dimensions[row[0].row].height = max(30, ceil(len(str(row[1].value or "")) / 95) * 16)


def _generated_cell_count(result: ExposureResult) -> int:
    base_cells = (len(result.rows) + 1) * 26
    sheet4_rows = len(result.rows) + len({row.group if row.group != "Others" else row.category for row in result.rows}) + 2
    sheet4_cells = sheet4_rows * 6
    movement_rows = max(len(result.groups), len(result.added_groups), len(result.exited_groups))
    special_category_count = len(special_category_display_groups(result))
    sheet8_rows = movement_rows + special_category_count + len(result.limit_categories or result.categories) + 4
    sheet8_cells = sheet8_rows * 8
    validation_cells = 10 * 6
    review_cells = (len(result.review_issues) + 1) * 8
    return base_cells + sheet4_cells + sheet8_cells + validation_cells + review_cells + (len(_report_info_rows(result)) + 1) * 2


def export_workbook(result: ExposureResult, path: str | Path, *, max_bytes: int | None = None) -> Path:
    if _generated_cell_count(result) > MAX_GENERATED_CELLS:
        raise WorkbookOutputLimitError("The generated workbook exceeds the cell safety limit.")
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    workbook = Workbook()
    try:
        _base_sheet(workbook, result)
        _sheet4(workbook, result)
        _sheet8(workbook, result)
        _validation(workbook, result)
        _review(workbook, result)
        _report_info(workbook, result)
        workbook.calculation.fullCalcOnLoad = True
        workbook.calculation.forceFullCalc = True
        with temporary.open("w+b") as stream:
            output = _CappedWriter(stream, max_bytes) if max_bytes is not None else stream
            workbook.save(output)
            if isinstance(output, _CappedWriter) and output.exceeded:
                raise WorkbookOutputLimitError("The generated workbook exceeds the configured output size limit.")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(destination)
        return destination
    except DebtTrackerError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        # A successfully computed ExposureResult can still fail to serialize (an
        # unwritable style, an openpyxl internal limit, a disk error) well after every
        # curated check above has passed. Without this boundary that raw exception
        # reaches the client as an unclassifiable fragment instead of an actionable
        # message, even though nothing about the *input* workbooks was at fault.
        temporary.unlink(missing_ok=True)
        raise DebtTrackerError(
            "The workbook could not be generated. Try again; if this keeps happening, contact support."
        ) from exc
    finally:
        workbook.close()


def export_single_sheet_stream(path: str | Path, sheet_name: str, *, max_bytes: int) -> BinaryIO:
    if sheet_name not in {"Sheet4", "Sheet8"}:
        raise ValueError("Only Sheet4 and Sheet8 can be downloaded separately.")
    try:
        source = load_workbook(path, data_only=False, keep_links=False)
    except Exception as exc:
        raise DebtTrackerError(
            "This sheet could not be exported. Try again; if this keeps happening, contact support."
        ) from exc
    stream: BinaryIO = tempfile.SpooledTemporaryFile(max_size=min(max_bytes, 2 * 1024 * 1024), mode="w+b")
    try:
        output = Workbook()
        target = output.active
        target.title = sheet_name
        original = source[sheet_name]
        for row in original.iter_rows():
            target.append([cell.value for cell in row])
        target.freeze_panes = original.freeze_panes
        target.sheet_view.showGridLines = False
        _style_sheet(target, tuple(original.column_dimensions[get_column_letter(index)].width or 12 for index in range(1, original.max_column + 1)))
        capped = _CappedWriter(stream, max_bytes)
        output.save(capped)
        output.close()
        if capped.exceeded:
            raise WorkbookOutputLimitError("The generated workbook exceeds the configured output size limit.")
        stream.seek(0)
        return stream
    except DebtTrackerError:
        stream.close()
        raise
    except Exception as exc:
        stream.close()
        raise DebtTrackerError(
            "This sheet could not be exported. Try again; if this keeps happening, contact support."
        ) from exc
    finally:
        source.close()


def export_single_sheet(path: str | Path, sheet_name: str) -> bytes:
    stream = export_single_sheet_stream(path, sheet_name, max_bytes=100 * 1024 * 1024)
    try:
        return stream.read()
    finally:
        stream.close()
