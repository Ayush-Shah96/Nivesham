from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from dataclasses import replace
from math import isfinite
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Sequence

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula, DataTableFormula

from .errors import WorkbookSchemaError
from .models import BrokerPosition, ClassificationHint, Holding, MasterData, OpeningPosition, PeriodComparison, SourceReference, frozen_mapping
from .normalization import canonical_broker_name, canonical_group_name, normalize_fund_name, normalize_text


MAX_ROWS = 100_000
MAX_COLUMNS = 250
MAX_SHEETS = 50
MAX_TOTAL_CELLS = 5_000_000
MAX_WORKBOOK_CELL_TEXT_CHARACTERS = 4_096
MAX_SCOPE_TEXT_CHARACTERS = 512
MAX_NUMERIC_OR_DATE_TEXT_CHARACTERS = 128
MAX_FORMULA_TEXT_CHARACTERS = MAX_WORKBOOK_CELL_TEXT_CHARACTERS
MAX_CONSUMED_TEXT_CHARACTERS = 5_000_000
MAX_HEADER_SCAN_CHARACTERS = 2_000_000
MAX_RAW_IDENTIFIER_CHARACTERS = 512
MAX_NORMALIZED_IDENTIFIER_CHARACTERS = 256
MAX_MASTER_GROUP_WORK_UNITS = 500_000
# Fuzzy canonicalization is super-linear in label length; the live workbook has
# grown to 201 groups (issuer-level entries are now tracked individually, not
# just broad categories), so 300 keeps headroom above that without admitting
# large adversarial SequenceMatcher sets. Verified against the current live
# workbook: parses in well under a second, far inside MAX_MASTER_GROUP_WORK_UNITS.
MAX_DISTINCT_HISTORICAL_GROUPS = 300
_CANONICAL_GROUP_SCAN_FACTOR = 4


def _source_name(path, name=None):
    value = name if name is not None else Path(path).name
    if not isinstance(value, str) or len(value) > 512 or "\x00" in value:
        raise WorkbookSchemaError("The source workbook name is invalid.")
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _coordinate(index, row):
    return f"{get_column_letter(index + 1)}{row}" if index is not None else ""


class _WorkbookTextGuard:
    __slots__ = ("consumed_characters", "header_scan_characters", "rows_scanned")

    def __init__(self) -> None:
        self.consumed_characters = 0
        self.header_scan_characters = 0
        self.rows_scanned = 0

    def scan_row(self) -> None:
        # Row iteration is bounded by MAX_ROWS/MAX_COLUMNS rather than a sheet's
        # self-declared <dimension> (some exporters, e.g. ICRA Analytics' portfolio
        # downloads, write an inaccurate one that undersells the real extent, which
        # would otherwise make openpyxl's read-only mode silently truncate real data).
        # This counter is the real enforcement of the total-cell safety budget now
        # that the declared dimension can no longer be trusted for that purpose.
        self.rows_scanned += 1
        if self.rows_scanned * MAX_COLUMNS > MAX_TOTAL_CELLS:
            raise WorkbookSchemaError("The workbook expands beyond the safe processing limit.")

    def text(
        self,
        value: object,
        *,
        max_characters: int = MAX_WORKBOOK_CELL_TEXT_CHARACTERS,
        header_scan: bool = False,
        strip: bool = False,
    ) -> str:
        if value is None:
            return ""
        text = value if isinstance(value, str) else str(value)
        character_count = len(text)
        if character_count > max_characters:
            raise WorkbookSchemaError("Workbook text safety limit exceeded for a single cell.")

        self.consumed_characters += character_count
        if self.consumed_characters > MAX_CONSUMED_TEXT_CHARACTERS:
            raise WorkbookSchemaError("Workbook exceeds the cumulative workbook text safety budget.")
        if header_scan:
            self.header_scan_characters += character_count
            if self.header_scan_characters > MAX_HEADER_SCAN_CHARACTERS:
                raise WorkbookSchemaError("Workbook exceeds the cumulative header-scan text safety budget.")
        return text.strip() if strip else text

    def normalize(
        self,
        value: object,
        *,
        max_characters: int = MAX_WORKBOOK_CELL_TEXT_CHARACTERS,
        max_normalized_characters: int = MAX_WORKBOOK_CELL_TEXT_CHARACTERS,
        header_scan: bool = False,
    ) -> str:
        text = self.text(value, max_characters=max_characters, header_scan=header_scan)
        normalized = normalize_text(text)
        if len(normalized) > max_normalized_characters:
            raise WorkbookSchemaError("Workbook text safety limit exceeded after normalization.")
        return normalized

    def identifier(
        self,
        value: object,
        *,
        normalizer: Callable[[object], str] | None = None,
    ) -> tuple[str, str]:
        text = self.text(value, max_characters=MAX_RAW_IDENTIFIER_CHARACTERS, strip=True)
        normalized = (normalizer or normalize_text)(text)
        if len(normalized) > MAX_NORMALIZED_IDENTIFIER_CHARACTERS:
            raise WorkbookSchemaError("Workbook identifier or label length exceeds the supported safety limit.")
        return text, normalized


def _is_formula(value: object) -> bool:
    return isinstance(value, (ArrayFormula, DataTableFormula)) or (isinstance(value, str) and value.startswith("="))


def _guard_formula_object(value: object, text_guard: _WorkbookTextGuard) -> None:
    if isinstance(value, ArrayFormula):
        text_guard.text(value.text, max_characters=MAX_FORMULA_TEXT_CHARACTERS)
    elif isinstance(value, DataTableFormula):
        text_guard.text(value.ref, max_characters=MAX_FORMULA_TEXT_CHARACTERS)


def _number(value: object, text_guard: _WorkbookTextGuard) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = text_guard.text(value, max_characters=MAX_NUMERIC_OR_DATE_TEXT_CHARACTERS)
        if value.strip() in {"", "--"}:
            return None
    if _is_formula(value):
        _guard_formula_object(value, text_guard)
        raise WorkbookSchemaError(
            "Formula-backed numeric cells are not supported. Replace formulas with their calculated numeric values before uploading."
        )
    if isinstance(value, bool):
        raise WorkbookSchemaError("Workbook numeric cells must contain a valid number or a supported blank value.")
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("₹", "").strip()
        if cleaned.endswith("%"):
            cleaned = cleaned[:-1]
        try:
            number = float(cleaned)
        except ValueError as exc:
            raise WorkbookSchemaError(
                "Workbook numeric cells must contain a valid number or a supported blank value."
            ) from exc
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise WorkbookSchemaError(
                "Workbook numeric cells must contain a valid number or a supported blank value."
            ) from exc
    if not isfinite(number):
        raise WorkbookSchemaError("Workbook numeric values must be finite.")
    return number


def _number_from_cached_formula(
    value: object,
    cached_value: object,
    text_guard: _WorkbookTextGuard,
) -> float | None:
    """Accept a formula only when the XLSX also contains a finite numeric cache."""
    if isinstance(value, str) and value.startswith("="):
        value = text_guard.text(value, max_characters=MAX_FORMULA_TEXT_CHARACTERS)
    if not _is_formula(value):
        return _number(value, text_guard)
    _guard_formula_object(value, text_guard)
    number = _number(cached_value, text_guard)
    if number is None:
        raise WorkbookSchemaError(
            "Formula-backed numeric cells require a cached numeric value. Recalculate the workbook and save it before uploading."
        )
    return number


def _date(value: object, text_guard: _WorkbookTextGuard) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    text = text_guard.text(value, max_characters=MAX_NUMERIC_OR_DATE_TEXT_CHARACTERS, strip=True)
    if not text or text == "--":
        return None
    # Perpetual instruments (AT1 bonds and similar) legitimately have no maturity date.
    if normalize_text(text) in {"perpetual", "perp", "na", "nil", "none"}:
        return None
    # Day-first formats only. An ambiguous MM/DD source would otherwise parse silently and
    # wrongly for any day of the month <= 12, so unrecognised text is left blank instead.
    for pattern in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _header_map(row: Sequence[object], text_guard: _WorkbookTextGuard) -> dict[str, int]:
    # First occurrence wins on a duplicate header. Dual-period comparison exports
    # (a broker's current-period column block followed by a prior-period block
    # reusing the same header text, e.g. "Holding(%)" twice) rely on this: the
    # first block is the report's primary "as on" period, and a position newly
    # added this period leaves the prior-period block blank. Letting the later
    # occurrence win instead reads that blank as a missing allocation for what
    # is actually a valid current-period holding.
    headers: dict[str, int] = {}
    for index, value in enumerate(row):
        if not isinstance(value, str):
            continue
        normalized = text_guard.normalize(value, header_scan=True)
        if normalized and normalized not in headers:
            headers[normalized] = index
    return headers


def _find_header(
    worksheet,
    required: tuple[set[str], ...],
    text_guard: _WorkbookTextGuard,
    max_scan: int = 100,
) -> tuple[int, dict[str, int]] | None:
    # max_row/max_col intentionally ignore worksheet.max_row/max_column: those are
    # read from the sheet's self-declared <dimension>, which some exporters get
    # wrong (see _bounded_rows below), and trusting it here would mean the header
    # row is never even reached for an affected file.
    for row_number, row in enumerate(
        worksheet.iter_rows(min_row=1, max_row=max_scan, max_col=MAX_COLUMNS, values_only=True), 1
    ):
        headers = _header_map(row, text_guard)
        if all(any(candidate in headers for candidate in alternatives) for alternatives in required):
            return row_number, headers
    return None


