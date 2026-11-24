from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def canonical_broker_name(value: object) -> str:
    """Normalize broker workbook filenames to stable display names.

    Long brand tokens may appear as part of filenames (for example
    ``ICICI_Prudential_Portfolio.xlsx``); short aliases are matched as complete
    normalized tokens to avoid false positives such as ``SBI`` inside a word.
    """
    raw = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.[^.]+$", "", raw).strip()
    normalized = normalize_text(stem)
    tokens = set(normalized.split())
    compact = re.sub(r"[^A-Z0-9]", "", stem.upper())
    aliases = (
        ("STATEBANKOFINDIA", "SBI", True), ("SBI", "SBI", False),
        ("ICICIPRUDENTIAL", "ICICI", True), ("ICICI", "ICICI", False),
        ("KOTAK", "Kotak", True), ("HDFC", "HDFC", True), ("AXIS", "Axis", True),
        ("360ONE", "360", True), ("IIFL", "360", False), ("360", "360", False),
        ("ADITYABIRLA", "Aditya Birla", True), ("ABSL", "Aditya Birla", False),
        ("FRANKLINTEMPLETON", "Franklin Templeton", True), ("FRANKLIN", "Franklin Templeton", True),
        ("MOTILALOSWAL", "Motilal Oswal", True), ("MOTILAL", "Motilal Oswal", True),
        ("NIPPONINDIA", "Nippon India", True), ("RELIANCE", "Nippon India", True),
        ("MIRAE", "Mirae Asset", True), ("EDELWEISS", "Edelweiss", True),
        ("BAJAJ", "Bajaj", True), ("CANARA", "Canara", True),
        ("BANKOFBARODA", "Bank of Baroda", True), ("BOB", "Bank of Baroda", False),
        ("DSP", "DSP", False), ("HSBC", "HSBC", True), ("INVESCO", "Invesco", True),
        ("TATA", "Tata", False), ("UTI", "UTI", False), ("BANDHAN", "Bandhan", True),
        ("SUNDARAM", "Sundaram", True), ("PGIM", "PGIM", False), ("QUANT", "Quant", True),
        ("MAHINDRA", "Mahindra", True), ("JMFINANCIAL", "JM Financial", True),
        ("JM", "JM Financial", False), ("ASK", "ASK", False),
    )
    for token, label, allow_substring in aliases:
        if (token in compact if allow_substring else token.casefold() in tokens):
            return label
    if "mprofit" in tokens:
        return "MProfit"
    return stem[:80] or "Broker source"


FUND_REPLACEMENTS = (
    (r"\bprudential\b", "pru"),
    (r"\bcorporate\b", "corp"),
    (r"\bsun life\b", "sl"),
    (r"\bplus\b", " "),
)
FUND_NOISE = re.compile(r"\b(?:direct|dir|regular|reg|plan|growth|option|cumulative|debt)\b")

GROUP_ID_ALIASES = {
    "gr infra": "gr infra trust",
    "gr infra group": "gr infra trust",
    "gr infra trust": "gr infra trust",
    "goi": "goi",
    "government of india": "goi",
    "tata": "tata",
    "tata group": "tata",
}

# Display aliases only: no ownership inference or fuzzy brand matching. In particular,
# Reliance Capital and Reliance Industries, and the two Muthoot families, stay separate.
REPORT_GROUP_ALIASES = {
    'axis group': 'axis', 'bajaj group': 'bajaj', 'hdfc group': 'hdfc',
    'tata group': 'tata', 'embassy group': 'embassy', 'l and t ltd': 'l and t',
    'blackstone group': 'blackstone', 'bharti airtel ltd': 'bharti',
    'muthoot finance ltd': 'muthoot', 'reliance industries': 'reliance',
}


def report_group_identity(value: object) -> str:
    key = normalize_text(value)
    return REPORT_GROUP_ALIASES.get(key, key)


def report_group_displays(values: Iterable[str]) -> dict[str, str]:
    displays: dict[str, str] = {}
    for value in values:
        displays.setdefault(report_group_identity(value), value)
    return displays


def normalize_fund_name(value: object) -> str:
    text = normalize_text(value)
    text = re.sub(r"\bfunddirect\b", "fund direct", text)
    for pattern, replacement in FUND_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = FUND_NOISE.sub(" ", text)
    text = re.sub(r"\bg\b", " ", text)
    text = " ".join(text.split())
    aliases = {
        # Exact, source-verified identities; dates, series and FoF wrappers elsewhere
        # remain significant. Do not generalize these into date/number stripping.
        "iifl dynamic bond fund": "360 one dynamic bond fund",
        "axis corp 06 10 21 fund": "axis corp bond fund",
        "axis corp fund 06 10 21": "axis corp bond fund",
        "edelweiss nifty psu bond sdl index fund 2026": "edelweiss nifty psu bond sdl apr 2026 50 50 index fund",
        "axis cpse sdl 2025 70 30 index fund": "axis crisil ibx 70 30 cpse sdl april 2025 index fund",
        "nippon india nivesh lakshya fund gr new": "nippon india nivesh lakshya fund",
        "nippon india nivesh lakshya fund gr": "nippon india nivesh lakshya fund",
        "gdg union gilt fund": "union gilt fund",
        "sbi magnum constant maturity fund": "sbi constant maturity 10 year gilt fund",
        "axis crisil sdl 2027 index fund": "axis crisil ibx sdl may 2027 index fund",
        "hdfc nifty g sec sep 2032 v1 index fund": "hdfc nifty g sec sep 2032 index fund",
        "sbi magnum gilt fund": "sbi gilt fund",
        "kotak bond short term": "kotak bond short term fund",
        "icici pru all seasons bond fund": "icici pru all seasons bond fund",
        "kotak fmp series 300": "kotak fmp 300",
        "kotak fmp 300 1223d": "kotak fmp 300",
        "kotak fmp series 300 1223 days": "kotak fmp 300",
        "kotak fmp 300 1223 days": "kotak fmp 300",
    }
    return aliases.get(text, text)


