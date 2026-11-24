from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pdfplumber
from openpyxl import load_workbook


SECURITY_ALIASES = (
    "security", "security name", "scrip", "scrip name", "stock", "stock name",
    "company", "company name", "instrument", "instrument name", "item description",
    "description", "security description", "name",
)
QUANTITY_ALIASES = (
    "quantity", "qty", "qnty", "units", "unit", "shares", "share quantity",
    "no of shares", "no. of shares", "number of shares", "shares held", "units held",
    "quantity held", "qty held", "holding qty", "holding quantity", "holding", "holdings",
    "current holding", "current holdings", "current quantity", "current qty", "closing quantity",
    "closing qty", "closing holdings", "closing holding", "balance quantity", "balance qty",
    "net quantity", "net qty", "position quantity", "position qty", "position", "free quantity",
    "free qty", "total quantity", "total qty", "total units", "no of units", "no. of units",
    "shares/units", "shares / units", "quantity as on", "holding as on",
)
ISIN_ALIASES = ("isin", "isin code", "isin no", "isin number", "security isin", "isin id")
AVERAGE_COST_ALIASES = ("avg cost", "average cost", "avg. cost", "cost price", "average price", "avg price", "unit cost")
CURRENT_PRICE_ALIASES = ("current price", "cmp", "market price", "ltp", "closing price", "current rate", "price")
INVESTMENT_VALUE_ALIASES = ("investment value", "invested value", "cost value", "buy value", "purchase value", "cost", "book value")
CURRENT_VALUE_ALIASES = ("current value", "market value", "present value", "valuation", "portfolio value", "market valuation", "closing value")
PNL_ALIASES = ("p&l", "pnl", "profit/loss", "profit loss", "gain/loss", "unrealised p&l", "unrealized p&l", "gain loss")
PNL_PCT_ALIASES = ("p&l %", "pnl %", "pnl pct", "profit/loss %", "gain/loss %", "return %", "% g/l", "% gain", "% return")
SECTOR_ALIASES = ("sector", "industry", "industry group", "sector name")
PERCENTAGE_ALIASES = (
    "%", "percent", "percentage", "holding %", "holding percentage", "% holding",
    "allocation", "allocation %", "% allocation", "portfolio %", "portfolio percentage",
    "weight", "weight %", "% weight", "% assets", "assets %", "portfolio weight",
    "holding weight", "allocation weight", "share of portfolio", "weightage",
    "weightage %", "holding weightage", "portfolio weightage", "contribution perc",
    "contribution percentage", "contribution %", "% assets", "assets percentage",
    "percentage of assets", "percent of assets", "% of assets", "% of portfolio", "portfolio share %",
)

GENERIC_FILENAME_WORDS = {
    "portfolio", "appraisal", "report", "position", "analysis", "detailed", "holding",
    "holdings", "statement", "monthly", "summary", "valuation", "appraisalreport",
}
MONTH_WORDS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
    "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
    "oct", "october", "nov", "november", "dec", "december",
}


@dataclass
class ParsedHolding:
    isin: str | None
    security_name: str
    quantity: float
    measurement_type: str = "QUANTITY"
    average_cost: float | None = None
    current_price: float | None = None
    investment_value: float | None = None
    current_value: float | None = None
    pnl: float | None = None
    pnl_pct: float | None = None
    sector: str | None = None
    source_reference: str = ""
    raw_row: dict[str, Any] | None = None


class PortfolioParserError(ValueError):
    pass


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_header(value: Any) -> str:
    text = clean_text(value).lower()
    text = text.replace("&", "and")
    text = re.sub(r"[\u00a0\r\n\t]+", " ", text)
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalized_aliases(values: Iterable[str]) -> set[str]:
    return {normalize_header(value) for value in values}


