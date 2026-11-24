from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ii_platform.database.models import Company

from .parsers import ParsedHolding, clean_text


def normalize_security_name(value: str) -> str:
    text = (value or "").upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\b(IN|EQ|EQUITY|LTD|LIMITED|INDIA|THE|NEW|FV|RS)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_sector_map() -> dict[str, str]:
    path = Path("./data/portfolio-comparison/sector-map.json")
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def load_company_maps(db) -> tuple[dict[str, Company], dict[str, Company]]:
    rows = (await db.scalars(select(Company))).all()
    by_isin: dict[str, Company] = {}
    by_name: dict[str, Company] = {}
    for company in rows:
        if company.isin:
            by_isin[str(company.isin).upper().replace(" ", "")] = company
        by_name[normalize_security_name(company.legal_name)] = company
        for alias in company.aliases or []:
            by_name[normalize_security_name(str(alias))] = company
    return by_isin, by_name


def normalized_holding(
    row: ParsedHolding,
    *,
    company_by_isin: dict[str, Company],
    company_by_name: dict[str, Company],
    sector_map: dict[str, str],
) -> dict[str, Any]:
    name_key = normalize_security_name(row.security_name)
    isin_key = (row.isin or "").upper().replace(" ", "") or None
    company = company_by_isin.get(isin_key) if isin_key else None
    confidence = 1.0 if company else 0.0
    if company is None:
        company = company_by_name.get(name_key)
        confidence = 0.75 if company else 0.0

    security_name = row.security_name.strip() or isin_key or "Unknown Security"
    if company is not None and company.legal_name:
        security_name = str(company.legal_name).strip()
        name_key = normalize_security_name(security_name)

    sector = (row.sector or "").strip()
    if not sector and isin_key:
        sector = sector_map.get(isin_key, "")
    if not sector and name_key:
        sector = sector_map.get(name_key, "")

    return {
        "isin": isin_key,
        "security_name": security_name,
        "normalized_security_name": name_key,
        "quantity": float(row.quantity or 0.0),
        "measurement_type": row.measurement_type or "QUANTITY",
        "average_cost": row.average_cost,
        "current_price": row.current_price,
        "investment_value": row.investment_value,
        "current_value": row.current_value,
        "pnl": row.pnl,
        "pnl_pct": row.pnl_pct,
        "sector": sector or "Unmapped",
        "company_id": int(company.id) if company else None,
        "identity_confidence": confidence,
        "source_reference": row.source_reference,
        "raw_row": row.raw_row or {},
    }

# Portfolio comparison is intended to track listed operating-company equities,
# not every line item that can appear in a PMS appraisal. Keep this classification
# conservative: explicit instrument/product wording is excluded, while ordinary
# issuer names such as banks, asset managers and finance companies remain stocks.
_NON_EQUITY_NAME_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:etf|exchange[- ]traded fund)\b",
        r"\bmutual funds?\b|\bindex funds?\b|\b(?:liquid|debt|gilt|balanced|hybrid|equity|overnight|money market|small cap|mid cap|large cap|multi cap|flexi cap|focused|thematic|sectoral|short duration|ultra short|corporate bond|credit risk|dynamic bond|arbitrage|equity savings|multi asset|savings) funds?\b",
        r"\bfunds?\b|\bAIFs?\b|\balternative investment funds?\b|\bventure capital funds?\b|\bprivate equity funds?\b",
        r"\b(?:direct|regular) plan\b|\b(?:growth|dividend|idcw) option\b|\b(?:direct|regular) growth\b",
        r"\bfund units?\b|\bunits? of (?:the )?fund\b",
        r"^\s*cash\s*$|\bcash\b.{0,30}\b(?:equivalent|equivalents|balance|balances|in hand)\b|\bcash rec(?:eivable)?\b|\bcash pay(?:able)?\b",
        r"\bbank balances?\b|\bbank accounts?\b|\bsweep accounts?\b|\baccrued income\b|\bdividend receivable\b|\binterest receivable\b|\bother receivables?\b|\bsundry debtors?\b|\bmargin money\b|\btds receivable\b|\bgst receivable\b",
        r"\b(?:fixed|term) deposits?\b|\bcertificate of deposit\b|\bcommercial papers?\b",
        r"\bnon[- ]convertible debentures?\b|\bNCDs?\b|\bdebentures?\b|\bbonds?\b",
        r"\btreasury bills?\b|\bT[- ]?bills?\b|\bgovernment securities\b|\bG[- ]?secs?\b|\bSDLs?\b",
        r"\bsovereign gold bonds?\b|\bSGBs?\b",
        r"\bREITs?\b|\bInvITs?\b|\breal estate investment trusts?\b|\binfrastructure investment trusts?\b",
        r"\bpreference shares?\b|\bpreferred shares?\b|\bwarrants?\b|\bright entitlements?\b",
        r"\bderivatives?\b|\bfutures?\b|\boptions?\b|\bput options?\b|\bcall options?\b",
        r"\bbees\b",
        r"\bgovernment of india\b.*\b(?:19|20)\d{2}\b|\bGOI\b.*\b(?:19|20)\d{2}\b",
    )
)

_NON_EQUITY_CATEGORY_HEADERS = re.compile(
    r"\b(?:asset class|asset type|instrument type|security type|product type|"
    r"investment type|investment category|instrument category|security category|"
    r"nature of investment|holding type|classification|category)\b",
    re.IGNORECASE,
)

_NON_EQUITY_CATEGORY_VALUES = {
    "cash", "cash equivalent", "cash equivalents", "cash and cash equivalent",
    "cash and cash equivalents", "mutual fund", "mutual funds", "mf", "mfs",
    "etf", "etfs", "exchange traded fund", "exchange traded funds",
    "debt", "fixed income", "bond", "bonds", "debenture", "debentures",
    "reit", "reits", "invit", "invits", "derivative", "derivatives",
    "commodity", "commodities", "deposit", "deposits", "fixed deposit",
    "fixed deposits", "term deposit", "term deposits", "other assets",
}


def is_equity_stock(security_name: str, raw_row: dict[str, Any] | None = None) -> bool:
    """Return False for non-equity instruments commonly mixed into PMS appraisals.

    Excel/PDF table parsers retain original header/value pairs in ``__headers__``
    so explicit asset-class or instrument-type columns can take precedence over
    an otherwise ambiguous security label. Older persisted rows without that
    metadata are still filtered by their security name.
    """
    name = clean_text(security_name)
    if not name:
        return False
    for pattern in _NON_EQUITY_NAME_PATTERNS:
        if pattern.search(name):
            return False

    raw = raw_row if isinstance(raw_row, dict) else {}
    header_map = raw.get("__headers__")
    if isinstance(header_map, dict):
        for index, header in header_map.items():
            if not _NON_EQUITY_CATEGORY_HEADERS.search(clean_text(header)):
                continue
            category_value = clean_text(raw.get(str(index)))
            if category_value and any(pattern.search(category_value) for pattern in _NON_EQUITY_NAME_PATTERNS):
                return False
            category_key = re.sub(r"[^a-z]+", " ", category_value.lower()).strip()
            category_tokens = set(category_key.split())
            if category_key in _NON_EQUITY_CATEGORY_VALUES or category_tokens.intersection({"mf", "mfs", "etf", "etfs"}):
                return False

    # Some PDF extraction paths expose the complete position line rather than
    # a structured header map. Check the text only for explicit product wording.
    raw_text = clean_text(raw.get("text"))
    if raw_text and any(pattern.search(raw_text) for pattern in _NON_EQUITY_NAME_PATTERNS):
        return False
    return True