def _bounded_rows(sheet, text_guard: _WorkbookTextGuard, *, min_row: int = 1, count: bool = True):
    """Iterate a worksheet's data rows without trusting its self-declared <dimension>.

    openpyxl's read-only mode derives max_row/max_column from that tag by default;
    an exporter that writes an inaccurate one (seen from ICRA Analytics' portfolio
    downloads, which declare only cell A1 as used on sheets that actually hold
    hundreds of real rows) makes the sheet read back as empty. Bounding explicitly
    by MAX_ROWS/MAX_COLUMNS instead reads what is actually there — those are
    already the workbook's own admitted worst-case ceiling per sheet, so this
    can't admit more than an honestly-dimensioned maximal sheet already could.
    `count` feeds the real observed row volume into the text guard's running
    safety budget, since the declared dimension can no longer be trusted for
    that purpose either; pass count=False for a second, zipped iterator over the
    same rows (e.g. a cached-formula-values pass) to avoid double-counting.
    """
    for row in sheet.iter_rows(min_row=min_row, max_row=MAX_ROWS, max_col=MAX_COLUMNS, values_only=True):
        if count:
            text_guard.scan_row()
        yield row


def _pick(headers: dict[str, int], *names: str) -> int | None:
    for name in names:
        if name in headers:
            return headers[name]
    return None


def _cell(row: Sequence[object], index: int | None) -> object:
    return row[index] if index is not None and index < len(row) else None


def _reserve_master_group_work(current: int, additional: int) -> int:
    updated = current + additional
    if updated > MAX_MASTER_GROUP_WORK_UNITS:
        raise WorkbookSchemaError(
            "Workbook exceeds the group-canonicalization safety budget. "
            "Reduce the number of historical groups or mapping rows."
        )
    return updated


def _canonical_scan_work(call_count: int, historical_group_count: int) -> int:
    return call_count * historical_group_count * _CANONICAL_GROUP_SCAN_FACTOR


def _growing_canonical_scan_work(call_count: int) -> int:
    historical_group_pairs = call_count * (call_count - 1) // 2
    return historical_group_pairs * _CANONICAL_GROUP_SCAN_FACTOR


def _load(path: str | Path, *, data_only: bool = False):
    try:
        # Formula text is intentionally retained so hostile formula-like labels can be
        # escaped on export instead of disappearing through cached-value loading.
        workbook = load_workbook(path, read_only=True, data_only=data_only, keep_links=False)
    except Exception as exc:
        raise WorkbookSchemaError("The workbook could not be opened safely.") from exc
    if len(workbook.worksheets) > MAX_SHEETS:
        workbook.close()
        raise WorkbookSchemaError("The workbook contains too many worksheets.")
    # No upfront max_row/max_column/total-cell check here: those would come from
    # each sheet's self-declared <dimension>, which real exporters get wrong in
    # both directions (ICRA Analytics undersells it; Excel itself oversells it
    # once formatting is applied past the real data — e.g. a 10-column sheet
    # whose dimension claims 16,382 columns because a fill/border was dragged
    # across the row). Rejecting on that number throws out legitimate small
    # workbooks. The real caps are enforced where rows are actually read:
    # iter_rows is bounded by MAX_ROWS/MAX_COLUMNS in _bounded_rows and
    # _find_header, and _WorkbookTextGuard.scan_row enforces MAX_TOTAL_CELLS
    # against rows actually scanned.
    return workbook


def _sheet_named(workbook, expected: str):
    normalized = expected.strip().casefold()
    return next(
        (sheet for sheet in workbook.worksheets if sheet.title.strip().casefold() == normalized),
        None,
    )


# Crystal-Reports-style multi-section transaction ledgers (see parse_holdings) label some
# sections "debt" that aren't actually pure debt mutual funds -- a real client file's
# "Hybrid Open Ended Debt MF" section held Parag Parikh Dynamic Asset Allocation Fund and
# Edelweiss Multi Asset Allocation Fund, genuinely hybrid/multi-asset funds, not debt. This
# allow-list reflects what each section was actually found to hold, not a guess from its
# label -- do not "fix" it later by pattern-matching section titles containing "debt".
# Liquid funds are excluded too, for consistency with the generic MProfit-style path above,
# which only accepts the "Debt Funds"/"Debt Fund" subcategory, not "Liquid".
_LEDGER_DEBT_SECTIONS = {
    "open ended debt mtual fund",
    # normalize_text strips punctuation, so the two entries below are the normalized
    # forms of "Open Ended Debt Mutual Fund - New" and "Fixed Maturity Plans (FMP)" --
    # verified directly (normalize_text("Fixed Maturity Plans (FMP)") ==
    # "fixed maturity plans fmp"), not the section labels' own literal punctuation.
    "open ended debt mutual fund new",
    "fixed maturity plans fmp",
}


def _ledger_section_title(row: Sequence[object], name_index: int, text_guard: _WorkbookTextGuard) -> str | None:
    """Return a ledger row's section title, or None if this isn't a section-header row.

    A section-header row (e.g. "OPEN ENDED DEBT MTUAL FUND") carries text in the first
    column, with the Fund Name column blank -- unlike a real transaction row, which always
    has a fund name. Checking only these two columns (rather than requiring every other
    column blank) is deliberate: a real file had a stray leftover value in a far column of
    an otherwise-blank title row, which a stricter all-other-columns-blank check would have
    missed entirely.
    """
    first = row[0] if row else None
    if first is None or (isinstance(first, str) and not first.strip()):
        return None
    name_cell = _cell(row, name_index)
    if name_cell is not None and not (isinstance(name_cell, str) and not name_cell.strip()):
        return None
    return text_guard.text(first, strip=True)


def _accumulate_holding(
    name: str,
    value_cell: object,
    cached_value_cell: object,
    sheet_title: str,
    layout: str,
    text_guard: _WorkbookTextGuard,
    aggregated: dict[str, tuple[str, float]],
    seen_on_sheet: dict[str, str],
    layout_by_key: dict[str, str],
    value_scale: float = 1.0,
    sources: dict | None = None,
    source: SourceReference | None = None,
) -> None:
    name, key = text_guard.identifier(name, normalizer=normalize_fund_name)
    value = _number_from_cached_formula(value_cell, cached_value_cell, text_guard)
    if value is None:
        raise WorkbookSchemaError("A closing valuation is required for every debt mutual fund holding.")
    if value <= 0:
        return
    if not key:
        return
    # Rows are summed within a worksheet (one fund can span several folios), but the same
    # fund appearing on two worksheets -- a consolidated tab plus a per-member tab, say --
    # would be silently double counted.
    previous_sheet = seen_on_sheet.setdefault(key, sheet_title)
    if previous_sheet != sheet_title:
        raise WorkbookSchemaError(
            f"The holding '{name}' appears on both the '{previous_sheet}' and '{sheet_title}' "
            "worksheets. Adding both together would count it twice and overstate exposure. "
            "Please supply a holdings workbook that lists each fund on one worksheet only."
        )
    display, existing = aggregated.get(key, (name, 0.0))
    aggregated[key] = (display, existing + value * value_scale / 10_000_000)
    layout_by_key.setdefault(key, layout)
    if sources is not None and source is not None:
        sources[key] = (*sources.get(key, ()), replace(source, value=value, scale=value_scale))


def _ledger_value_scale(row, cached_row, headers, text_guard) -> float:
    quantity_index = _pick(headers, "unit shares", "units", "quantity")
    nav_index = _pick(headers, "cur nav", "current nav", "nav")
    if quantity_index is None or nav_index is None:
        return 1.0  # Older ledgers explicitly store rupees and have no unit controls.
    values = [
        _number_from_cached_formula(_cell(row, i), _cell(cached_row, i), text_guard)
        for i in (quantity_index, nav_index, headers["cur value"])
    ]
    units, nav, stored = values
    if stored is not None and stored <= 0:
        return 0.0
    if units is None or nav is None or stored is None or units <= 0 or nav <= 0:
        raise WorkbookSchemaError("Holdings value scale cannot be verified: units, NAV and value are required.")
    rupees = units * nav
    if not isfinite(rupees):
        raise WorkbookSchemaError("Holdings value scale exceeds the supported numeric range.")
    for scale in (1.0, 100.0):
        if abs(stored * scale - rupees) <= max(0.000001, rupees * 0.0001):
            return scale
    raise WorkbookSchemaError("Holdings value scale does not reconcile with units × NAV. Check the source values.")