SECURITY_ALIAS_KEYS = _normalized_aliases(SECURITY_ALIASES)
QUANTITY_ALIAS_KEYS = _normalized_aliases(QUANTITY_ALIASES)
ISIN_ALIAS_KEYS = _normalized_aliases(ISIN_ALIASES)
AVERAGE_COST_ALIAS_KEYS = _normalized_aliases(AVERAGE_COST_ALIASES)
CURRENT_PRICE_ALIAS_KEYS = _normalized_aliases(CURRENT_PRICE_ALIASES)
INVESTMENT_VALUE_ALIAS_KEYS = _normalized_aliases(INVESTMENT_VALUE_ALIASES)
CURRENT_VALUE_ALIAS_KEYS = _normalized_aliases(CURRENT_VALUE_ALIASES)
PNL_ALIAS_KEYS = _normalized_aliases(PNL_ALIASES)
PNL_PCT_ALIAS_KEYS = _normalized_aliases(PNL_PCT_ALIASES)
SECTOR_ALIAS_KEYS = _normalized_aliases(SECTOR_ALIASES)
PERCENTAGE_ALIAS_KEYS = _normalized_aliases(PERCENTAGE_ALIASES)


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if not text or text.lower() in {"-", "—", "–", "na", "n/a", "nil"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("₹", "").replace("Rs.", "").replace("Rs", "")
    text = text.replace(",", "").replace("%", "")
    text = re.sub(r"[^0-9+\-.]", "", text)
    if not text or text in {"+", "-", "."}:
        return None
    try:
        number = float(text)
        return -number if negative else number
    except ValueError:
        return None


def looks_like_isin(value: Any) -> bool:
    text = clean_text(value).upper().replace(" ", "")
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", text))


def detect_file_format(value: str | Path) -> str:
    suffix = Path(value).suffix.lower()
    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return "EXCEL"
    if suffix == ".pdf":
        return "PDF"
    raise PortfolioParserError(f"Unsupported portfolio file type: {suffix or 'unknown'}")


def filename_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", Path(value or "").stem.lower())


# Canonical PMS labels and spelling variants seen in appraisal files.  Matching is
# deliberately based on the complete cleaned identity (not a loose substring), so
# a client/portfolio title such as "Shital 360 One" is not silently merged with
# the 360 Pipe PMS merely because it contains "360 One".
PMS_CANONICAL_LABELS = {
    "2p2": "2P2",
    "360pipe": "360 Pipe",
    "91capital": "91 Capital",
    "bastion": "Bastion",
    "molecule": "Molecule",
    "ndpms": "NDPMS",
    "niveshaay": "Niveshaay",
    "persistence": "Persistence",
    "sageonecore": "Sageone Core",
    "sageonesmallcap": "Sageone Smallcap",
    "xponent": "Xponent",
    "emkay": "Emkay",
}

PMS_IDENTITY_ALIASES = {
    "2p2": ("2p2", "2 p 2", "two p two", "2 point 2 capital", "2point2 capital"),
    "360pipe": ("360 pipe", "360pipe", "360 one pipe", "360onepipe"),
    "91capital": (
        "91 capital", "91capital", "91 cap", "91cap", "ninety one capital",
        "ninetyone capital", "ninety-one capital", "ninety one cap", "ninetyone cap",
    ),
    "bastion": ("bastion", "bastion pms"),
    "molecule": ("molecule", "molecule pms"),
    "ndpms": ("ndpms", "nd pms", "n d p m s", "n.d.p.m.s."),
    "niveshaay": ("niveshaay", "niveshay", "niveshaay pms", "noveshaay"),
    "persistence": ("persistence", "persistence pms"),
    "sageonecore": ("sageone core", "sage one core", "sageonecore", "sage 1 core"),
    "sageonesmallcap": (
        "sageone smallcap", "sage one small cap", "sage one smallcap",
        "sageonesmallcap", "sage 1 small cap", "sageone small",
    ),
    "xponent": ("xponent", "xponent pms"),
    "emkay": ("emkay", "emkay global", "emkay global financial services"),
}

_PMS_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical_key, _aliases in PMS_IDENTITY_ALIASES.items():
    for _alias in _aliases:
        _alias_key = re.sub(r"[^a-z0-9]+", "", _alias.lower())
        _PMS_ALIAS_TO_CANONICAL[_alias_key] = _canonical_key

_GENERIC_PMS_IDENTITY_WORDS = {
    "pms", "fund", "scheme", "portfolio", "appraisal", "report", "statement",
    "monthly", "summary", "valuation", "holding", "holdings", "investment",
    "investments", "management", "services", "advisor", "advisors", "advisory",
    "limited", "ltd", "private", "pvt", "llp", "inc", "llc", "global", "financial",
}


def pms_identity_key(value: str) -> str:
    """Return a canonical PMS key across case, spacing, punctuation and known aliases.

    Unknown PMS names remain usable: they are normalized generically instead of
    being rejected for not matching a fixed filename template.
    """
    text = normalize_header(value)
    if not text:
        return ""
    text = re.sub(r"\bninety[\s-]*one\b", "91", text)
    text = re.sub(r"\bsmall[\s-]*cap\b", "smallcap", text)
    text = re.sub(r"\bnd\s+pms\b", "ndpms", text)
    text = re.sub(r"\b2\s+p\s+2\b", "2p2", text)
    text = re.sub(r"\bthree\s+hundred\s+sixty\b", "360", text)
    tokens = [token for token in text.split() if token not in _GENERIC_PMS_IDENTITY_WORDS]
    compact = re.sub(r"[^a-z0-9]+", "", " ".join(tokens))
    return _PMS_ALIAS_TO_CANONICAL.get(compact, compact)


def canonical_pms_name(value: str) -> str:
    """Return the preferred display name for a known PMS, otherwise keep its label."""
    key = pms_identity_key(value)
    if key in PMS_CANONICAL_LABELS:
        return PMS_CANONICAL_LABELS[key]
    cleaned = clean_text(value)
    return cleaned or "Unidentified PMS"


def normalize_pms_name(value: str) -> str:
    """Derive a stable PMS identity from a filename without strict naming rules."""
    stem = Path(value or "").stem
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    # Preserve the acronym as one identity before the generic "PMS" descriptor
    # is removed from other filenames.
    stem = re.sub(r"\bnd\s+pms\b", "NDPMS", stem, flags=re.IGNORECASE)
    stem = re.sub(
        r"\b(?:portfolio|appraisal|report|position|analysis|detailed|holding|holdings|statement|monthly|summary|valuation|pms|client|account|manager|snapshot|details|data|copy)\b",
        " ",
        stem,
        flags=re.IGNORECASE,
    )
    stem = re.sub(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        " ",
        stem,
        flags=re.IGNORECASE,
    )
    stem = re.sub(r"\b(?:q[1-4]|quarter\s*[1-4]|fy\s*\d{2,4}|\d{4}\s*q[1-4])\b", " ", stem, flags=re.IGNORECASE)
    # Remove common YYYY-MM, YYYY-MM-DD and DD-MM-YYYY filename dates
    # without leaving the month/day fragments behind as part of the PMS name.
    stem = re.sub(r"\b(?:19|20)\d{2}[-_./ ](?:0?[1-9]|1[0-2])(?:[-_./ ](?:0?[1-9]|[12]\d|3[01]))?\b", " ", stem)
    stem = re.sub(r"\b(?:0?[1-9]|[12]\d|3[01])[-_./ ](?:0?[1-9]|1[0-2])[-_./ ](?:19|20)\d{2}\b", " ", stem)
    stem = re.sub(r"\b(?:19|20)\d{2}\b", " ", stem)
    stem = re.sub(r"\b\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4}\b", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" ._-")
    if not stem or re.fullmatch(r"\d+", stem):
        raise PortfolioParserError(f"Could not derive a PMS name from filename: {value}")
    return canonical_pms_name(stem)


def _clean_identity_candidate(value: Any) -> str:
    """Remove report boilerplate from a possible PMS/manager name."""
    text = clean_text(value)
    if not text:
        return ""

    # Remove legal suffixes first, so a repeated manager prefix can be detected
    # even when the first occurrence ends in LLP/Limited.
    text = re.sub(r"\b(?:llp|limited|ltd|private|pvt\.?|inc\.?|llc)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:portfolio appraisal|appraisal report|portfolio report|detailed portfolio report)\b", " ", text, flags=re.IGNORECASE)
    parts = re.split(r"\s+[-–|:]\s+", text, maxsplit=1)
    if len(parts) == 2:
        left_key = normalize_header(parts[0])
        right_key = normalize_header(parts[1])
        if left_key and right_key.startswith(left_key):
            text = parts[1]

    # "Scheme XYZ Fund I" is a common factsheet heading; keep XYZ rather than
    # discarding everything after the word "scheme".
    text = re.sub(r"^\s*(?:scheme|fund|portfolio|pms)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:fund|portfolio|pms)\b.*$", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:account|client|customer|data as of|as of)\b.*$", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^A-Za-z0-9& ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_.,")
    if not text or len(text) < 2 or len(text) > 80:
        return ""
    if normalize_header(text) in {
        "portfolio", "appraisal", "report", "portfolio appraisal", "portfolio report",
        "holdings", "holding overview", "holding analysis", "equity", "company name",
        "snapshot", "factsheet", "strategy", "fund snapshot", "portfolio composition",
    }:
        return ""
    return text