def compatible_fund_names(left: str, right: str) -> bool:
    """Protect product identity before fuzzy matching spelling/plan variants."""
    a, b = set(left.split()), set(right.split())
    for alternatives in ({"low", "short", "medium", "long", "ultra"}, {"fof", "etf"}):
        if a & alternatives != b & alternatives:
            return False
    if ("gilt" in a and "corp" in b) or ("corp" in a and "gilt" in b):
        return False
    if {t for t in a if t.isdigit()} != {t for t in b if t.isdigit()}:
        return False
    months = {"jan": "january", "feb": "february", "mar": "march", "apr": "april",
              "jun": "june", "jul": "july", "aug": "august", "sep": "september",
              "sept": "september", "oct": "october", "nov": "november", "dec": "december"}
    month_names = set(months) | set(months.values()) | {"may"}
    if {months.get(t, t) for t in a & month_names} != {months.get(t, t) for t in b & month_names}:
        return False
    return ("fmp" in a) == ("fmp" in b)


def group_identity(value: object) -> str:
    """Return a stable comparison key without changing the displayed group label."""
    key = normalize_text(value)
    return GROUP_ID_ALIASES.get(key, key)


def canonical_group_name(value: object, historical_groups: Iterable[str] = ()) -> str:
    original = str(value or "").strip()
    key = normalize_text(original)
    historical = tuple(str(item).strip() for item in historical_groups if str(item).strip())
    exact = {normalize_text(item): item for item in historical}
    exact_identity = {group_identity(item): item for item in historical}
    if not key or key in {"blank", "none", "na"}:
        return exact.get("others", "Others")
    # Named groups from the mapping workbook win over the pattern shortcuts below.
    # Those shortcuts match on a substring, so without this ordering a workbook that
    # deliberately lists two separate issuers sharing a brand name (for example a
    # promoter-group split) would still be collapsed into one group.
    if group_identity(original) in exact_identity:
        return exact_identity[group_identity(original)]
    if key in exact:
        return exact[key]
    for normalized, display in exact.items():
        if SequenceMatcher(None, key, normalized).ratio() >= 0.94:
            return display
    if pattern_group_name(key):
        return exact.get(normalize_text(pattern_group_name(key)), pattern_group_name(key))
    return original or exact.get("others", "Others")


def pattern_group_name(key: str) -> str:
    """Group an issuer by name pattern when the mapping workbook does not name it.

    These are last-resort shortcuts for issuers absent from both the mapping workbook
    and the curated seed list. They match on a substring, so they can only be trusted
    for names the workbook has not already classified.
    """
    # BHARAT Bond ETFs are intentionally not pattern-mapped to PSU. The client's
    # reference (Debt Exposure queries, 23-Jul-2026) requires them in "Others";
    # engine._special_group applies that rule so classification stays consistent.
    #
    # "Govt POI <stake%> <entity>" / "GOI owned-<stake%> <entity>" is a recurring master-
    # file labeling convention for government-owned entities -- verbose but semantically
    # identical to the plain "PSU" tag seed_mappings.py and other master files use for the
    # very same issuers (e.g. "Govt POI 74.51% NHPC Ltd" and "NHPC Ltd." -> PSU both mean
    # "PSU"). Left uncanonicalized, dozens of these would each read as their own one-off
    # group -- fragmenting what should be a single PSU concentration total -- and, worse,
    # would each look like a genuine disagreement with seed_mappings' plain "PSU" answer to
    # engine._classify's master-vs-seed conflict check, flooding the review queue with false
    # positives for a difference that is purely cosmetic, not a real classification dispute.
    if key == "govt sdl uday bonds":
        return "SDL"
    if (
        "nabard" in key
        or "national bank for agriculture and rural development" in key
        or re.search(r"\bntpc\b", key)
        or "power grid corporation of india" in key
        or "power grid corpn of india" in key
        or re.match(r"^goi owned \d", key)
        # A bare "Govt <entity>" / "Govt POI <stake%> <entity>" label names a specific
        # government-owned entity (National Housing Bank, Indian Railway Finance Corp,
        # Export-Import Bank, ...), so it means "PSU" too -- but "Govt of India" itself is
        # the sovereign, not a PSU, so that specific phrase is carved out explicitly.
        or (key.startswith("govt ") and not key.startswith("govt of india"))
    ):
        return "PSU"
    if key in {"reliance", "reliance industries", "reliance group", "reeliance"} or key.startswith("reliance industries "):
        return "Reliance"
    return ""


def paper_identity(paper: object, isin: object = "") -> str:
    # Dashboard paper counts follow the historical workbook's distinct paper-name
    # convention. ISIN remains an audit field, but one ISIN may legitimately appear
    # under multiple instrument descriptions or tranches.
    return normalize_text(paper) or normalize_text(isin)


def safe_excel_text(value: object) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if text.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text