def parse_holdings(path: str | Path, *, source_name=None) -> tuple[Holding, ...]:
    filename = _source_name(path, source_name)
    workbook = _load(path)
    cached_workbook = _load(path, data_only=True)
    text_guard = _WorkbookTextGuard()
    aggregated: dict[str, tuple[str, float]] = {}
    seen_on_sheet: dict[str, str] = {}
    layout_by_key: dict[str, str] = {}
    scales_by_sheet: dict[str, float] = {}
    sources: dict[str, tuple[SourceReference, ...]] = {}
    try:
        for sheet in workbook.worksheets:
            cached_sheet = cached_workbook[sheet.title]
            found = _find_header(
                sheet,
                (
                    {"asset name", "scheme name", "fund name"},
                    {"closing valuation", "current value", "market value"},
                ),
                text_guard,
                150,
            )
            if found:
                header_row, headers = found
                name_index = _pick(headers, "asset name", "scheme name", "fund name")
                value_index = _pick(headers, "closing valuation", "current value", "market value")
                category_index = _pick(headers, "category", "asset category")
                subcategory_index = _pick(headers, "sub category", "subcategory")
                asset_class_index = _pick(headers, "asset class", "type")
                folio_index = _pick(headers, "folio ref no", "folio", "folio no", "folio number", "reference no")
                rows = _bounded_rows(sheet, text_guard, min_row=header_row + 1)
                cached_rows = _bounded_rows(cached_sheet, text_guard, min_row=header_row + 1, count=False)
                for row_number, (row, cached_row) in enumerate(zip(rows, cached_rows), header_row + 1):
                    name = text_guard.text(_cell(row, name_index), strip=True)
                    if not name:
                        continue
                    if folio_index is not None and not text_guard.text(_cell(row, folio_index), strip=True):
                        # Portfolio-summary exports (e.g. MProfit) that lack Category/Sub
                        # Category/Asset Class columns interleave report rollups ("Net
                        # Worth", a per-category subtotal) and a trailing "Powered by
                        # MProfit" footer into the same Asset Name/Current Value columns
                        # as real holdings. Every genuine holding carries a folio/
                        # reference number; those rollup and footer rows don't, so a
                        # blank folio is what distinguishes them here. Without this,
                        # the rollups get summed in on top of the real per-fund total
                        # (observed: reported total came out 3x actual) and the footer
                        # row's missing valuation trips "closing valuation is required"
                        # and blocks the import outright.
                        continue
                    category = text_guard.normalize(
                        _cell(row, category_index),
                        max_characters=MAX_SCOPE_TEXT_CHARACTERS,
                        max_normalized_characters=MAX_NORMALIZED_IDENTIFIER_CHARACTERS,
                    )
                    subcategory = text_guard.normalize(
                        _cell(row, subcategory_index),
                        max_characters=MAX_SCOPE_TEXT_CHARACTERS,
                        max_normalized_characters=MAX_NORMALIZED_IDENTIFIER_CHARACTERS,
                    )
                    asset_class = text_guard.normalize(
                        _cell(row, asset_class_index),
                        max_characters=MAX_SCOPE_TEXT_CHARACTERS,
                        max_normalized_characters=MAX_NORMALIZED_IDENTIFIER_CHARACTERS,
                    )
                    if category_index is not None and category != "debt":
                        continue
                    if subcategory_index is not None and subcategory not in {"debt funds", "debt fund"}:
                        continue
                    if asset_class_index is not None and asset_class not in {"mutual funds", "mutual fund"}:
                        continue
                    _accumulate_holding(
                        name,
                        _cell(row, value_index),
                        _cell(cached_row, value_index),
                        sheet.title,
                        "mprofit-style",
                        text_guard,
                        aggregated,
                        seen_on_sheet,
                        layout_by_key,
                        sources=sources,
                        source=SourceReference(filename, sheet.title, row_number, _coordinate(value_index, row_number),
                            None),
                    )
                continue

            # Crystal-Reports-style multi-section transaction ledger: no per-row Scheme/
            # Asset Name column and no Closing Valuation/Current Value/Market Value column
            # -- instead "Fund Name" and "Cur Value", grouped under repeating section-title
            # rows (_LEDGER_DEBT_SECTIONS / _ledger_section_title above). Tried only when
            # the generic schema above found nothing at all on this sheet, so it can never
            # change behavior for any workbook that already parses today.
            ledger_found = _find_header(sheet, ({"fund name"}, {"cur value"}), text_guard, 150)
            if not ledger_found:
                continue
            _header_row, ledger_headers = ledger_found
            name_index = ledger_headers["fund name"]
            value_index = ledger_headers["cur value"]
            current_section: str | None = None
            rows = _bounded_rows(sheet, text_guard, min_row=1)
            cached_rows = _bounded_rows(cached_sheet, text_guard, min_row=1, count=False)
            for row_number, (row, cached_row) in enumerate(zip(rows, cached_rows), 1):
                section_title = _ledger_section_title(row, name_index, text_guard)
                if section_title is not None:
                    current_section = normalize_text(section_title)
                    continue
                name = text_guard.text(_cell(row, name_index), strip=True)
                if not name:
                    continue
                # This ledger's header row repeats at the top of every section, but only
                # the very first occurrence has "Cur Value" populated in its own header
                # cell -- every later occurrence leaves that one cell blank even though
                # "Fund Name" repeats correctly every time (confirmed against the real
                # file). Recognizing a repeat header by column position -- the Fund Name
                # cell here normalizing back to "fund name" -- is what lets every
                # section's header actually get skipped, instead of misread as a data row
                # with no value.
                if normalize_text(name) == "fund name":
                    continue
                if current_section not in _LEDGER_DEBT_SECTIONS:
                    continue
                scale = _ledger_value_scale(row, cached_row, ledger_headers, text_guard)
                if not scale:
                    continue
                previous_scale = scales_by_sheet.setdefault(sheet.title, scale)
                if previous_scale != scale:
                    raise WorkbookSchemaError("Holdings contain an inconsistent value scale within one worksheet.")
                _accumulate_holding(
                    name,
                    _cell(row, value_index),
                    _cell(cached_row, value_index),
                    sheet.title,
                    "crystal-reports-ledger",
                    text_guard,
                    aggregated,
                    seen_on_sheet,
                    layout_by_key,
                    scale,
                    sources=sources,
                    source=SourceReference(filename, sheet.title, row_number, _coordinate(value_index, row_number),
                        None, scale),
                )
        if not aggregated:
            raise WorkbookSchemaError(
                "The holdings workbook must contain Asset Name and Closing Valuation or Current Value columns for debt mutual funds."
            )
        return tuple(
            Holding(display, value, layout_by_key[key], scales_by_sheet.get(seen_on_sheet[key], 1.0), sources.get(key, ()))
            for key, (display, value) in aggregated.items()
        )
    except WorkbookSchemaError:
        raise
    except Exception as exc:
        # openpyxl's read_only mode only validates the archive shape at load time (see
        # _load above); a sheet's actual XML is parsed lazily, row by row, inside the
        # loop above. Content-level corruption (common in files that have been through
        # repeated broker/export "Save As" round-trips) only surfaces here, as a raw
        # openpyxl/XML exception with no relation to this workbook's schema. Without this
        # boundary that raw exception bypasses every curated check above it and reaches
        # the client as an opaque, unclassifiable fragment instead of an actionable message.
        raise WorkbookSchemaError(
            "The holdings workbook could not be fully read. It may be corrupted or saved in an unusual "
            "format. Try re-saving it as a standard .xlsx from Excel (or Google Sheets: File, Download, "
            "Microsoft Excel) and upload it again."
        ) from exc
    finally:
        workbook.close()
        cached_workbook.close()


def _broker_schema(headers: dict[str, int]) -> tuple[str, dict[str, int | None]] | None:
    if "scheme name" not in headers:
        return None
    if "holding" in headers and "company name" in headers:
        # ASK exports carry both a source-level Current Value (rupees) and an
        # Amount column (client/fund-scaled crore value). Keep those meanings
        # separate so neither can be mistaken for underlying scheme AUM.
        if "current value" in headers and "amount" in headers:
            return "layout-ask", {
                "scheme": headers["scheme name"],
                "paper": headers["company name"],
                "allocation": headers["holding"],
                "issuer": _pick(headers, "issuer name"),
                "rating": _pick(headers, "rating"),
                "isin": _pick(headers, "isin", "isin code"),
                "maturity": _pick(headers, "maturity date"),
                "market_value": headers["current value"],
                "client_amount": headers["amount"],
                "fund_size": None,
                "in_out": _pick(headers, "in out"),
                "asset_class": _pick(headers, "asset", "asset class", "portfolio nature"),
                "asset_type": _pick(headers, "asset type", "instrument type", "instrument name"),
            }
        return "layout-a", {
            "scheme": headers["scheme name"],
            "paper": headers["company name"],
            "allocation": headers["holding"],
            "issuer": _pick(headers, "issuer name"),
            "rating": _pick(headers, "rating"),
            "isin": _pick(headers, "isin", "isin code"),
            "maturity": _pick(headers, "maturity date"),
            "market_value": _pick(headers, "market value rs in crores", "market value crs"),
            "client_amount": None,
            "fund_size": None,
            "in_out": _pick(headers, "in out"),
            "asset_class": _pick(headers, "asset", "asset class", "portfolio nature"),
            "asset_type": _pick(headers, "asset type", "instrument type", "instrument name"),
        }
    if "of net assets" in headers and "company name" in headers:
        return "layout-b", {
            "scheme": headers["scheme name"],
            "paper": headers["company name"],
            "allocation": headers["of net assets"],
            "issuer": _pick(headers, "issuer name"),
            "rating": _pick(headers, "rating"),
            "isin": _pick(headers, "isin code", "isin"),
            "maturity": _pick(headers, "maturity date"),
            "market_value": _pick(headers, "market value crs", "market value rs in crores"),
            "client_amount": None,
            "fund_size": _pick(headers, "fund size crs"),
            "in_out": None,
            "asset_class": _pick(headers, "asset", "asset class", "portfolio nature"),
            "asset_type": _pick(headers, "asset type", "instrument type", "instrument name"),
        }
    return None


def _icra_scheme_schema(headers: dict[str, int]) -> dict[str, int | None] | None:
    # ICRA Analytics' "Detailed Portfolio" per-scheme export: one worksheet per fund
    # scheme, with the scheme name sitting above the header (see _icra_scheme_title)
    # rather than in a "Scheme Name" column. That absence -- true across every sheet
    # of the real fixture -- is exactly what keeps this from ever matching layout-a/b,
    # which both hard-require "scheme name".
    if "scheme name" in headers:
        return None
    if "company name" not in headers or "of net assets" not in headers:
        return None
    return {
        "paper": headers["company name"],
        "allocation": headers["of net assets"],
        "issuer": _pick(headers, "issuer name"),
        "rating": _pick(headers, "rating"),
        "asset_class": _pick(headers, "asset", "asset class", "portfolio nature"),
        "asset_type": _pick(headers, "asset type", "instrument type", "instrument name"),
        # Applied per-row at read time (only when the primary rating cell is blank),
        # not resolved once at header-detection time like _pick's compile-time
        # alternates -- a row can carry a primary rating while another on the same
        # sheet needs the fallback, so this can't be flattened into one index.
        "rating_fallback": _pick(headers, "long term rating"),
        "isin": _pick(headers, "isin code"),
        "maturity": _pick(headers, "maturity date"),
        # normalize_text collapses "Market Value (In Crs. )" and "Market Value In Crs"
        # to the same key, but both alternates are listed to be safe against a variant
        # this fixture didn't happen to exercise.
        "market_value": _pick(headers, "market value in crs", "market value (in crs. )"),
        "client_amount": None,
    }