def _pms_name_from_pdf(path: str | Path) -> str | None:
    """Infer a PMS label from an early report heading when the filename is generic."""
    priority: list[tuple[int, int, str]] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page_number, page in enumerate(pdf.pages[:3], 1):
                text = page.extract_text(layout=False) or ""
                for line_number, raw_line in enumerate(text.splitlines(), 1):
                    # On later pages only the top handful of lines are used;
                    # holdings/security rows are never an identity source.
                    if line_number > 14:
                        break
                    line = clean_text(raw_line)
                    if not line or len(line) > 100 or line[0].islower():
                        continue
                    lower = line.lower()
                    if any(looks_like_isin(token.strip(".,;()")) for token in line.split()):
                        continue
                    if any(term in lower for term in (
                        "investment objective", "address", "email", "phone", "telephone",
                        "account :", "account:", "as of", "data as of", "benchmark",
                        "market cap", "company name", "disclaimer", "registration no",
                        "investment manager", "registered office", "corporate office",
                    )):
                        continue

                    # Explicit scheme/fund/portfolio headings are strongest.
                    if re.search(r"\b(?:fund|portfolio|pms)\b", lower):
                        candidate = _clean_identity_candidate(line)
                        if candidate:
                            word_count = len(candidate.split())
                            score = 150 - (page_number - 1) * 18 - line_number
                            if word_count > 7:
                                score -= (word_count - 7) * 6
                            if lower.startswith(("the ", "portfolio ", "fund ", "scheme ")):
                                score -= 8
                            priority.append((score, page_number, candidate))

                    # Legal entity labels are a weaker fallback. Ignore long
                    # commentary sentences so phrases like "build a portfolio"
                    # cannot be mistaken for a manager name.
                    if re.search(r"\b(?:llp|limited|ltd|private|pvt\.?|llc)\b", lower):
                        candidate = _clean_identity_candidate(line)
                        if candidate and len(candidate.split()) <= 7:
                            score = 90 - (page_number - 1) * 18 - line_number
                            priority.append((score, page_number, candidate))
    except Exception:
        return None

    if not priority:
        return None
    priority.sort(key=lambda item: (item[0], -item[1], -len(item[2])), reverse=True)
    return priority[0][2]