_ICRA_BOILERPLATE_NORMALIZED = {
    normalize_text("ICRA Analytics Limited"),
    normalize_text("Electric Mansion, 4F, Appasaheb Marathe Marg, Prabhadevi, Mumbai 400 025, India"),
    normalize_text("Detailed Portfolio"),
}


def _is_icra_boilerplate_or_date(text: str) -> bool:
    """True for ICRA's fixed rows-1-3 boilerplate text or the row-4 report date.

    This is a defensive backstop, not the primary skip mechanism: rows 1-3 are
    already structurally excluded because their real text sits in column B (column
    A, which is all this helper and its caller ever look at, is blank there), and
    row 4's date is likewise outside column A's populated range in the real fixture.
    It exists so a title search that strays past that structural boundary still
    doesn't mistake boilerplate or a bare date for a scheme name.
    """
    if normalize_text(text) in _ICRA_BOILERPLATE_NORMALIZED:
        return True
    stripped = text.strip()
    for pattern in ("%d %b %Y", "%d-%b-%Y"):
        try:
            datetime.strptime(stripped, pattern)
            return True
        except ValueError:
            continue
    return False


def _icra_scheme_title(sheet, header_row: int, text_guard: _WorkbookTextGuard) -> str | None:
    """Find the scheme name sitting in column A above the header row.

    Scans upward from the header, bounded to at most 10 rows above it (counted
    against the shared row-scan safety budget), and returns the nearest non-blank,
    non-boilerplate/non-date column-A cell. Returns None rather than guessing
    further if nothing usable is found within that bound.
    """
    min_row = max(1, header_row - 10)
    if min_row >= header_row:
        return None
    rows = list(sheet.iter_rows(min_row=min_row, max_row=header_row - 1, max_col=MAX_COLUMNS, values_only=True))
    for row in reversed(rows):
        text_guard.scan_row()
        text = text_guard.text(_cell(row, 0), strip=True)
        if text and not _is_icra_boilerplate_or_date(text):
            return text
    return None


def _parse_icra_scheme_sheet(sheet, cached_sheet, text_guard: _WorkbookTextGuard, filename) -> list[BrokerPosition]:
    """Dedicated per-sheet path for ICRA Analytics' "Detailed Portfolio" export.

    Unlike layout-a/b, ICRA has no explicit "out" marker for a blank allocation --
    a blank "% of Net Assets" here just means the row is a section marker (Debt/
    Equity/Others) or footer text, not a real position, so it is skipped silently
    rather than raising WorkbookSchemaError.
    """
    found = _find_header(sheet, ({"company name"}, {"of net assets"}), text_guard, 30)
    if not found:
        return []
    header_row, headers = found
    schema = _icra_scheme_schema(headers)
    if schema is None:
        return []
    title = _icra_scheme_title(sheet, header_row, text_guard)
    if title is None:
        return []
    scheme, _normalized_scheme = text_guard.identifier(title, normalizer=normalize_fund_name)

    positions: list[BrokerPosition] = []
    rows = _bounded_rows(sheet, text_guard, min_row=header_row + 1)
    cached_rows = _bounded_rows(cached_sheet, text_guard, min_row=header_row + 1, count=False)
    for row_number, (row, cached_row) in enumerate(zip(rows, cached_rows), header_row + 1):
        paper = text_guard.text(_cell(row, schema["paper"]), strip=True)
        if not paper or paper == "--" or paper.lower().startswith("total "):
            continue
        if normalize_text(paper) == "grand total":
            continue
        allocation_points = _number_from_cached_formula(
            _cell(row, schema["allocation"]),
            _cell(cached_row, schema["allocation"]),
            text_guard,
        )
        if allocation_points is None:
            # No explicit "out" marker exists in this format; a blank allocation here
            # is a section marker or footer row, not an error condition.
            continue
        if abs(allocation_points) > 100.5:
            raise WorkbookSchemaError("Broker allocation percentages must be between -100 and 100.")
        paper, _normalized_paper = text_guard.identifier(paper)
        issuer, _normalized_issuer = text_guard.identifier(_cell(row, schema["issuer"]))
        rating_text = text_guard.text(_cell(row, schema["rating"]), strip=True)
        if rating_text:
            rating, _normalized_rating = text_guard.identifier(rating_text)
        else:
            rating, _normalized_rating = text_guard.identifier(_cell(row, schema["rating_fallback"]))
        isin, _normalized_isin = text_guard.identifier(_cell(row, schema["isin"]))
        market_value = _number_from_cached_formula(
            _cell(row, schema["market_value"]),
            _cell(cached_row, schema["market_value"]),
            text_guard,
        )
        positions.append(
            BrokerPosition(
                scheme=scheme,
                paper=paper,
                allocation=allocation_points / 100,
                issuer=issuer,
                rating=rating,
                maturity=_date(_cell(row, schema["maturity"]), text_guard),
                isin=isin,
                market_value_crore=market_value,
                layout="icra-per-scheme",
                asset_class=text_guard.identifier(_cell(row, schema["asset_class"]))[0],
                asset_type=text_guard.identifier(_cell(row, schema["asset_type"]))[0],
                source=SourceReference(filename, sheet.title, row_number, _coordinate(schema["allocation"], row_number), allocation_points),
                rating_cell=_coordinate(schema["rating"] if rating_text else schema["rating_fallback"], row_number),
            )
        )
    return positions


def _optional_prior_number(value, cached_value, text_guard):
    """Malformed comparison cells disable comparison, without weakening text budgets."""
    if _is_formula(value):
        if isinstance(value, str):
            text_guard.text(value, max_characters=MAX_FORMULA_TEXT_CHARACTERS)
        _guard_formula_object(value, text_guard)
        value = cached_value
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = text_guard.text(value, max_characters=MAX_NUMERIC_OR_DATE_TEXT_CHARACTERS)
        value = value.replace(",", "").replace("₹", "").strip().removesuffix("%")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if isfinite(number) else None


def _broker_comparison_schema(row, preceding_row, layout, text_guard):
    if layout != "layout-a":
        return None
    columns = [i for i, value in enumerate(row) if isinstance(value, str) and normalize_text(value) == "holding"]
    if len(columns) != 2:
        return None
    current, previous = columns
    current_date = _date(_cell(preceding_row, current), text_guard)
    previous_date = _date(_cell(preceding_row, previous), text_guard)
    if current_date and previous_date and previous_date < current_date:
        return previous, previous_date, current_date
    return None


def _broker_label(filename: str) -> str:
    return canonical_broker_name(filename)


def _allocation_header_date(value: object, text_guard: _WorkbookTextGuard) -> date | None:
    text = text_guard.text(value, max_characters=MAX_NUMERIC_OR_DATE_TEXT_CHARACTERS, strip=True)
    match = re.search(r"([A-Za-z]{3,9})[- /](20\d{2})", text)
    if not match:
        return None
    for fmt in ("%b-%Y", "%B-%Y"):
        try:
            parsed = datetime.strptime(f"{match.group(1)}-{match.group(2)}", fmt)
            return date(parsed.year, parsed.month, 1)
        except ValueError:
            continue
    return None


def _parse_icici_matrix_sheet(sheet, cached_sheet, text_guard: _WorkbookTextGuard, filename) -> list[BrokerPosition]:
    """Parse the ICICI wide issuer-by-scheme allocation matrix.

    Unlike the security-level exports, each non-empty fund column is an issuer-level
    allocation. No ISIN, bond, maturity, rating or security market value is invented.
    """
    # Do not use sheet.max_row as a scan bound: some broker exports carry an
    # undersized worksheet dimension even though the actual XML contains rows.
    # _bounded_rows already enforces the safe hard ceiling for the workbook.
    scan_end = 20
    header_row = None
    headers = ()
    for row_number, row in enumerate(_bounded_rows(sheet, text_guard, min_row=1), 1):
        values = tuple(row)
        normalized = [text_guard.normalize(value, header_scan=True) for value in values]
        company_columns = [index for index, item in enumerate(normalized) if item == "company name"]
        has_company = bool(company_columns)
        company_index = company_columns[0] if company_columns else -1
        has_allocation = any(
            index > company_index and "allocation" in item
            for index, item in enumerate(normalized)
        )
        if has_company and has_allocation:
            header_row, headers = row_number, values
            break
        if row_number >= scan_end:
            break
    if header_row is None:
        return []

    normalized_headers = [text_guard.normalize(value, header_scan=True) for value in headers]
    company_col = normalized_headers.index("company name")
    asset_col = next((i for i, value in enumerate(normalized_headers) if value == "asset"), None)
    sector_col = next((i for i, value in enumerate(normalized_headers) if value == "sector"), None)
    allocation_columns = [i for i, value in enumerate(normalized_headers) if i > company_col and "allocation" in value]

    # Scheme names are normally in the row immediately above the allocation
    # headers. Some workbooks insert an extra period/title row; search the five
    # preceding rows and take the row with the most populated allocation columns.
    scheme_row_number = None
    best_scheme_score = 0
    for candidate_row_number in range(max(1, header_row - 5), header_row):
        candidate_row = next(_bounded_rows(sheet, text_guard, min_row=candidate_row_number))
        score = sum(
            1 for col in allocation_columns
            if text_guard.text(_cell(candidate_row, col), strip=True)
        )
        if score > best_scheme_score:
            best_scheme_score = score
            scheme_row_number = candidate_row_number
    if scheme_row_number is None:
        return []
    scheme_row = next(_bounded_rows(sheet, text_guard, min_row=scheme_row_number))

    fund_columns = []
    for col in allocation_columns:
        header_text = text_guard.text(headers[col], strip=True)
        scheme_text = text_guard.text(_cell(scheme_row, col), strip=True)
        if not scheme_text:
            continue
        scheme, _ = text_guard.identifier(scheme_text, normalizer=normalize_fund_name)
        fund_columns.append((col, scheme, _allocation_header_date(header_text, text_guard)))
    if not fund_columns:
        return []

    output: list[BrokerPosition] = []
    # This worksheet shape is the ICICI Prudential issuer-by-scheme matrix.
    # Detect the provider from the schema as well as the filename so generic
    # names such as "Debt_Portfolio_Sep26.xlsx" do not lose the ICICI identity.
    broker = "ICICI"
    rows = _bounded_rows(sheet, text_guard, min_row=header_row + 1)
    cached_rows = _bounded_rows(cached_sheet, text_guard, min_row=header_row + 1, count=False)
    for row_number, (row, cached_row) in enumerate(zip(rows, cached_rows), header_row + 1):
        company = text_guard.text(_cell(row, company_col), strip=True)
        company_key = normalize_text(company)
        if not company or company_key in {"total", "grand total", "company name"}:
            continue
        # ICICI appends a long legal disclaimer in column A. It is not a
        # holding row and must be skipped before identifier-length validation.
        if company_key.startswith(("source", "disclaimer", "note:")):
            continue
        company, _ = text_guard.identifier(company)
        asset = text_guard.identifier(_cell(row, asset_col))[0] if asset_col is not None else "Debt"
        sector = text_guard.identifier(_cell(row, sector_col))[0] if sector_col is not None else ""
        for col, scheme, allocation_date in fund_columns:
            points = _number_from_cached_formula(_cell(row, col), _cell(cached_row, col), text_guard)
            if points is None or abs(points) <= 1e-12:
                continue
            if abs(points) > 100.5:
                raise WorkbookSchemaError("ICICI issuer allocation percentages must be between 0 and 100.")
            cell = _coordinate(col, row_number)
            output.append(BrokerPosition(
                scheme=scheme, paper=company, allocation=points / 100, issuer=company,
                rating="", maturity=None, isin="", market_value_crore=None,
                layout="icici-issuer-matrix", category="", source=SourceReference(
                    filename, sheet.title, row_number, cell, points), rating_cell="",
                asset_class=asset or "Debt", asset_type="Issuer-level allocation",
                source_scheme=scheme, broker=broker, position_level="issuer",
                allocation_date=allocation_date,
            ))
    return output


def parse_scheme_crosswalk(paths: Iterable[str | Path]) -> dict[str, str]:
    """Read optional Scheme Holdings -> Portfolio Summary Report aliases (ASK export)."""
    mappings: dict[str, str] = {}
    for path in paths:
        workbook = _load(path, data_only=True)
        text_guard = _WorkbookTextGuard()
        try:
            for sheet in workbook.worksheets:
                found = _find_header(sheet, ({"scheme holdings name"}, {"portfolio summary report name"}), text_guard, 20)
                if not found:
                    continue
                header_row, headers = found
                source_col = headers["scheme holdings name"]
                target_col = headers["portfolio summary report name"]
                for row_number, row in enumerate(_bounded_rows(sheet, text_guard, min_row=header_row + 1), header_row + 1):
                    source = text_guard.text(_cell(row, source_col), strip=True)
                    target = text_guard.text(_cell(row, target_col), strip=True)
                    if not source or not target:
                        continue
                    if normalize_text(source) in {"scheme holdings name", "scheme name portfolio report name mapping used by vlookup in column d"}:
                        continue
                    key = normalize_fund_name(source)
                    existing = mappings.get(key)
                    if existing and normalize_fund_name(existing) != normalize_fund_name(target):
                        raise WorkbookSchemaError(f"Conflicting fund crosswalk entries for {source!r} in {Path(path).name}.")
                    mappings[key] = target
                break
        finally:
            workbook.close()
    return mappings


def parse_broker(path: str | Path, *, source_name=None) -> tuple[BrokerPosition, ...]:
    filename = _source_name(path, source_name)
    workbook = _load(path)
    cached_workbook = _load(path, data_only=True)
    text_guard = _WorkbookTextGuard()
    positions: list[BrokerPosition] = []
    broker_label = _broker_label(filename)
    try:
        for sheet in workbook.worksheets:
            cached_sheet = cached_workbook[sheet.title]
            icici_positions = _parse_icici_matrix_sheet(sheet, cached_sheet, text_guard, filename)
            if icici_positions:
                positions.extend(icici_positions)
                continue
            current: tuple[str, dict[str, int | None]] | None = None
            comparison_schema = None
            preceding_row = ()
            for row_number, (row, cached_row) in enumerate(zip(
                _bounded_rows(sheet, text_guard),
                _bounded_rows(cached_sheet, text_guard, count=False),
            ), 1):
                date_row, preceding_row = preceding_row, cached_row
                headers = _header_map(row, text_guard)
                detected = _broker_schema(headers)
                if detected:
                    current = detected
                    comparison_schema = _broker_comparison_schema(row, date_row, current[0], text_guard)
                    continue
                if not current:
                    continue
                layout, indexes = current
                scheme = text_guard.text(_cell(row, indexes["scheme"]), strip=True)
                paper = text_guard.text(_cell(row, indexes["paper"]), strip=True)
                if not scheme or not paper:
                    continue
                normalized_scheme = text_guard.normalize(scheme)
                if normalized_scheme.startswith(("accord fintech", "source", "data as on", "report generated")):
                    continue
                if (
                    normalized_scheme in {"grand total", "total", "scheme name"}
                    or normalized_scheme.startswith("total ")
                    or paper.lower().startswith("total ")
                    or paper == "--"
                ):
                    continue
                scheme, _normalized_fund = text_guard.identifier(scheme, normalizer=normalize_fund_name)
                paper, _normalized_paper = text_guard.identifier(paper)
                allocation_points = _number_from_cached_formula(
                    _cell(row, indexes["allocation"]),
                    _cell(cached_row, indexes["allocation"]),
                    text_guard,
                )
                market_value = _number_from_cached_formula(
                    _cell(row, indexes["market_value"]),
                    _cell(cached_row, indexes["market_value"]),
                    text_guard,
                )
                client_amount = _number_from_cached_formula(
                    _cell(row, indexes.get("client_amount")),
                    _cell(cached_row, indexes.get("client_amount")),
                    text_guard,
                )
                if layout == "layout-ask":
                    # Current Value is INR; Amount is already expressed in crore.
                    market_value = market_value / 10_000_000 if market_value is not None else None
                fund_size = _number_from_cached_formula(
                    _cell(row, indexes["fund_size"]),
                    _cell(cached_row, indexes["fund_size"]),
                    text_guard,
                )
                allocation_cells = _coordinate(indexes["allocation"], row_number)
                if layout == "layout-b" and market_value is not None and fund_size and fund_size > 0:
                    allocation_cells = f'{_coordinate(indexes["market_value"], row_number)} / {_coordinate(indexes["fund_size"], row_number)} × 100'
                    allocation_points = market_value / fund_size * 100
                in_out = text_guard.normalize(
                    _cell(row, indexes["in_out"]),
                    max_characters=MAX_SCOPE_TEXT_CHARACTERS,
                    max_normalized_characters=MAX_NORMALIZED_IDENTIFIER_CHARACTERS,
                )
                if allocation_points is None:
                    if in_out == "out":
                        allocation_points = 0.0
                    else:
                        raise WorkbookSchemaError(
                            "A broker allocation is required unless an out position can be identified explicitly."
                        )
                if abs(allocation_points) > 100.5:
                    raise WorkbookSchemaError("Broker allocation percentages must be between -100 and 100.")
                issuer, _normalized_issuer = text_guard.identifier(_cell(row, indexes["issuer"]))
                rating, _normalized_rating = text_guard.identifier(_cell(row, indexes["rating"]))
                isin, _normalized_isin = text_guard.identifier(_cell(row, indexes["isin"]))
                comparison = None
                if comparison_schema:
                    prior_index, prior_date, current_date = comparison_schema
                    prior_value = _cell(row, prior_index)
                    prior = _optional_prior_number(prior_value, _cell(cached_row, prior_index), text_guard)
                    if prior is None and prior_value in (None, "", "--") and in_out == "in":
                        prior = 0.0
                    if prior is not None and abs(prior) <= 100.5:
                        comparison = PeriodComparison(prior_date, current_date, prior / 100,
                            SourceReference(filename, sheet.title, row_number, _coordinate(prior_index, row_number), prior))
                positions.append(
                    BrokerPosition(
                        scheme=scheme,
                        paper=paper,
                        allocation=allocation_points / 100,
                        issuer=issuer,
                        rating=rating,
                        maturity=_date(_cell(row, indexes["maturity"]), text_guard),
                        isin=isin,
                        market_value_crore=market_value,
                        layout=layout,
                        source=SourceReference(filename, sheet.title, row_number, allocation_cells, allocation_points),
                        rating_cell=_coordinate(indexes["rating"], row_number),
                        comparison=comparison,
                        asset_class=text_guard.identifier(_cell(row, indexes["asset_class"]))[0],
                        asset_type=text_guard.identifier(_cell(row, indexes["asset_type"]))[0],
                        source_scheme=scheme,
                        broker=broker_label,
                        position_level="security",
                        client_amount_crore=client_amount,
                        allocation_date=comparison.current_date if comparison else None,
                    )
                )
            if current is None:
                # No row on this sheet ever matched layout-a/b (both hard-require a
                # "Scheme Name" column). That is exactly ICRA Analytics' "Detailed
                # Portfolio" per-scheme shape and never true for any layout-a/b
                # fixture, real or synthetic, so this path is unreachable for them.
                positions.extend(_parse_icra_scheme_sheet(sheet, cached_sheet, text_guard, filename))
        if not positions:
            raise WorkbookSchemaError(
                "The broker workbook must contain Scheme Name, Company Name, and Holding(%) or % of Net Assets columns."
            )
        return tuple(positions)
    except WorkbookSchemaError:
        raise
    except Exception as exc:
        # See the matching comment in parse_holdings: read_only mode parses each
        # sheet's XML lazily as this loop iterates, so content-level corruption
        # (common after repeated broker/export "Save As" round-trips) surfaces here
        # as a raw openpyxl/XML exception, not a schema problem this function checked.
        raise WorkbookSchemaError(
            "The broker workbook could not be fully read. It may be corrupted or saved in an unusual "
            "format. Try re-saving it as a standard .xlsx from Excel (or Google Sheets: File, Download, "
            "Microsoft Excel) and upload it again."
        ) from exc
    finally:
        workbook.close()
        cached_workbook.close()