def _pms_name_from_excel(path: str | Path) -> str | None:
    """Infer a PMS name from workbook title cells when the filename is generic."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    candidates: list[tuple[int, int, str]] = []
    try:
        for sheet_index, sheet in enumerate(workbook.worksheets):
            title = normalize_header(sheet.title)
            if any(skip in title for skip in ("understanding the report", "index sheet", "instructions", "notes", "glossary")):
                continue
            for row_index, row in enumerate(sheet.iter_rows(min_row=1, max_row=12, min_col=1, max_col=8, values_only=True), 1):
                for col_index, value in enumerate(row, 1):
                    text = clean_text(value)
                    if not text or len(text) > 100 or re.fullmatch(r"[\d.,% -]+", text):
                        continue
                    lower = text.lower()
                    if any(term in lower for term in (
                        "report as of", "report generation", "member :", "registered office",
                        "corporate office", "email", "tel no", "account", "holding report",
                        "understanding the report", "description", "field name", "total investment",
                        "wealth and investment limited", "securities limited", "depository",
                        "custodian", "compliance officer",
                    )):
                        continue
                    candidate = _clean_identity_candidate(text)
                    if not candidate:
                        continue
                    score = 24 - row_index * 2 - col_index
                    if re.search(r"\b(?:capital|fund|portfolio|pms)\b", lower):
                        score += 75
                    elif re.search(r"\bwealth\b", lower):
                        score += 8
                    if row_index <= 2 and col_index <= 2:
                        score += 20
                    if "consolidated" in title and re.search(r"\bcapital\b", lower):
                        score += 10
                    candidates.append((score, sheet_index, candidate))
    finally:
        workbook.close()

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return candidates[0][2]


def identify_pms_name(
    filename: str,
    path: str | Path,
    *,
    archive_path: str | None = None,
) -> str:
    """Identify PMS flexibly from the filename, ZIP folder, or report content.

    No exact filename template is required. Informative filenames are preferred
    because they generally provide the shortest, most stable PMS label. Generic
    names such as ``report.pdf`` fall back to report text or workbook title cells.
    """
    try:
        filename_name = normalize_pms_name(filename)
        if filename_name:
            return filename_name
    except PortfolioParserError:
        pass

    if archive_path:
        folder = Path(str(archive_path).replace("\\", "/")).parent.name
        if folder:
            try:
                folder_name = normalize_pms_name(folder)
                if folder_name:
                    return folder_name
            except PortfolioParserError:
                pass

    try:
        file_format = detect_file_format(path)
        content_name = _pms_name_from_pdf(path) if file_format == "PDF" else _pms_name_from_excel(path)
        if content_name:
            return canonical_pms_name(content_name)
    except Exception:
        pass

    # Keep otherwise-unidentifiable files processable and distinct. This is
    # intentionally a fallback rather than a rigid naming-rule failure.
    stem = Path(filename or path).stem.strip() or "Report"
    return f"Unidentified PMS - {stem}"[:200]


def canonical_header(value: Any) -> str | None:
    normalized = normalize_header(value)
    if not normalized:
        return None
    if normalized in SECURITY_ALIAS_KEYS:
        return "security_name"
    if normalized in QUANTITY_ALIAS_KEYS:
        return "quantity"
    if normalized in ISIN_ALIAS_KEYS:
        return "isin"
    if normalized in AVERAGE_COST_ALIAS_KEYS:
        return "average_cost"
    if normalized in CURRENT_PRICE_ALIAS_KEYS:
        return "current_price"
    if normalized in INVESTMENT_VALUE_ALIAS_KEYS:
        return "investment_value"
    if normalized in CURRENT_VALUE_ALIAS_KEYS:
        return "current_value"
    if normalized in PNL_ALIAS_KEYS:
        return "pnl"
    if normalized in PNL_PCT_ALIAS_KEYS:
        return "pnl_pct"
    if normalized in SECTOR_ALIAS_KEYS:
        return "sector"
    if normalized in PERCENTAGE_ALIAS_KEYS:
        return "percentage"

    # Narrow substring fallback for compound headings such as "Security / Scrip Name".
    for alias in SECURITY_ALIAS_KEYS:
        if alias and alias in normalized:
            return "security_name"
    for alias in QUANTITY_ALIAS_KEYS:
        if alias and alias in normalized:
            return "quantity"
    for alias in ISIN_ALIAS_KEYS:
        if alias and alias in normalized:
            return "isin"
    return None


def _quantity_header_score(value: Any) -> int:
    normalized = normalize_header(value)
    if not normalized:
        return -10000
    score = 0
    if normalized in QUANTITY_ALIAS_KEYS:
        score += 100
    positive_terms = ("quantity", "qty", "qnty", "unit", "units", "share", "shares", "holding", "holdings", "position", "balance")
    negative_terms = ("value", "amount", "price", "rate", "cost", "pnl", "profit", "loss", "return", "percent", "%", "allocation", "weight")
    for term in positive_terms:
        if normalize_header(term) in normalized:
            score += 16
    for term in negative_terms:
        if normalize_header(term) in normalized:
            score -= 28
    if any(term in normalized for term in ("current", "closing", "balance", "net")):
        score += 12
    if any(term in normalized for term in ("free", "pledged", "blocked")):
        score -= 8
    return score


def _metric_header_score(value: Any) -> tuple[int, str | None]:
    normalized = normalize_header(value)
    if not normalized:
        return -10000, None
    if normalized in QUANTITY_ALIAS_KEYS:
        return 120, "quantity"
    if normalized in PERCENTAGE_ALIAS_KEYS:
        return 120, "percentage"
    q_score = _quantity_header_score(value)
    percent_terms = ("%", "percent", "percentage", "allocation", "weight", "assets")
    p_score = 0
    for term in percent_terms:
        if term in normalized:
            p_score += 28
    if any(term in normalized for term in ("holding", "portfolio", "share")):
        p_score += 12
    if any(term in normalized for term in ("value", "amount", "price", "cost", "pnl", "gain", "return")):
        p_score -= 22
    return (p_score, "percentage") if p_score > q_score and p_score >= 30 else (q_score, "quantity")


def _detect_excel_header(rows: list[list[Any]]) -> tuple[int, dict[int, str], str]:
    best: tuple[int, dict[int, str], int, str] | None = None
    for idx, row in enumerate(rows[:100]):
        mapped: dict[int, str] = {}
        for col_idx, value in enumerate(row):
            key = canonical_header(value)
            if key and key not in mapped.values():
                mapped[col_idx] = key
        metric_candidates = [(_metric_header_score(value), col_idx) for col_idx, value in enumerate(row) if col_idx not in mapped]
        metric_candidates = [item for item in metric_candidates if item[0][0] >= 30]
        if "quantity" not in mapped.values() and "percentage" not in mapped.values() and metric_candidates:
            (score, metric), col_idx = max(metric_candidates, key=lambda item: item[0][0])
            mapped[col_idx] = metric
        # Some exports intentionally use a very short header such as "Equity"
        # or "Scrip" for the name column while the second column carries the
        # semantic metric. If the metric is confidently detected, infer the first
        # populated text column as the security-name field rather than requiring a
        # vendor-specific header.
        if "security_name" not in mapped.values() and ({"quantity", "percentage"} & set(mapped.values())):
            metric_col = next(index for index, semantic in mapped.items() if semantic in {"quantity", "percentage"})
            candidate_cols = [
                i for i, value in enumerate(row)
                if i != metric_col and clean_text(value) and normalize_header(value) not in {
                    "isin", "isin code", "isin no", "isin number"
                }
            ]
            if candidate_cols:
                mapped[candidate_cols[0]] = "security_name"
        metric_type = "PERCENTAGE" if "percentage" in mapped.values() else "QUANTITY"
        score = len(mapped) + (4 if "security_name" in mapped.values() else 0) + (4 if metric_type.lower() in mapped.values() else 0) + (2 if "isin" in mapped.values() else 0)
        if best is None or score > best[2]:
            best = (idx, mapped, score, metric_type)
    if best is None or "security_name" not in best[1].values() or not ({"quantity", "percentage"} & set(best[1].values())):
        raise PortfolioParserError("Could not identify a security/name column and a holding metric column in the workbook.")
    return best[0], best[1], best[3]


def _normalize_percentage_value(value: Any, *, raw_header: str = "") -> float | None:
    parsed = parse_number(value)
    if parsed is None:
        return None
    text = clean_text(value)
    # Explicit percent strings are already expressed in percentage points.
    if "%" in text:
        return parsed
    # Spreadsheet exports frequently encode 5.36% as 0.0536 when the cell
    # is numeric. Normalize fractional proportions into percentage points while
    # leaving values already expressed as percentage points (e.g. 5.36) intact.
    # Do not require an exact alias match here: headers such as "% of assets"
    # can be correctly classified as percentage columns by _metric_header_score
    # but were previously missed by the exact-membership check, producing a
    # 100x understatement when the report workbook later converted points back
    # to Excel percentage fractions.
    _, inferred_metric = _metric_header_score(raw_header)
    is_percentage_header = (
        normalize_header(raw_header) in PERCENTAGE_ALIAS_KEYS
        or inferred_metric == "percentage"
    )
    if abs(parsed) <= 1.0 and is_percentage_header:
        return parsed * 100.0
    return parsed


def _is_non_security_name(value: str) -> bool:
    key = normalize_header(value)
    if not key:
        return True
    excluded = (
        "total", "grand total", "equity", "cash", "cash and equivalent", "cash and cash equivalent", "cash rec payable",
        "cash receivable", "bank account", "bank accounts", "accrued income", "listed",
        "mutual fund", "debt", "fixed income", "note", "notes",
    )
    return key in excluded or key.startswith("total ")

def _parse_excel_rows(rows: list[list[Any]], source: str) -> list[ParsedHolding]:
    header_idx, mapped, metric_type = _detect_excel_header(rows)
    detected = {semantic: clean_text(rows[header_idx][index]) for index, semantic in mapped.items() if index < len(rows[header_idx])}
    metric_col = next(index for index, semantic in mapped.items() if semantic in {"quantity", "percentage"})
    metric_header = clean_text(rows[header_idx][metric_col])
    output: list[ParsedHolding] = []
    for row_num, row in enumerate(rows[header_idx + 1 :], header_idx + 2):
        values = {semantic: row[idx] if idx < len(row) else None for idx, semantic in mapped.items()}
        raw = {str(idx): clean_text(value) for idx, value in enumerate(row) if clean_text(value)}
        raw["__detected_columns__"] = detected
        raw["__headers__"] = {str(index): clean_text(value) for index, value in enumerate(rows[header_idx]) if clean_text(value)}
        security = clean_text(values.get("security_name"))
        raw_metric = values.get("percentage") if "percentage" in values else values.get("quantity")
        quantity = _normalize_percentage_value(raw_metric, raw_header=metric_header) if metric_type == "PERCENTAGE" else parse_number(raw_metric)
        isin = clean_text(values.get("isin")).upper().replace(" ", "") or None
        if not security or quantity is None:
            continue
        if isin and not looks_like_isin(isin):
            isin = None
        if _is_non_security_name(security):
            continue
        if len(security) > 150:
            continue
        avg_cost = parse_number(values.get("average_cost"))
        current_price = parse_number(values.get("current_price"))
        investment_value = parse_number(values.get("investment_value"))
        current_value = parse_number(values.get("current_value"))
        pnl = parse_number(values.get("pnl"))
        pnl_pct = parse_number(values.get("pnl_pct"))
        if current_value is None and current_price is not None:
            current_value = quantity * current_price
        if investment_value is None and avg_cost is not None:
            investment_value = quantity * avg_cost
        if pnl is None and current_value is not None and investment_value is not None:
            pnl = current_value - investment_value
        if pnl_pct is None and pnl is not None and investment_value:
            pnl_pct = pnl / investment_value * 100
        output.append(
            ParsedHolding(
                isin=isin,
                security_name=security,
                quantity=float(quantity),
                measurement_type=metric_type,
                average_cost=avg_cost,
                current_price=current_price,
                investment_value=investment_value,
                current_value=current_value,
                pnl=pnl,
                pnl_pct=pnl_pct,
                source_reference=f"{source}:row-{row_num}",
                raw_row=raw,
            )
        )
    return output


def parse_excel(path: str | Path) -> list[ParsedHolding]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    candidates: list[tuple[str, list[ParsedHolding]]] = []
    try:
        for sheet in workbook.worksheets:
            title = clean_text(sheet.title)
            title_key = normalize_header(title)
            if any(skip in title_key for skip in ("understanding the report", "index sheet", "instructions", "notes", "glossary")):
                continue
            rows = [list(row) for row in sheet.iter_rows(values_only=True)]
            if not rows:
                continue
            try:
                parsed = _parse_excel_rows(rows, f"{Path(path).name}:{sheet.title}")
            except PortfolioParserError:
                continue
            if parsed:
                candidates.append((title, parsed))
    finally:
        workbook.close()

    if not candidates:
        raise PortfolioParserError("No security-level table containing a recognizable holding metric could be extracted from the Excel workbook.")

    consolidated = [rows for title, rows in candidates if "consolidated" in normalize_header(title)]
    results = max(consolidated or [rows for _, rows in candidates], key=len)
    return _dedupe_rows(results)


def _group_words_by_line(words: list[dict[str, Any]], tolerance: float = 2.4) -> list[list[dict[str, Any]]]:
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda item: (float(item["top"]), float(item["x0"]))):
        if not lines or abs(float(word["top"]) - float(lines[-1][0]["top"])) > tolerance:
            lines.append([word])
        else:
            lines[-1].append(word)
    return lines


def _word_text(word: dict[str, Any]) -> str:
    return clean_text(word.get("text"))


def _word_number(word: dict[str, Any], *, include_percent: bool = False) -> float | None:
    text = _word_text(word)
    if not text or ("%" in text and not include_percent):
        return None
    return parse_number(text)


def _find_header(lines: list[list[dict[str, Any]]]) -> tuple[int, dict[str, float]] | None:
    for index, line in enumerate(lines):
        joined = normalize_header(" ".join(_word_text(word) for word in line))
        if not joined:
            continue
        has_security = any(alias in joined for alias in SECURITY_ALIAS_KEYS | {"item description"})
        has_quantity = any(alias in joined for alias in QUANTITY_ALIAS_KEYS)
        has_percentage = any(alias in joined for alias in PERCENTAGE_ALIAS_KEYS)
        if not (has_security and (has_quantity or has_percentage)):
            continue

        positions: dict[str, float] = {}
        # Multi-word headers must use the x-position of the first word in the
        # phrase. This matters for layouts such as "Item Description" and
        # "Scrip Name" where the actual security data begins well before the
        # second header word.
        lowered_words = [normalize_header(_word_text(word)) for word in line]
        for phrase in (("item", "description"), ("scrip", "name"), ("security", "name"), ("item", "name")):
            for i in range(len(lowered_words) - 1):
                if tuple(lowered_words[i:i + 2]) == phrase:
                    positions["security_x"] = float(line[i]["x0"])
                    break
            if "security_x" in positions:
                break
        for word in line:
            n = normalize_header(_word_text(word))
            x = float(word["x0"])
            if "security_x" not in positions and (n in SECURITY_ALIAS_KEYS or n == "item description"):
                positions.setdefault("security_x", x)
            elif n in QUANTITY_ALIAS_KEYS:
                positions.setdefault("metric_x", x)
                positions.setdefault("metric_type", "QUANTITY")
                positions.setdefault("metric_header", _word_text(word))
            elif n in PERCENTAGE_ALIAS_KEYS:
                positions.setdefault("metric_x", x)
                positions.setdefault("metric_type", "PERCENTAGE")
                positions.setdefault("metric_header", _word_text(word))
            elif n == "%" and has_percentage:
                positions.setdefault("metric_x", x)
                positions.setdefault("metric_type", "PERCENTAGE")
                positions.setdefault("metric_header", "Holding %")
            elif n in ISIN_ALIAS_KEYS:
                positions.setdefault("isin_x", x)
            elif n in CURRENT_VALUE_ALIAS_KEYS:
                positions.setdefault("market_x", x)
            elif n in CURRENT_PRICE_ALIAS_KEYS:
                positions.setdefault("price_x", x)
            elif n in AVERAGE_COST_ALIAS_KEYS:
                positions.setdefault("unit_cost_x", x)
            elif n in INVESTMENT_VALUE_ALIAS_KEYS:
                positions.setdefault("cost_x", x)
            elif n in PNL_ALIAS_KEYS:
                positions.setdefault("gain_x", x)
        if "metric_x" not in positions and has_percentage:
            percent_word = next((word for word in line if _word_text(word) == "%"), None)
            if percent_word is not None:
                positions["metric_x"] = float(percent_word["x0"])
                positions["metric_type"] = "PERCENTAGE"
                positions["metric_header"] = joined
        if "metric_x" in positions:
            positions.setdefault("security_x", min(float(word["x0"]) for word in line))
            return index, positions
    return None


def _nearest_numeric(
    line: list[dict[str, Any]],
    x: float,
    *,
    max_distance: float = 75.0,
    include_percent: bool = False,
) -> tuple[float, dict[str, Any]] | None:
    candidates: list[tuple[float, float, dict[str, Any]]] = []
    for word in line:
        number = _word_number(word, include_percent=include_percent)
        if number is None:
            continue
        center = (float(word["x0"]) + float(word["x1"])) / 2
        distance = abs(center - x)
        if distance <= max_distance:
            candidates.append((distance, center, word))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    distance, _, word = candidates[0]
    return _word_number(word, include_percent=include_percent) or 0.0, word


def _clean_pdf_name(name: str, isin: str | None) -> str:
    text = clean_text(name)
    if isin:
        text = re.sub(re.escape(isin), " ", text, flags=re.IGNORECASE)
    # Strip common security-description suffixes used by PMS statements.
    text = re.sub(r"\b(?:eq|equity|new|fv|rs\.?|f\.?v\.?|re\.?\d*|rs\.?\d+/?-?)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ./-")
    text = re.sub(r"\s+\d+(?:/[-–—]?)?\.?$", "", text).strip(" ./-")
    # Some PDF table extractors concatenate visually separate company-name
    # words (e.g. "AartiPharmalabs" or "CGPowerandIndustrialSolutions").
    # Restore only clear title-case boundaries in long single tokens; leave
    # short mixed-case brand names such as "eClerx" untouched.
    spaced_tokens: list[str] = []
    for token in text.split():
        if len(token) >= 12:
            token = re.sub(r"(?<=[A-Za-z])(?=(?:and|of|the|for|in|on|to)[A-Z])", " ", token)
            token = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token)
            token = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", token)
            token = re.sub(r"(?<=[a-z])(?=\d)", " ", token)
            token = re.sub(r"(?<=[0-9])(?=[A-Z][a-z])", " ", token)
        spaced_tokens.extend(token.split())
    tokens = spaced_tokens
    # Some appraisal PDFs have both a trading symbol and the human-readable
    # description (e.g. "AARTIDRUGS AARTI DRUGS LTD"). If the first token is
    # literally the concatenation of the next one or two name tokens, drop the
    # redundant symbol. This is deliberately structural, not PMS-specific.
    if len(tokens) >= 3:
        first = re.sub(r"[^A-Z0-9]", "", tokens[0].upper())
        next_one = re.sub(r"[^A-Z0-9]", "", tokens[1].upper())
        next_two = re.sub(r"[^A-Z0-9]", "", tokens[2].upper())
        if first and (first == next_one + next_two or (next_one and first.startswith(next_one) and len(first) - len(next_one) <= 6)) and len(first) <= 20:
            tokens = tokens[1:]
    return " ".join(tokens)[:200]


def _parse_pdf_position_pages(path: str | Path) -> list[ParsedHolding]:
    results: list[ParsedHolding] = []
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if not words:
                continue
            lines = _group_words_by_line(words)
            header = _find_header(lines)
            if not header:
                continue
            header_index, pos = header
            current_name: str | None = None
            last_data_top: float | None = None
            metric_type = pos.get("metric_type", "QUANTITY")
            metric_x = float(pos["metric_x"])
            metric_header = str(pos.get("metric_header") or metric_type)
            for line_number, line in enumerate(lines[header_index + 1 :], header_index + 2):
                top = float(line[0]["top"])
                text = clean_text(" ".join(_word_text(word) for word in line))
                lower = text.lower()
                if not text:
                    continue
                if any(token in lower for token in ("asset class distribution", "portfolio position analysis")):
                    continue
                if lower.startswith(("total", "grand total", "cash and equivalent", "bank account", "accrued income", "equity", "listed")):
                    continue
                qty_hit = _nearest_numeric(
                    line,
                    metric_x,
                    max_distance=85.0,
                    include_percent=metric_type == "PERCENTAGE",
                )
                if qty_hit is None:
                    # Wrapped security-name line: append only text to the left of the quantity column.
                    if current_name and last_data_top is not None and top - last_data_top < 30:
                        left_words = [w for w in line if float(w["x0"]) < metric_x - 5 and _word_number(w) is None]
                        continuation = clean_text(" ".join(_word_text(w) for w in left_words))
                        if continuation:
                            current_name = clean_text(f"{current_name} {continuation}")
                    continue

                raw_metric = qty_hit[1].get("text")
                quantity = (
                    _normalize_percentage_value(raw_metric, raw_header=metric_header)
                    if metric_type == "PERCENTAGE"
                    else parse_number(raw_metric)
                )
                if quantity is None:
                    continue

                # Build the security name from the semantic name region rather than
                # from all text before Quantity. This prevents ticker/ISIN columns
                # from becoming part of the stored security name.
                name_end_x = pos.get("isin_x") or metric_x
                if "isin_x" in pos:
                    # ISIN gives us a strong right-hand boundary. Some PDFs align
                    # the multi-word header differently from the data column, so
                    # using the entire left region is more reliable here.
                    security_words = [
                        word for word in line
                        if float(word["x0"]) < name_end_x - 5
                        and _word_number(word) is None
                        and not looks_like_isin(_word_text(word))
                    ]
                else:
                    security_words = [
                        word for word in line
                        if pos.get("security_x", 0) - 4 <= float(word["x0"]) < name_end_x - 5
                        and _word_number(word) is None
                        and not looks_like_isin(_word_text(word))
                    ]
                security = clean_text(" ".join(_word_text(word) for word in security_words))
                if not security:
                    # Some layouts have a single text block to the left of Quantity.
                    left_words = [word for word in line if float(word["x0"]) < metric_x - 5 and _word_number(word) is None]
                    security = clean_text(" ".join(_word_text(word) for word in left_words))

                isin = None
                for word in line:
                    token = _word_text(word).upper().replace(" ", "")
                    if looks_like_isin(token):
                        isin = token
                        break

                security = _clean_pdf_name(security, isin)
                if _is_non_security_name(security):
                    continue
                rejected = (
                    "generated by", "e-mail id", "email id", "phone number", "sebi registration",
                    "discrepancy", "page:", "registration no", "disclaimer", "contact",
                )
                if any(token in security.lower() for token in rejected):
                    continue
                if len(security) > 180:
                    continue

                current_name = security
                last_data_top = top

                current_value = None
                current_price = None
                investment_value = None
                average_cost = None
                pnl = None
                pnl_pct = None

                if pos.get("price_x") is not None:
                    hit = _nearest_numeric(line, pos["price_x"], max_distance=80.0)
                    current_price = hit[0] if hit else None
                if pos.get("market_x") is not None:
                    hit = _nearest_numeric(line, pos["market_x"], max_distance=90.0)
                    current_value = hit[0] if hit else None
                if pos.get("unit_cost_x") is not None:
                    hit = _nearest_numeric(line, pos["unit_cost_x"], max_distance=75.0)
                    average_cost = hit[0] if hit else None
                if pos.get("cost_x") is not None:
                    hit = _nearest_numeric(line, pos["cost_x"], max_distance=85.0)
                    investment_value = hit[0] if hit else None
                if pos.get("gain_x") is not None:
                    hit = _nearest_numeric(line, pos["gain_x"], max_distance=85.0)
                    pnl = hit[0] if hit else None
                percent_words = [parse_number(_word_text(word)) for word in line if "%" in _word_text(word)]
                percent_words = [value for value in percent_words if value is not None]
                if percent_words:
                    pnl_pct = percent_words[0]
                if current_value is not None and investment_value is not None:
                    pnl = current_value - investment_value
                    pnl_pct = pnl / investment_value * 100 if investment_value else None

                results.append(
                    ParsedHolding(
                        isin=isin,
                        security_name=security,
                        quantity=float(quantity),
                        measurement_type=metric_type,
                        average_cost=average_cost,
                        current_price=current_price,
                        investment_value=investment_value,
                        current_value=current_value,
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        source_reference=f"{Path(path).name}:page-{page_number}:line-{line_number}",
                        raw_row={
                            "text": text,
                            "__detected_columns__": {
                                "security_name": "detected semantic region",
                                "quantity" if metric_type == "QUANTITY" else "percentage": metric_header,
                                "isin": "detected ISIN header" if "isin_x" in pos else None,
                            },
                        },
                    )
                )
    return _dedupe_rows(results)


def _parse_pdf_table_fallback(path: str | Path) -> list[ParsedHolding]:
    """Extract holdings from tables with explicit security and metric headers."""
    results: list[ParsedHolding] = []
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            for table_index, table in enumerate(page.extract_tables()):
                if not table:
                    continue
                header_index = None
                mapped: dict[int, str] = {}
                for row_index, row in enumerate(table[:35]):
                    trial: dict[int, str] = {}
                    for col_index, value in enumerate(row):
                        key = canonical_header(value)
                        if key and key not in trial.values():
                            trial[col_index] = key
                    if "security_name" in trial.values() and ({"quantity", "percentage"} & set(trial.values())):
                        header_index = row_index
                        mapped = trial
                        break
                if header_index is None:
                    continue

                metric_semantic = next(
                    semantic for semantic in mapped.values() if semantic in {"quantity", "percentage"}
                )
                metric_index = next(index for index, semantic in mapped.items() if semantic == metric_semantic)
                metric_header = clean_text(table[header_index][metric_index])
                metric_type = "PERCENTAGE" if metric_semantic == "percentage" else "QUANTITY"

                for row_number, row in enumerate(table[header_index + 1 :], header_index + 2):
                    values = {semantic: row[index] if index < len(row) else None for index, semantic in mapped.items()}
                    security = clean_text(values.get("security_name"))
                    raw_metric = values.get(metric_semantic)
                    metric_value = (
                        _normalize_percentage_value(raw_metric, raw_header=metric_header)
                        if metric_type == "PERCENTAGE"
                        else parse_number(raw_metric)
                    )
                    if not security or metric_value is None:
                        continue
                    isin = clean_text(values.get("isin")).upper().replace(" ", "") or None
                    if isin and not looks_like_isin(isin):
                        isin = None
                    security = _clean_pdf_name(security, isin)
                    if _is_non_security_name(security) or len(security) > 180:
                        continue
                    results.append(
                        ParsedHolding(
                            isin=isin,
                            security_name=security,
                            quantity=float(metric_value),
                            measurement_type=metric_type,
                            average_cost=parse_number(values.get("average_cost")),
                            current_price=parse_number(values.get("current_price")),
                            investment_value=parse_number(values.get("investment_value")),
                            current_value=parse_number(values.get("current_value")),
                            pnl=parse_number(values.get("pnl")),
                            pnl_pct=parse_number(values.get("pnl_pct")),
                            source_reference=f"{Path(path).name}:page-{page_number}:table-{table_index}:row-{row_number}",
                            raw_row={
                                **{str(index): clean_text(value) for index, value in enumerate(row) if clean_text(value)},
                                "__headers__": {str(index): clean_text(value) for index, value in enumerate(table[header_index]) if clean_text(value)},
                            },
                        )
                    )
    return _dedupe_rows(results)


def _parse_pdf_allocation_sections(path: str | Path) -> list[ParsedHolding]:
    """Extract name + percentage holdings from narrative/2-column allocation layouts.

    This intentionally relies on document semantics (allocation/composition sections
    and percentage-bearing rows), not on a PMS/vendor name.
    """
    results: list[ParsedHolding] = []
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            lines = _group_words_by_line(words)
            in_allocation = False
            seen_rows = 0
            for line_number, line in enumerate(lines, 1):
                text = clean_text(" ".join(_word_text(w) for w in line))
                lower = text.lower()
                if not text:
                    continue
                if re.search(r"\b(portfolio composition|portfolio holdings|holdings composition|portfolio allocation)\b", lower):
                    in_allocation = True
                    seen_rows = 0
                    continue
                if not in_allocation:
                    continue
                if lower.startswith(("note:", "performance", "disclaimer", "factsheet")):
                    if seen_rows:
                        in_allocation = False
                    continue
                percent_words = [w for w in line if "%" in _word_text(w)]
                if not percent_words:
                    continue
                # A composition row is one or more name/percentage pairs. Ignore
                # isolated percentages from charts/tables by requiring meaningful text.
                for percent_word in percent_words:
                    value = parse_number(_word_text(percent_word))
                    if value is None:
                        continue
                    px = float(percent_word["x0"])
                    left = [w for w in line if float(w["x1"]) < px - 2 and not looks_like_isin(_word_text(w))]
                    # For two-column rows, select the text since the prior percentage;
                    # otherwise take the text immediately preceding this percentage.
                    prev_percent_x = max((float(w["x1"]) for w in percent_words if float(w["x1"]) < px), default=None)
                    if prev_percent_x is not None:
                        left = [w for w in left if float(w["x0"]) > prev_percent_x + 4]
                    name = clean_text(" ".join(_word_text(w) for w in left))
                    name = _clean_pdf_name(name, None)
                    if not name or len(name) > 160:
                        continue
                    if _is_non_security_name(name):
                        continue
                    if any(token in name.lower() for token in ("allocation", "portfolio composition", "note")):
                        continue
                    results.append(ParsedHolding(
                        isin=None,
                        security_name=name,
                        quantity=float(value),
                        measurement_type="PERCENTAGE",
                        source_reference=f"{Path(path).name}:page-{page_number}:line-{line_number}",
                        raw_row={"text": text, "__detected_columns__": {"security_name": "allocation text region", "percentage": "percent-bearing column"}},
                    ))
                    seen_rows += 1
    return _dedupe_rows(results)

def _parse_pdf_numbered_percentage_rows(path: str | Path) -> list[ParsedHolding]:
    """Read numbered security/weight tables from text-based PDF disclosures.

    Some multi-page portfolio disclosures have a well-structured table but the
    PDF table extractor only returns page one (and can mistake a KPI count for a
    security row).  Text extraction preserves the serial number, security name
    and terminal weight on each line, so it is a reliable page-by-page fallback.
    """
    results: list[ParsedHolding] = []
    row_pattern = re.compile(
        r"^\s*(?P<serial>\d{1,3})\s+(?P<security>.+?)\s+(?P<weight>[+-]?\d+(?:\.\d+)?)\s*%\s*$"
    )
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            page_text = page.extract_text(layout=False) or ""
            lines = [clean_text(line) for line in page_text.splitlines()]
            header_index = next(
                (
                    index
                    for index, line in enumerate(lines)
                    if "security" in normalize_header(line)
                    and "weight" in normalize_header(line)
                ),
                None,
            )
            if header_index is None:
                continue

            for line_number, line in enumerate(lines[header_index + 1 :], header_index + 2):
                if not line:
                    continue
                lower = line.lower()
                if lower.startswith(("total", "balance", "important information", "disclaimer", "risk", "no advice")):
                    break
                match = row_pattern.match(line)
                if not match:
                    continue
                security = _clean_pdf_name(match.group("security"), None)
                if not security or _is_non_security_name(security):
                    continue
                # A numbered holdings disclosure may include digits in a company
                # name (e.g. One 97 Communications); the serial alone is stripped
                # by the regex and the remaining name must contain a letter.
                if not re.search(r"[A-Za-z]", security) or len(security) > 180:
                    continue
                weight = parse_number(match.group("weight"))
                if weight is None:
                    continue
                results.append(
                    ParsedHolding(
                        isin=None,
                        security_name=security,
                        quantity=float(weight),
                        measurement_type="PERCENTAGE",
                        source_reference=f"{Path(path).name}:page-{page_number}:line-{line_number}",
                        raw_row={
                            "text": line,
                            "serial_number": int(match.group("serial")),
                            "__detected_columns__": {
                                "security_name": "numbered PDF security column",
                                "percentage": "terminal weight percent",
                            },
                        },
                    )
                )
    return _dedupe_rows(results)


def _dedupe_rows(rows: list[ParsedHolding]) -> list[ParsedHolding]:
    deduped: dict[tuple[str | None, str], ParsedHolding] = {}
    for row in rows:
        name_key = re.sub(r"[^a-z0-9]+", "", row.security_name.lower())
        key = (row.isin, name_key)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = row
        elif not existing.isin and row.isin:
            deduped[key] = row
    return list(deduped.values())


def parse_pdf(path: str | Path) -> list[ParsedHolding]:
    # Prefer structured tables, but also inspect numbered percentage disclosures
    # page-by-page. Some PDF table extractors return only the first page and can
    # mistake a document-level KPI (e.g. "31 holdings") for a security row.
    table_rows = _parse_pdf_table_fallback(path)
    numbered_rows = _parse_pdf_numbered_percentage_rows(path)
    if numbered_rows and len(numbered_rows) >= len(table_rows):
        results = numbered_rows
    else:
        results = table_rows
    if not results:
        results = _parse_pdf_position_pages(path)
    if not results:
        results = _parse_pdf_allocation_sections(path)
    if not results:
        raise PortfolioParserError(
            "No security-level holdings table containing a recognizable holding metric could be extracted from the PDF. "
            "The report may be scanned/image-only, or its table exposes neither a unit holding nor an allocation/percentage metric."
        )
    return results


def parser_key_for_pms(file_format: str) -> str:
    return "pdf-adaptive-holdings" if file_format == "PDF" else "excel-adaptive-holdings"


def parse_file(path: str | Path, pms_name: str = "") -> tuple[str, list[ParsedHolding]]:
    file_format = detect_file_format(path)
    if file_format == "PDF":
        return parser_key_for_pms(file_format), parse_pdf(path)
    if file_format == "EXCEL":
        return parser_key_for_pms(file_format), parse_excel(path)
    raise PortfolioParserError(f"Unsupported portfolio file type: {file_format}")