def parse_brokers(paths: str | Path | Iterable[str | Path], *, source_names=None) -> tuple[BrokerPosition, ...]:
    """Parse and merge one or more broker portfolio workbooks into a single position set.

    Each broker/fund-house file typically covers a different set of funds, so their
    positions are simply concatenated (this is the SRD's "consolidate data from multiple
    input files"). A fund is never silently split across files: if the same scheme appears
    in two workbooks, its allocations combine and the engine's per-scheme 100%-allocation
    check raises a clear WorkbookSchemaError instead of quietly double-counting it.
    """
    if isinstance(paths, (str, Path)):
        paths = (paths,)
    paths = tuple(paths)
    if source_names is not None and (not isinstance(source_names, (list, tuple)) or len(source_names) != len(paths)):
        raise WorkbookSchemaError("Broker source names must match the uploaded workbooks.")
    merged: list[BrokerPosition] = []
    for index, path in enumerate(paths):
        merged.extend(parse_broker(path, source_name=source_names[index] if source_names is not None else None))
    return tuple(merged)


def _sheet_header(
    sheet,
    required: tuple[set[str], ...],
    text_guard: _WorkbookTextGuard,
    scan: int = 30,
):
    return _find_header(sheet, required, text_guard, scan)


def _find_named_or_unique_schema_sheet(
    workbook,
    expected_name: str,
    required: tuple[set[str], ...],
    text_guard: _WorkbookTextGuard,
    scan: int,
    *,
    named_schema_error: str,
    missing_error: str,
    equivalent=None,
) -> tuple[Any, int, dict[str, int]]:
    """Prefer an explicitly named sheet; otherwise compare all schema candidates.

    Multiple copies are accepted only when the supplied bounded comparator verifies
    identical consumed mappings and detail multisets, including duplicate counts.
    Differing candidates require the user's explicit base-sheet selection.
    """
    named = _sheet_named(workbook, expected_name)
    if named is not None:
        found = _sheet_header(named, required, text_guard, scan)
        if not found:
            raise WorkbookSchemaError(named_schema_error)
        header_row, headers = found
        return named, header_row, headers

    candidates: list[tuple[Any, int, dict[str, int]]] = []
    for sheet in workbook.worksheets:
        found = _sheet_header(sheet, required, text_guard, scan)
        if found:
            header_row, headers = found
            candidates.append((sheet, header_row, headers))

    if not candidates:
        raise WorkbookSchemaError(missing_error)
    if len(candidates) > 1:
        if equivalent is not None and equivalent(candidates):
            return candidates[0]
        names = ", ".join(f'"{sheet.title}"' for sheet, _row, _headers in candidates)
        raise WorkbookSchemaError(
            f'Multiple worksheets ({names}) match the required "{expected_name}" schema, so it is not '
            f'possible to tell which one is authoritative. Rename the correct one to "{expected_name}" '
            "and re-upload."
        )
    return candidates[0]



def _discover_group_snapshot_sheet(workbook, text_guard: _WorkbookTextGuard):
    """Find the prior-period business-group summary by schema, not worksheet name.

    A valid snapshot has a group-label column and an exposure/value column. Rating
    pivots normally use Row Labels plus a rating/category field and are deliberately
    not accepted as group snapshots. Preferred names only break ties; they do not
    define the schema.
    """
    candidates = []
    for sheet in workbook.worksheets:
        found = _sheet_header(
            sheet,
            ({"group", "group exposure", "business group"},
             {"exposure", "sum of exposure", "paper amount in cr", "amount in cr", "amt in cr", "total exposure"}),
            text_guard,
            30,
        )
        if not found:
            continue
        header_row, headers = found
        group_index = _pick(headers, "group", "group exposure", "business group")
        amount_index = _pick(headers, "exposure", "sum of exposure", "paper amount in cr", "amount in cr", "amt in cr", "total exposure")
        if group_index is None or amount_index is None:
            continue
        # Require actual populated group/value rows. A title or rating pivot alone
        # must never become the opening business-group baseline.
        populated = 0
        distinct = set()
        for row in _bounded_rows(sheet, text_guard, min_row=header_row + 1):
            label = text_guard.text(_cell(row, group_index), max_characters=MAX_RAW_IDENTIFIER_CHARACTERS, strip=True)
            if not label:
                continue
            key = normalize_text(label)
            if key in {"grand total", "total"}:
                continue
            value = _cell(row, amount_index)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            populated += 1
            distinct.add(key)
        if populated == 0:
            continue
        title = normalize_text(sheet.title)
        # A human-readable group summary wins over generic/legacy sheet names.
        title_score = 3 if "group" in title else (2 if "exposure" in title else (1 if title == "sheet8" else 0))
        header_score = 2 if "group" in headers or "group exposure" in headers or "business group" in headers else 0
        candidates.append((title_score + header_score, len(distinct), sheet, header_row, headers))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and candidates[1][:2] == best[:2]:
        names = ", ".join(f'"{item[2].title}"' for item in candidates if item[:2] == best[:2])
        raise WorkbookSchemaError(
            f"Multiple worksheets ({names}) look like prior-period business-group exposure summaries. "
            "Select/rename the authoritative group summary sheet and upload again."
        )
    return best[2], best[3], best[4]


def _discover_issuer_mapping_sheet(workbook, text_guard: _WorkbookTextGuard):
    """Find issuer-to-group mapping by its two semantic columns, not 'Sheet6'."""
    candidates = []
    for sheet in workbook.worksheets:
        found = _sheet_header(
            sheet,
            ({"group", "business group"}, {"group as per mf", "row labels", "issuer", "issuer name", "borrower", "borrower name"}),
            text_guard,
            30,
        )
        if not found:
            continue
        header_row, headers = found
        group_index = _pick(headers, "group", "business group")
        issuer_index = _pick(headers, "group as per mf", "row labels", "issuer", "issuer name", "borrower", "borrower name")
        if group_index is None or issuer_index is None or group_index == issuer_index:
            continue
        rows_with_both = 0
        for row in _bounded_rows(sheet, text_guard, min_row=header_row + 1):
            if text_guard.text(_cell(row, group_index), strip=True) and text_guard.text(_cell(row, issuer_index), strip=True):
                rows_with_both += 1
        if rows_with_both:
            title = normalize_text(sheet.title)
            score = 2 if "mapping" in title or "group" in title else (1 if title == "sheet6" else 0)
            candidates.append((score, rows_with_both, sheet, header_row, headers))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = candidates[0]
    if len(candidates) > 1 and candidates[1][:2] == best[:2]:
        names = ", ".join(f'"{item[2].title}"' for item in candidates if item[:2] == best[:2])
        raise WorkbookSchemaError(
            f"Multiple worksheets ({names}) look like issuer-to-group mappings. "
            "Select the authoritative mapping sheet and upload again."
        )
    return best[2], best[3], best[4]

def _historical_group_label(value: object, text_guard: _WorkbookTextGuard) -> tuple[str, str]:
    label = text_guard.text(value, max_characters=MAX_RAW_IDENTIFIER_CHARACTERS, strip=True)
    if normalize_text(label) == "grand total":
        return label, "grand total"
    label = re.sub(r"\s+total\s*$", "", label, flags=re.IGNORECASE).strip()
    normalized = normalize_text(label)
    if len(normalized) > MAX_NORMALIZED_IDENTIFIER_CHARACTERS:
        raise WorkbookSchemaError("Workbook identifier or label length exceeds the supported safety limit.")
    return label, normalized


def _cached_text(value, cached_value, text_guard):
    if not _is_formula(value):
        return value
    _guard_formula_object(value, text_guard)
    if isinstance(value, str):
        text_guard.text(value, max_characters=MAX_FORMULA_TEXT_CHARACTERS)
    return cached_value  # Missing formula caches never become literal rating text.


def _report_snapshot_date(workbook, cached_workbook, text_guard) -> date | None:
    info = _sheet_named(workbook, "Report Info")
    if info is None:
        return None
    dates = set()
    rows = _bounded_rows(info, text_guard)
    cached_rows = _bounded_rows(cached_workbook[info.title], text_guard, count=False)
    for row, cached in zip(rows, cached_rows):
        if text_guard.normalize(_cell(row, 0)) != "report as of":
            continue
        value = _cached_text(_cell(row, 1), _cell(cached, 1), text_guard)
        if value == "Not recorded":
            continue
        parsed = _date(value, text_guard)
        if parsed is None:
            raise WorkbookSchemaError("The mapping report date is missing or invalid. Check Report Info in the mapping workbook.")
        dates.add(parsed)
    if len(dates) > 1:
        raise WorkbookSchemaError("The mapping workbook contains conflicting report dates. Check its Report Info sheet.")
    return next(iter(dates), None)


def _equivalent_base_sheets(candidates, cached_workbook, text_guard) -> bool:
    signatures = []
    for sheet, header_row, headers in candidates:
        signature = Counter()
        final_mapping = {}
        indices = [_pick(headers, *names) for names in (
            ("fund name as per broker", "fund name"), ("paper name",), ("group exposure",),
            ("of holding fund level", "of holding", "holding", "allocation"),
            ("paper amount in cr", "market value crs", "amount"), ("paper maturity", "maturity date"),
            ("prop mkt value",), ("rating",), ("issuer", "issuer name"), ("isin", "isin code"), ("category",),
            ("asset", "asset class"), ("asset type", "instrument type"))]
        rows = _bounded_rows(sheet, text_guard, min_row=header_row + 1)
        cached_rows = _bounded_rows(cached_workbook[sheet.title], text_guard, min_row=header_row + 1, count=False)
        for row, cached in zip(rows, cached_rows):
            fund, paper, group = [text_guard.identifier(_cell(row, i))[0] for i in indices[:3]]
            if not paper:
                continue  # Subtotals carry no paper-to-group mapping.
            final_mapping[normalize_text(paper)] = group
            numbers = tuple(_number_from_cached_formula(_cell(row, i), _cell(cached, i), text_guard)
                            for i in indices[3:5])
            exposure = _number(_cached_text(_cell(row, indices[6]), _cell(cached, indices[6]), text_guard), text_guard)
            rating = text_guard.identifier(_cached_text(_cell(row, indices[7]), _cell(cached, indices[7]), text_guard))[0]
            labels = tuple(text_guard.identifier(_cell(row, i))[0] for i in indices[8:])
            signature[(fund, paper, group, *numbers, _date(_cell(row, indices[5]), text_guard), exposure,
                       rating, *labels)] += 1
        signatures.append((signature, final_mapping))
    return bool(signatures[0][0]) and all(item == signatures[0] for item in signatures[1:])


def parse_master(path: str | Path, base_sheet: str | None = None, *, source_name=None) -> MasterData:
    filename = _source_name(path, source_name)
    workbook = _load(path)
    text_guard = _WorkbookTextGuard()
    cached_workbook = None
    try:
        # The historical reference workbook legitimately contains formulas in its
        # prior-period totals. openpyxl never evaluates them; the second read exposes
        # only stored results, which are still subjected to finite-number validation.
        cached_workbook = _load(path, data_only=True)
        snapshot_date = _report_snapshot_date(workbook, cached_workbook, text_guard)
        if base_sheet and _sheet_named(workbook, base_sheet) is None:
            raise WorkbookSchemaError("The selected mapping base sheet does not exist.")
        base, base_header_row, base_headers = _find_named_or_unique_schema_sheet(
            workbook,
            base_sheet or "BASE",
            # "Fund Name" (without "As Per Broker") is a real header variant seen from the
            # same client across periods -- the required column is otherwise identical.
            ({"paper name"}, {"group exposure"}, {"fund name as per broker", "fund name"}),
            text_guard,
            20,
            named_schema_error="The mapping workbook BASE sheet has an unsupported schema.",
            equivalent=lambda candidates: _equivalent_base_sheets(candidates, cached_workbook, text_guard),
            missing_error=(
                "The mapping workbook must contain a BASE sheet. No worksheet with the required "
                "Paper Name / Group Exposure / Fund Name columns was found either."
            ),
        )
        paper_index = base_headers["paper name"]
        subgroup_index = base_headers["group exposure"]
        fund_index = _pick(base_headers, "fund name as per broker", "fund name")
        paper_maturity_index = _pick(base_headers, "paper maturity", "maturity date")
        fund_maturity_index = _pick(base_headers, "fund maturity")
        # "Holding" without the "(Fund Level)" qualifier, and its bare "% of Holding"
        # header rendering (normalises to "of holding"), are the same client's short
        # column-label variant seen in a differently-formatted period export.
        allocation_index = _pick(base_headers, "of holding fund level", "of holding", "holding", "allocation")
        # "Amount" is the same client's short label for "Paper Amount in Cr" seen in a
        # differently-formatted period export.
        market_value_index = _pick(base_headers, "paper amount in cr", "market value crs", "amount")
        opening_value_index = _pick(base_headers, "prop mkt value")
        if opening_value_index is None:
            opening_value_index = market_value_index
        opening_rating_index = _pick(base_headers, "rating")
        issuer_index = _pick(base_headers, "issuer", "issuer name")
        isin_index = _pick(base_headers, "isin", "isin code")
        profile_category_index = _pick(base_headers, "category")
        asset_index = _pick(base_headers, "asset", "asset class")
        asset_type_index = _pick(base_headers, "asset type", "instrument type")
        opening_rows: list[OpeningPosition] = []
        opening_complete = True
        base_rows: list[tuple[str, str, str, str, str, str]] = []
        base_issuer_rows: set[tuple[str, str]] = set()
        paper_issuer_candidates: dict[str, set[str]] = defaultdict(set)
        isin_issuer_candidates: dict[str, set[str]] = defaultdict(set)
        issuer_labels: dict[str, str] = {}
        profile_rows: dict[str, list[BrokerPosition]] = defaultdict(list)
        profile_names: dict[str, set[str]] = defaultdict(set)
        cached_base = cached_workbook[base.title]
        rows = _bounded_rows(base, text_guard, min_row=base_header_row + 1)
        cached_rows = _bounded_rows(cached_base, text_guard, min_row=base_header_row + 1, count=False)
        for row_number, (row, cached_row) in enumerate(zip(rows, cached_rows), base_header_row + 1):
            paper, paper_key = text_guard.identifier(_cell(row, paper_index))
            subgroup, subgroup_key = text_guard.identifier(_cell(row, subgroup_index))
            fund, fund_key = text_guard.identifier(_cell(row, fund_index), normalizer=normalize_fund_name)
            issuer, issuer_key = text_guard.identifier(_cell(row, issuer_index))
            if paper and issuer_key:
                base_issuer_rows.add((issuer_key, subgroup_key))
            issuer = issuer or subgroup or paper
            if paper_key and subgroup:
                paper_issuer_candidates[paper_key].add(subgroup)
            if subgroup_key and subgroup:
                issuer_labels.setdefault(subgroup_key, subgroup)
            isin = text_guard.identifier(_cell(row, isin_index))[0]
            isin_key = normalize_text(isin)
            if isin_key and subgroup:
                isin_issuer_candidates[isin_key].add(subgroup)
            profile_category = text_guard.identifier(_cell(row, profile_category_index))[0]
            asset_class = text_guard.identifier(_cell(row, asset_index))[0]
            asset_type = text_guard.identifier(_cell(row, asset_type_index))[0]
            rating = text_guard.identifier(_cached_text(_cell(row, opening_rating_index),
                _cell(cached_row, opening_rating_index), text_guard))[0]
            maturity = _date(_cell(row, paper_maturity_index), text_guard)
            fund_maturity = _date(_cell(row, fund_maturity_index), text_guard)
            if paper or subgroup or fund:
                base_rows.append((paper, paper_key, subgroup, subgroup_key, fund, fund_key))
            if fund and paper:
                opening_amount = _number(_cached_text(_cell(row, opening_value_index),
                    _cell(cached_row, opening_value_index), text_guard), text_guard)
                if opening_amount is None:
                    opening_complete = False
                else:
                    opening_rows.append(OpeningPosition(BrokerPosition(
                        scheme=fund, paper=paper, issuer=issuer, allocation=0, rating=rating,
                        maturity=maturity, isin=isin, market_value_crore=None, layout="master-opening",
                        category=profile_category, asset_class=asset_class, asset_type=asset_type,
                        source=SourceReference(filename, base.title, row_number, _coordinate(opening_value_index, row_number), opening_amount),
                        fund_maturity=fund_maturity, source_scheme=fund, position_level="security"), opening_amount, subgroup))
            allocation_points = _number_from_cached_formula(
                _cell(row, allocation_index),
                _cell(cached_row, allocation_index),
                text_guard,
            )
            if not (fund and fund_key and paper and allocation_points is not None):
                continue
            if abs(allocation_points) > 100.5:
                raise WorkbookSchemaError("Master profile allocation percentages must be between -100 and 100.")
            market_value = _number_from_cached_formula(
                _cell(row, market_value_index),
                _cell(cached_row, market_value_index),
                text_guard,
            )
            profile_names[fund_key].add(fund.casefold())
            profile_rows[fund_key].append(
                BrokerPosition(
                    scheme=fund,
                    paper=paper,
                    allocation=allocation_points / 100,
                    issuer=issuer,
                    rating=rating,
                    maturity=maturity,
                    isin=isin,
                    market_value_crore=market_value,
                    layout="master-base",
                    category=profile_category,
                    asset_class=asset_class,
                    asset_type=asset_type,
                    source=SourceReference(filename, base.title, row_number, _coordinate(allocation_index, row_number), allocation_points),
                    rating_cell=_coordinate(opening_rating_index, row_number),
                    fund_maturity=fund_maturity,
                    source_scheme=fund,
                    position_level="security",
                )
            )

        fund_profiles: dict[str, tuple[BrokerPosition, ...]] = {}
        for fund_key, positions in profile_rows.items():
            allocation_total = sum(position.allocation for position in positions)
            if (
                len(profile_names[fund_key]) == 1
                and isfinite(allocation_total)
                and abs(allocation_total - 1) <= 0.001
            ):
                fund_profiles[fund_key] = tuple(positions)

        historical_groups: list[str] = []
        historical_group_set: set[str] = set()
        previous_groups: dict[str, float] = {}
        group_ratings: dict[str, str] = {}
        canonical_work = 0
        baseline_sheet = ""
        # Detect the opening business-group summary from its headers and content.
        # The previous quarter's Sheet8 is preferred by schema/title, but the
        # parser does not depend on a fixed worksheet name. Rating pivots are not
        # accepted as group summaries.
        discovered_snapshot = _discover_group_snapshot_sheet(workbook, text_guard)
        if discovered_snapshot is not None:
            summary, header_row, headers = discovered_snapshot
            group_index = _pick(headers, "group", "group exposure", "business group")
            amount_index = _pick(headers, "exposure", "sum of exposure", "paper amount in cr",
                                 "amount in cr", "amt in cr", "total exposure")
            rating_index = _pick(headers, "category", "ratings", "rating")
            snapshot = []
            rows = _bounded_rows(summary, text_guard, min_row=header_row + 1)
            cached_rows = _bounded_rows(cached_workbook[summary.title], text_guard,
                                        min_row=header_row + 1, count=False)
            for row_number, (row, cached_row) in enumerate(zip(rows, cached_rows), header_row + 1):
                raw_group, key = _historical_group_label(_cell(row, group_index), text_guard)
                if key == "grand total":
                    break
                amount = _number_from_cached_formula(_cell(row, amount_index),
                                                     _cell(cached_row, amount_index), text_guard)
                rating_value = _cached_text(_cell(row, rating_index), _cell(cached_row, rating_index), text_guard)
                rating, rating_key = text_guard.identifier(rating_value)
                if not key:
                    if amount is not None and abs(amount) > 1e-12:
                        # Sheet8 may contain category-only reconciliation lines such
                        # as Others, Cash and Unrated beneath the named group list.
                        # These are not business groups and must not be converted into
                        # a fictitious group called 'Others' for Added/Exited logic.
                        is_rating_bucket = ("bank" in rating_key or "non bank" in rating_key) and any(
                            grade in set(rating_key.split()) for grade in {"aaa", "aa", "a1"}
                        )
                        if rating_key in {
                            "others", "cash", "cash equivalent", "cash and equivalent", "unrated",
                            "not disclosed", "not disclosed for papers", "govt", "govt owned", "sdl",
                            "less", "total",
                        } or is_rating_bucket:
                            continue
                        raise WorkbookSchemaError(
                            f"The opening group summary '{summary.title}' has exposure in row {row_number} "
                            f"({rating or 'unclassified'}) without a business-group name. Resolve this "
                            "classification before running quarter-on-quarter group movements."
                        )
                    continue
                if amount is None:
                    continue
                snapshot.append((raw_group, amount, rating))
            if not snapshot:
                raise WorkbookSchemaError(
                    f"The opening group summary '{summary.title}' has the expected headers but no usable exposure rows."
                )
            canonical_work = _reserve_master_group_work(canonical_work, _growing_canonical_scan_work(len(snapshot)))
            snapshot_labels = {}
            for raw_group, amount, rating in snapshot:
                # Preserve the report's approved group label; only normalized exact
                # duplicates are consolidated. Fuzzy matching must not merge groups.
                group = snapshot_labels.setdefault(normalize_text(raw_group), raw_group)
                if group not in historical_group_set:
                    historical_groups.append(group)
                    historical_group_set.add(group)
                previous_groups[group] = previous_groups.get(group, 0.0) + amount
                if rating and (group not in group_ratings or abs(amount) > 0):
                    group_ratings[group] = rating
            baseline_sheet = summary.title

        issuer_classifications = {}
        paper_classifications = {}
        subgroup_groups: dict[str, str] = {}
        issuer_groups: dict[str, str] = {}
        discovered_mapping = _discover_issuer_mapping_sheet(workbook, text_guard)
        if discovered_mapping is not None:
            sheet6, header_row, headers = discovered_mapping
            group_index = headers["group"]
            subgroup_map_index = _pick(headers, "row labels", "group as per mf")
            rating_index = _pick(headers, "rating")
            category_index = _pick(headers, "concat")
            type_index = _pick(headers, "bank non bank govt owned", "bank non bank", "type")
            mapping_rows: list[tuple[str, str, str, str, SourceReference]] = []
            rows = _bounded_rows(sheet6, text_guard, min_row=header_row + 1)
            cached_rows = _bounded_rows(cached_workbook[sheet6.title], text_guard, min_row=header_row + 1, count=False)
            for row_number, (row, cached_row) in enumerate(zip(rows, cached_rows), header_row + 1):
                subgroup, subgroup_key = text_guard.identifier(_cell(row, subgroup_map_index))
                if subgroup_key in {"grand total", "total", "added", "exit", "rating", "category"}:
                    break
                if not subgroup:
                    continue
                raw_group, _raw_group_key = text_guard.identifier(_cell(row, group_index))
                concat_value = _cached_text(_cell(row, category_index), _cell(cached_row, category_index), text_guard)
                rating, _rating_key = text_guard.identifier(
                    concat_value
                    or _cached_text(_cell(row, rating_index), _cell(cached_row, rating_index), text_guard)
                )
                cell = _coordinate(category_index if concat_value else rating_index, row_number)
                kind = text_guard.identifier(_cached_text(_cell(row, type_index), _cell(cached_row, type_index), text_guard))[0]
                if kind and normalize_text(kind) not in normalize_text(rating):
                    rating = f"{rating} {kind}".strip()
                    cell += f", {_coordinate(type_index, row_number)}"
                mapping_rows.append((subgroup, subgroup_key, raw_group, rating,
                    SourceReference(filename, sheet6.title, row_number, cell, None)))

            canonical_work = _reserve_master_group_work(
                canonical_work,
                _canonical_scan_work(len(mapping_rows), len(historical_groups)),
            )
            for subgroup, subgroup_key, raw_group, rating, reference in mapping_rows:
                # A blank Sheet6 Group is an unresolved parent mapping, not permission
                # to turn the issuer itself into a parent group. Preserve the issuer
                # label for BASE/Sheet4 and let seed mappings or Review resolve parent.
                issuer_labels.setdefault(subgroup_key, subgroup)
                if not raw_group:
                    continue
                group = canonical_group_name(raw_group, historical_groups)
                subgroup_groups[subgroup_key] = group
                issuer_groups[subgroup_key] = group
                if rating:
                    issuer_classifications[subgroup_key] = ClassificationHint(group, rating, reference)
                if rating and group not in group_ratings:
                    group_ratings[group] = rating

        paper_groups: dict[str, str] = {}
        base_group_cache: dict[str, str] = {}
        for paper, paper_key, subgroup, subgroup_key, _fund, _fund_key in base_rows:
            if not paper:
                continue
            # paper_groups means PAPER -> PARENT GROUP. A blank historical parent
            # stays blank; the issuer label is retained separately in paper_issuers.
            group = subgroup_groups.get(subgroup_key)
            if group:
                paper_groups[paper_key] = group
            if subgroup_key in issuer_classifications:
                paper_classifications[paper_key] = issuer_classifications[subgroup_key]

        # Exported issuers can identify new securities, but a borrower that maps to
        # multiple groups (or only Others) must not become an automatic alias.
        issuer_candidates: dict[str, set[str]] = defaultdict(set)
        for issuer_key, subgroup_key in base_issuer_rows:
            group = subgroup_groups.get(subgroup_key)
            if group:
                issuer_candidates[issuer_key].add(group)
        for issuer_key, groups in issuer_candidates.items():
            if len(groups) == 1 and "Others" not in groups:
                issuer_groups.setdefault(issuer_key, next(iter(groups)))

        historical = tuple(dict.fromkeys((*historical_groups, *subgroup_groups.values(), "Others")))
        if len(historical) > MAX_DISTINCT_HISTORICAL_GROUPS:
            raise WorkbookSchemaError(
                "Workbook exceeds the distinct historical-group safety limit. "
                "Reduce the number of group mappings."
            )
        stats = {
            "base_rows": len(base_rows),
            "distinct_papers": len(
                {
                    paper_key
                    for paper, paper_key, _subgroup, _subgroup_key, _fund, _fund_key in base_rows
                    if paper
                }
            ),
            "funds": len(
                {
                    fund_key
                    for _paper, _paper_key, _subgroup, _subgroup_key, fund, fund_key in base_rows
                    if fund
                }
            ),
            "subgroups": len(
                {
                    subgroup_key
                    for _paper, _paper_key, subgroup, subgroup_key, _fund, _fund_key in base_rows
                    if subgroup
                }
            ),
            "subgroup_mappings": len(subgroup_groups),
            "fund_profiles": len(fund_profiles),
            "snapshot_groups": len(previous_groups),
        }
        # Investor exposure must reconcile to the independent opening summary. AUM,
        # incomplete caches, or details from another period cannot restate that baseline.
        opening_total = sum(item.exposure for item in opening_rows)
        snapshot_total = sum(previous_groups.values())
        # Sheet8's named business-group subtotal intentionally excludes its
        # category-only reconciliation rows (Others/Cash/Unrated). BASE still
        # contains the underlying securities, so its total may legitimately be
        # greater than the named-group subtotal. It must never be smaller: that
        # would mean the opening group baseline exceeds the security-level source.
        opening_tolerance = max(1e-8, abs(opening_total) * 1e-8)
        if (not opening_complete or not previous_groups or not isfinite(opening_total)
                or not isfinite(snapshot_total) or snapshot_total - opening_total > opening_tolerance):
            opening_rows = []
        return MasterData(
            paper_groups=frozen_mapping(paper_groups),
            subgroup_groups=frozen_mapping(subgroup_groups),
            issuer_groups=frozen_mapping(issuer_groups),
            previous_groups=frozen_mapping(previous_groups),
            group_ratings=frozen_mapping(group_ratings),
            historical_groups=historical,
            stats=frozen_mapping(stats),
            fund_profiles=frozen_mapping(fund_profiles),
            base_sheet=base.title,
            baseline_sheet=baseline_sheet,
            requested_base_sheet=base_sheet,
            opening_positions=tuple(opening_rows),
            snapshot_date=snapshot_date,
            issuer_classifications=frozen_mapping(issuer_classifications),
            paper_classifications=frozen_mapping(paper_classifications),
            paper_issuers=frozen_mapping({key: next(iter(values)) for key, values in paper_issuer_candidates.items() if len(values) == 1}),
            isin_issuers=frozen_mapping({key: next(iter(values)) for key, values in isin_issuer_candidates.items() if len(values) == 1}),
            issuer_labels=frozen_mapping(issuer_labels),
        )
    except WorkbookSchemaError:
        raise
    except Exception as exc:
        # See the matching comment in parse_holdings: read_only mode parses each
        # sheet's XML lazily as the loops above iterate, so content-level corruption
        # (common after repeated export/"Save As" round-trips) surfaces here as a raw
        # openpyxl/XML exception, not a schema problem this function checked for.
        raise WorkbookSchemaError(
            "The mapping workbook could not be fully read. It may be corrupted or saved in an unusual "
            "format. Try re-saving it as a standard .xlsx from Excel (or Google Sheets: File, Download, "
            "Microsoft Excel) and upload it again."
        ) from exc
    finally:
        workbook.close()
        if cached_workbook is not None:
            cached_workbook.close()
