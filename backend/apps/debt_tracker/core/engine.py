from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date
from difflib import SequenceMatcher
from math import isfinite
from functools import lru_cache
import re
from typing import Iterable, Mapping

from .errors import WorkbookSchemaError
from .models import (
    BrokerPosition,
    ExposureResult,
    ExposureRow,
    GroupExposure,
    Holding,
    MappingGap,
    MasterData,
    ReviewIssue,
    frozen_mapping,
)
from .movements import MOVEMENT_SCOPE, broker_comparison, group_movement_eligible, named_debt_group, status as membership_status, trend_status
from .seed_mappings import VERIFIED_SECURITIES, VERIFIED_STRUCTURES
from .normalization import (
    canonical_group_name,
    compatible_fund_names,
    group_identity,
    normalize_fund_name,
    normalize_text,
    paper_identity,
    pattern_group_name,
    report_group_displays,
    report_group_identity,
)
from .parsers import MAX_DISTINCT_HISTORICAL_GROUPS
from .options import validated_corrections, correction_source_urls
from .policies import validated_policy, evaluate_limits, effective_limits, POLICY_NAMES


@lru_cache(maxsize=64)
def _historical_group_indexes(historical: tuple[str, ...]):
    exact = {normalize_text(item): item for item in historical}
    exact_identity = {group_identity(item): item for item in historical}
    return exact, exact_identity


@lru_cache(maxsize=16384)
def _canonical_group_cached(value: str, historical: tuple[str, ...]) -> str:
    """Fast canonicalization for the paper hot path.

    Historical labels are matched by normalized name and explicit group aliases.
    Fuzzy similarity is deliberately not run for every paper: uncertain fuzzy matches
    belong in the review queue rather than silently changing a borrower's parent group.
    """
    original = str(value or '').strip()
    key = normalize_text(original)
    exact, exact_identity = _historical_group_indexes(historical)
    if not key or key in {'blank', 'none', 'na'}:
        return exact.get('others', 'Others')
    identity = group_identity(original)
    if identity in exact_identity:
        return exact_identity[identity]
    if key in exact:
        return exact[key]
    pattern = pattern_group_name(key)
    if pattern:
        return exact.get(normalize_text(pattern), pattern)
    return original or exact.get('others', 'Others')


CATEGORY_LIMITS = frozen_mapping(
    {
        "A1+ Bank": 0.05,
        "A1+ Non-Bank": 0.05,
        "A1 Bank": 0.05,
        "A1 Non-Bank": 0.05,
        "A1- Bank": 0.05,
        "A1- Non-Bank": 0.05,
        "AA- Bank": 0.05,
        "AA Bank": 0.05,
        "AA+ Bank": 0.05,
        "AA- Non-Bank": 0.05,
        "AA Non-Bank": 0.05,
        "AA+ Non-Bank": 0.05,
        "AA+(SO) Bank": 0.05,
        "AA+(SO) Non-Bank": 0.05,
        "AA+(CE) Bank": 0.05,
        "AA+(CE) Non-Bank": 0.05,
        "AA(SO) Bank": 0.05,
        "AA(SO) Non-Bank": 0.05,
        "AA(CE) Bank": 0.05,
        "AA(CE) Non-Bank": 0.05,
        "AA-(SO) Bank": 0.05,
        "AA-(SO) Non-Bank": 0.05,
        "AA-(CE) Bank": 0.05,
        "AA-(CE) Non-Bank": 0.05,
        "AAA Bank": 0.90,
        "AAA Non-Bank": 0.90,
        "AAA(SO) Bank": 0.90,
        "AAA(SO) Non-Bank": 0.90,
        "AAA(CE) Bank": 0.90,
        "AAA(CE) Non-Bank": 0.90,
        "Cash & Equivalent": 1.0,
        "Govt": 1.0,
        "Govt Owned": 1.0,
        "Not Disclosed for Papers": 0.05,
        "Others": 1.0,
        "SDL": 1.0,
        "Unrated": 0.05,
        **{f"{grade}(SO) {kind}": 0.05 for grade in ("A1+", "A1", "A1-") for kind in ("Bank", "Non-Bank")},
    }
)

SPECIAL_CATEGORY_NAMES = ("Cash & Equivalent", "Not Disclosed for Papers", "Others")

# BASE Category is a user-maintained helper column in historical mapping workbooks.
# Older workbooks also used that column for instrument labels (for example SOV,
# NCD, MLD, ZCB) rather than the exposure/rating category used by this engine.
# Only values in CATEGORY_LIMITS are authoritative exposure categories. Anything
# else is retained as source metadata by the parser, but must not override the
# category derived from the current security/issuer/rating evidence.
_CATEGORY_LOOKUP = {" ".join(str(label).split()).casefold(): label for label in CATEGORY_LIMITS}


def _saved_exposure_category(value: object) -> str | None:
    # Preserve significant rating punctuation (+, -, SO, CE); normalize only
    # whitespace and letter case so AA cannot collide with AA+.
    key = " ".join(str(value or "").split()).casefold()
    return _CATEGORY_LOOKUP.get(key) if key else None

# A saved historical profile is only a usable stand-in for a fund while the bonds it
# lists still exist. Once most of the money in that profile sits in paper that has
# already matured, the profile describes a portfolio the fund no longer holds, and
# reporting it as current invents exposure (typically parking the whole fund under the
# matured bond's group). Above this share of matured allocation the profile is treated
# as stale: the fund is reported as having no current broker data and raised for review
# instead of being estimated. Half is the natural cut — below it the profile still
# describes most of the fund, above it the profile is mostly a portfolio that is gone.
STALE_PROFILE_MATURED_ALLOCATION = 0.5

# Fund similarity uses SequenceMatcher and issuer classification uses substring
# and fuzzy historical-name checks. Workbook row limits alone do not bound the
# Cartesian work those operations can create, so keep explicit engine budgets.
# The live production workbook (201 historical groups, 2,409 positions once
# fallback profile matching is counted) needs ~6.76 million classification work
# units; 10 million keeps roughly the same proportional headroom the original
# 5 million gave the smaller reference workbook it was calibrated against.
MAX_FUND_MATCH_COMPARISONS = 25_000
MAX_ISSUER_CLASSIFICATION_WORK_UNITS = 10_000_000
# MAX_DISTINCT_HISTORICAL_GROUPS lives in parsers.py (imported above) so the
# parse-time cap and this classification-time cap can't drift apart the way
# they did previously, when each module kept its own copy of the same limit.
_CANONICAL_GROUP_SCAN_FACTOR = 4


def _direct_name(value: str) -> bool:
    key = normalize_text(value)
    return " direct " in f" {key} " or " dir " in f" {key} "


def _scheme_sets(positions: Iterable[BrokerPosition]) -> Mapping[str, tuple[BrokerPosition, ...]]:
    grouped: dict[str, list[BrokerPosition]] = defaultdict(list)
    for position in positions:
        grouped[position.scheme].append(position)
    return {name: tuple(rows) for name, rows in grouped.items()}


def _fund_bucket(value: str) -> str:
    return next(iter(normalize_fund_name(value).split()), "")


def _scheme_buckets(
    schemes: Mapping[str, tuple[BrokerPosition, ...]],
) -> Mapping[str, Mapping[str, tuple[BrokerPosition, ...]]]:
    buckets: dict[str, dict[str, tuple[BrokerPosition, ...]]] = defaultdict(dict)
    for scheme, rows in schemes.items():
        buckets[_fund_bucket(scheme)][scheme] = rows
    return {key: dict(values) for key, values in buckets.items()}


@dataclass(frozen=True, slots=True)
class _SchemePreference:
    has_layout_a: int
    allocation_closeness: float
    is_direct: bool


def _scheme_preferences(
    schemes: Mapping[str, tuple[BrokerPosition, ...]],
) -> Mapping[str, _SchemePreference]:
    preferences: dict[str, _SchemePreference] = {}
    for scheme, rows in schemes.items():
        allocation_total = sum(row.allocation for row in rows)
        preferences[scheme] = _SchemePreference(
            has_layout_a=int(any(row.layout == "layout-a" for row in rows)),
            allocation_closeness=-abs(allocation_total - 1),
            is_direct=_direct_name(scheme),
        )
    return frozen_mapping(preferences)


def _candidate_similarity(holding: str, scheme: str) -> float:
    left = normalize_fund_name(holding)
    right = normalize_fund_name(scheme)
    if left == right:
        return 1.0
    if not compatible_fund_names(left, right):
        return 0.0
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens or next(iter(left.split()), "") != next(iter(right.split()), ""):
        return 0.0
    jaccard = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left, right).ratio()
    return 0.6 * jaccard + 0.4 * sequence


def _candidate_preference(
    holding_is_direct: bool,
    scheme_preference: _SchemePreference,
) -> tuple[int, float, int]:
    return (
        scheme_preference.has_layout_a,
        scheme_preference.allocation_closeness,
        int(scheme_preference.is_direct == holding_is_direct),
    )


def _fund_candidate(
    holding: Holding,
    schemes: Mapping[str, tuple[BrokerPosition, ...]],
    scheme_preferences: Mapping[str, _SchemePreference],
) -> tuple[float, tuple[int, float, int], str, tuple[BrokerPosition, ...]] | None:
    ranked = sorted(
        ((_candidate_similarity(holding.fund_name, scheme), scheme, rows) for scheme, rows in schemes.items()),
        key=lambda item: (-item[0], item[1].casefold(), item[1]),
    )
    if not ranked or ranked[0][0] < 0.72:
        return None
    best_score = ranked[0][0]
    eligible = [item for item in ranked if abs(item[0] - best_score) < 1e-12]
    holding_is_direct = _direct_name(holding.fund_name)
    if len(eligible) == 1:
        if len(ranked) > 1 and best_score < 1 and best_score - ranked[1][0] < 0.08:
            return None
        score, scheme, rows = eligible[0]
        return score, _candidate_preference(holding_is_direct, scheme_preferences[scheme]), scheme, rows

    preferred = [
        (score, _candidate_preference(holding_is_direct, scheme_preferences[scheme]), scheme, rows)
        for score, scheme, rows in eligible
    ]
    best_preference = max(item[1] for item in preferred)
    finalists = [item for item in preferred if item[1] == best_preference]
    return finalists[0] if len(finalists) == 1 else None


def match_funds(
    holdings: Iterable[Holding], positions: Iterable[BrokerPosition]
) -> tuple[tuple[tuple[Holding, str, tuple[BrokerPosition, ...]], ...], tuple[str, ...]]:
    schemes = _scheme_sets(positions)
    scheme_buckets = _scheme_buckets(schemes)
    holding_values = tuple(holdings)
    candidate_schemes: list[Mapping[str, tuple[BrokerPosition, ...]]] = []
    comparison_count = 0
    for holding in holding_values:
        candidates = scheme_buckets.get(_fund_bucket(holding.fund_name), {})
        comparison_count += len(candidates)
        if comparison_count > MAX_FUND_MATCH_COMPARISONS:
            raise WorkbookSchemaError(
                "Workbook exceeds the fund-matching safety budget. "
                "Reduce the number of similarly named holdings or broker schemes."
            )
        candidate_schemes.append(candidates)

    scheme_preferences = _scheme_preferences(schemes)
    claims: dict[str, list[tuple[int, float, tuple[int, float, int], str, tuple[BrokerPosition, ...]]]] = defaultdict(list)
    unmatched_indexes: set[int] = set()
    for index, (holding, candidates) in enumerate(zip(holding_values, candidate_schemes, strict=True)):
        candidate = _fund_candidate(holding, candidates, scheme_preferences)
        if candidate is None:
            unmatched_indexes.add(index)
            continue
        score, preference, scheme, rows = candidate
        claims[scheme].append((index, score, preference, scheme, rows))

    resolved: dict[int, tuple[Holding, str, tuple[BrokerPosition, ...]]] = {}
    for scheme_claims in claims.values():
        best_strength = max((claim[1], claim[2]) for claim in scheme_claims)
        winners = [claim for claim in scheme_claims if (claim[1], claim[2]) == best_strength]
        if len(winners) == 1:
            index, _score, _preference, scheme, rows = winners[0]
            resolved[index] = (holding_values[index], scheme, rows)
        unmatched_indexes.update(claim[0] for claim in scheme_claims if claim not in winners or len(winners) > 1)

    matches = tuple(resolved[index] for index in sorted(resolved))
    unmatched = tuple(holding.fund_name for index, holding in enumerate(holding_values) if index in unmatched_indexes)
    return matches, unmatched


def _load_seed_mappings() -> Mapping[str, tuple[str, str]]:
    try:
        from .seed_mappings import ISSUER_MAPPINGS

        return ISSUER_MAPPINGS
    except ImportError:
        return {}


def _ensure_classification_budget(
    master: MasterData,
    positions: tuple[BrokerPosition, ...],
    baseline: Mapping[str, float],
) -> None:
    if len(master.historical_groups) > MAX_DISTINCT_HISTORICAL_GROUPS:
        raise WorkbookSchemaError(
            "Workbook exceeds the issuer-classification safety budget: "
            f"the historical-group safety limit is {MAX_DISTINCT_HISTORICAL_GROUPS}."
        )

    seed_mappings = _load_seed_mappings()
    # The substring comprehensions still visit short aliases before filtering
    # them, so every mapping entry must count toward the CPU budget.
    seed_alias_count = len(seed_mappings)
    master_alias_count = len(master.issuer_groups)
    # _classify() now checks the master file's own exact paper/issuer match BEFORE
    # seed_mappings (the uploaded workbook takes priority), so the master-alias scan
    # (cost `master_alias_count`) and the seed-alias scan (cost `seed_alias_count`) are
    # gated by two DIFFERENT exact-match populations, not the same one: a position exact-
    # matched by seed_mappings but not by the master file still pays the master-alias scan
    # first (master is checked unconditionally before seed), and a position exact-matched
    # by the master file but not by seed_mappings never reaches the seed-alias scan at all.
    # Charging both scans against a single "not in seed_mappings" count -- as if only that
    # one population ever paid for either scan -- undercounts real work for master-exact/
    # seed-miss positions, which is exactly backwards for a safety budget.
    non_exact_issuers = sum(
        1 for position in positions if normalize_text(position.issuer) not in seed_mappings
    )
    non_master_exact = sum(
        1
        for position in positions
        if normalize_text(position.paper) not in master.paper_groups
        and normalize_text(position.issuer) not in master.issuer_groups
    )

    # canonical_group_name builds normalized historical maps and may perform a
    # full fuzzy scan. Classification invokes it at most three times per position;
    # movement comparison invokes it once per prior group.
    canonical_calls = 3 * len(positions) + len(baseline)
    canonical_work = (
        canonical_calls
        * len(master.historical_groups)
        * _CANONICAL_GROUP_SCAN_FACTOR
    )
    alias_work = non_master_exact * master_alias_count + non_exact_issuers * seed_alias_count
    if canonical_work + alias_work > MAX_ISSUER_CLASSIFICATION_WORK_UNITS:
        raise WorkbookSchemaError(
            "Workbook exceeds the issuer-classification safety budget. "
            "Reduce the number of unmapped issuers or historical mapping aliases."
        )


_STATE_NAMES = {
            "tamil nadu": "TN", "madhya pradesh": "MP", "uttarpradesh": "Uttar Pradesh",
            "rajashthan": "Rajasthan", "uttarkhand": "Uttarakhand", "chhatisgar": "Chhattisgarh",
            **{name.casefold(): name for name in ("Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar",
                "Chhattisgarh", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir",
                "Jammu Kashmir", "Jharkhand", "Karnataka", "Kerala", "Maharashtra", "Manipur", "Meghalaya",
                "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Telangana",
                "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal")},
            "tamilnadu": "TN", "madhypradesh": "MP", "chhatisgarh": "Chhattisgarh",
            "west bangal": "West Bengal", "gujrat": "Gujarat", "pondicherry": "Puducherry", "puducherry": "Puducherry",
            "orissa": "Odisha",
        }


def _special_group(position: BrokerPosition, historical: tuple[str, ...], group_hint: str = "") -> str | None:
    key = normalize_text(f"{position.issuer} {position.paper}")
    fund_units = re.search(r"\b(?:fund|index|etf|fof|units)\b", key)
    hint = normalize_text(group_hint)
    state_evidence = re.search(r"\b(?:sdl|uday)\b", key) or hint in {"sdl", "govt sdl uday bonds"}
    if state_evidence and fund_units:
        return _canonical_group_cached("Others", tuple(historical))
    if state_evidence and normalize_text(position.rating) in {"", "sov", "sovereign", "sdl"}:
        for state_name, display in _STATE_NAMES.items():
            if re.search(rf"\b{re.escape(state_name)}\b", key):
                return _canonical_group_cached(f"{display} Govt", tuple(historical))
    # BHARAT Bond ETFs are handled earlier in _classify (they need an explicit "Others"
    # rating so they never inherit the Others group's aggregate "Cash" rating).
    sovereign_issuers = {"goi", "govt of india", "government of india"}
    sovereign_paper = re.match(r"^(?:\d+(?:\s+\d+)?\s+)?(?:strips\s+)?(?:goi|govt of india|government of india)\b",
                               normalize_text(position.paper))
    if (normalize_text(position.issuer) in sovereign_issuers or sovereign_paper
            or "treasury bill" in key):
        return _canonical_group_cached("GOI", tuple(historical))
    canonical = _canonical_group_cached(position.issuer, tuple(historical))
    if canonical in {"PSU", "Reliance"}:
        return canonical
    cash_terms = ("cash", "treps", "triparty repo", "cblo", "net receivable", "reverse repo")
    if any(term in key for term in cash_terms):
        return _canonical_group_cached("Others", tuple(historical))
    return None


def _matured_allocation(positions: tuple[BrokerPosition, ...], reference: date) -> float:
    """Share of a saved profile whose paper had already matured on ``reference``.

    Papers with no maturity date (cash, TREPS, perpetuals) never count as matured:
    an unknown date is not evidence that the holding is gone.
    """
    return sum(
        position.allocation
        for position in positions
        if position.maturity is not None and position.maturity < reference
    )


def _partition_fund_profiles(
    master: MasterData, reference: date
) -> tuple[tuple[BrokerPosition, ...], tuple[BrokerPosition, ...]]:
    """Split saved fund profiles into usable fallbacks and stale ones.

    A profile is only a candidate at all if it is complete (its allocations total 100%),
    which is the existing rule. Of those, any profile that is mostly matured paper is
    held back as stale so it can never be matched as a fallback; the fund it belongs to
    then stays unmatched and is raised for review instead of being silently estimated
    from a portfolio that no longer exists.
    """
    usable: list[BrokerPosition] = []
    stale: list[BrokerPosition] = []
    for positions in master.fund_profiles.values():
        if not positions:
            continue
        allocation_total = sum(position.allocation for position in positions)
        if not isfinite(allocation_total) or abs(allocation_total - 1) > 0.001:
            continue
        destination = (
            stale
            if _matured_allocation(positions, reference) > STALE_PROFILE_MATURED_ALLOCATION
            else usable
        )
        destination.extend(positions)
    return tuple(usable), tuple(stale)


def _is_security_correction_key(key):
    return bool(re.fullmatch(r"isin [a-z]{2}[a-z0-9]{9}[0-9] [0-9]{4} [0-9]{2} [0-9]{2}", key))


def _position_override(position, overrides_map, reference_date=None):
    values = overrides_map or {}
    def unscoped(value):
        key = normalize_text(value)
        return ("", "") if _is_security_correction_key(key) else values.get(key, ("", ""))
    issuer = unscoped(position.issuer)
    paper = unscoped(position.paper)
    scoped_key = (normalize_text(f"isin {position.isin} {reference_date.isoformat()}")
                  if position.isin and reference_date else "")
    security = values.get(scoped_key, ("", ""))
    # AI-reviewed corrections are exact-security and report-date scoped. They
    # cannot spread from a generic issuer name to another securitisation pool.
    return tuple(security[i] or paper[i] or issuer[i] for i in range(2))


def _issuer_alias_key(value):
    key = normalize_text(value)
    key = re.sub(r"\bcorpn\b", "corporation", key)
    key = re.sub(r"\blimited\b", "ltd", key)
    # Historical issuer labels sometimes retain the legal suffix after a merger.
    # This removes only that explicit descriptor, not ordinary company-name words.
    key = re.sub(r"\s+(?:amalgamated|merged entity)$", "", key)
    return key


def _generic_issuer(value):
    return normalize_text(value) in {
        "other securitised debt", "securitised debt", "other securitized debt", "securitized debt",
    }


def _master_classification(position, master, group):
    """Retain a specific issuer's type/structure without inheriting a parent's rating."""
    hint = (master.paper_classifications.get(normalize_text(position.paper))
            or (master.issuer_classifications.get(normalize_text(position.issuer))
                if not _generic_issuer(position.issuer) else None)
            or master.issuer_classifications.get(normalize_text(position.paper)))
    if hint is None:
        key = "" if _generic_issuer(position.issuer) else _issuer_alias_key(position.issuer)
        matches = [value for alias, value in master.issuer_classifications.items()
                   if key and _issuer_alias_key(alias) == key]
        if len(matches) == 1:
            hint = matches[0]
    return hint if hint and report_group_identity(hint.group) == report_group_identity(group) else None


def _opening_category(position, master):
    """Opening membership retains its own source classification when regrouped."""
    paper, issuer = normalize_text(position.paper), normalize_text(position.issuer)
    hint = master.paper_classifications.get(paper) or master.issuer_classifications.get(issuer)
    original_group = master.paper_groups.get(paper) or master.issuer_groups.get(issuer) or position.issuer
    return (position.category or (hint.rating if hint else '') or position.rating
            or master.group_ratings.get(original_group, '') or 'Unrated')


# PSU banks are consolidated under the historical June reporting parent group.
# Explicit user/security overrides still take precedence in _classify above.
_PSU_BANK_ISSUERS = {
    "bank of baroda", "canara bank", "punjab national bank",
    "union bank of india", "bank of india", "indian bank",
    "central bank of india", "uco bank", "bank of maharashtra",
    "indian overseas bank", "punjab and sind bank",
}


def _classify(
    position: BrokerPosition, master: MasterData, overrides_map: Mapping[str, tuple[str, str]] | None = None,
    reference_date: date | None = None,
) -> tuple[str, str, str, tuple[str, str] | None]:
    paper_key = normalize_text(position.paper)
    issuer_key = normalize_text(position.issuer)
    override = _position_override(position, overrides_map, reference_date)
    if override and override[0]:
        return override[0], "local-override", "", None
    security = VERIFIED_SECURITIES.get(position.isin.strip().upper())
    if security:
        return security[0], "curated-security", security[1], None
    # This is an asset description shared by unrelated pools, not a borrower.
    # Exact paper assignments and a complete descriptive alias remain usable.
    if _generic_issuer(issuer_key):
        issuer_key = ""
    bank_alias = re.sub(r"\s+ltd$", "", _issuer_alias_key(issuer_key))
    if bank_alias in _PSU_BANK_ISSUERS:
        return "PSU", "june-report-psu-bank-rule", "Govt Owned", None
    if issuer_key in {"capital infra trust", "gawar construction ltd", "gawar construction limited"}:
        return "Others", "client-rule", "Others", None
    # BHARAT Bond ETFs go to the residual "Others" group AND the "Others" category per the
    # client's reference (Debt Exposure queries, 23-Jul-2026): never PSU/Govt Owned, and
    # never Cash. The explicit "Others" rating stops them inheriting the Others group's
    # aggregate rating (which is "Cash" in some mapping files) and being mislabelled cash.
    if "bharat bond" in normalize_text(f"{position.issuer} {position.paper}"):
        return _canonical_group_cached("Others", master.historical_groups), "canonical-rule", "Others", None
    special = _special_group(position, master.historical_groups, master.paper_groups.get(paper_key, ""))
    if special:
        return special, "canonical-rule", "", None
    seed_mappings = _load_seed_mappings()
    seed = seed_mappings.get(issuer_key) or seed_mappings.get(_issuer_alias_key(position.issuer))
    # A meaningful client master assignment wins over a fallback seed. Generic or
    # self-name placeholders contain no parent classification; verified seeds fill them.
    def _is_real_grouping(candidate: str) -> bool:
        candidate_key = normalize_text(candidate)
        return bool(candidate_key) and not _generic_issuer(candidate_key) and candidate_key not in {"others", "blank", "unmapped"} and (
            not issuer_key or not candidate_key.startswith(issuer_key))

    def _resolve_master_candidate(raw_candidate: str) -> str | None:
        canonical = pattern_group_name(normalize_text(raw_candidate)) or raw_candidate
        return canonical if _is_real_grouping(canonical) else None

    if paper_key in master.paper_groups:
        candidate = _resolve_master_candidate(master.paper_groups[paper_key])
        if candidate is not None:
            return candidate, "master-paper", "", None
    if issuer_key in master.issuer_groups:
        candidate = _resolve_master_candidate(master.issuer_groups[issuer_key])
        if candidate is not None:
            return candidate, "master-issuer", "", None
    combined = normalize_text(f"{position.issuer} {position.paper}")
    master_candidates: set[str] = set()
    for alias, group in master.issuer_groups.items():
        if not _generic_issuer(alias) and len(alias) >= 8 and (alias in combined or (issuer_key and issuer_key in alias)):
            resolved = _resolve_master_candidate(group)
            if resolved is not None:
                master_candidates.add(resolved)
    if len(master_candidates) == 1:
        candidate = master_candidates.pop()
        return candidate, "master-alias", "", None
    if seed:
        if seed == ("Others", "Others") and normalize_text(position.rating) == "equity":
            return "Others", "review", "", None
        return seed[0], "curated-issuer", seed[1], None
    seeded_candidates = {
        value
        for alias, value in seed_mappings.items()
        if (len(alias) >= 8 or (len(alias) >= 7 and alias.endswith(" ltd"))) and issuer_key and (
            re.search(rf"\b{re.escape(_issuer_alias_key(alias))}\b", _issuer_alias_key(position.issuer))
            or _issuer_alias_key(position.issuer) == _issuer_alias_key(alias))
    }
    seeded_groups = {value[0] for value in seeded_candidates}
    if len(seeded_groups) == 1:
        display = next(iter(seeded_groups))
        rating = next((value[1] for value in seeded_candidates if value[1]), "")
        if (display, rating) == ("Others", "Others") and normalize_text(position.rating) == "equity":
            return "Others", "review", "", None
        return display, "curated-alias", rating, None
    return _canonical_group_cached("Others", master.historical_groups), "review", "", None


_RATING_GRADES = ("AAA", "AA+", "AA-", "AA", "A1+", "A1-", "A1")

# Creditworthiness rank: 0 = AAA (best), higher index = more conservative.
# Distinct from _RATING_GRADES, which is ordered for safe prefix/substring matching
# (AA- before AA to avoid "AA" matching first). Here AA correctly outranks AA-.
_GRADE_CONSERVATISM: dict[str, int] = {
    g: i for i, g in enumerate(("AAA", "AA+", "AA", "AA-", "A1+", "A1-", "A1"))
}


def rating_grade(rating: object) -> str:
    """Return the most conservative grade found in the rating string, or "" if ungraded.

    When a paper carries ratings from two agencies (e.g. "CRISIL A1+ / ICRA A1") the
    more conservative (lower credit quality) grade is returned. For a single rating the
    result is the same as before.

    Whitespace is normalised to single spaces but never stripped entirely: this keeps
    rating-agency prefixes (e.g. "ICRA", "CRISIL") as separate tokens so the trailing
    'A' of "ICRA" cannot merge with "A1+" into "ICRAA1+" and produce a false "AA" hit.
    """
    # Preserve word boundaries by collapsing runs of whitespace to a single space rather
    # than removing them. "ICRA A1+" stays ["ICRA", "A1+"], not "ICRAA1+".
    text = re.sub(r"\s+", " ", str(rating or "").upper()).replace("–", "-").replace("—", "-").strip()
    if not text:
        return ""
    enhancement = ""
    marker = re.search(r"\(?\b(SO|CE)\b\)?", text)
    if marker:
        enhancement = f"({marker.group(1)})"
        text = " ".join((text[: marker.start()] + text[marker.end() :]).split())
    # Split on both whitespace and "/" to separate dual ratings like "AAA/AAA (SO)".
    # Replace "/" with space, then split.
    text_with_spaces = text.replace("/", " ")
    tokens = text_with_spaces.split()
    matched: set[str] = set()
    # Pass 1: exact token match — handles bare grades ("A1+") and agency-prefixed grades
    # ("ICRA A1+", "CRISIL AA-") without letting the prefix bleed into the grade string.
    for grade in _RATING_GRADES:
        if grade in tokens:
            matched.add(grade)
    # Pass 2: token-prefix match — handles outlook/watch suffixes appended without a space
    # (e.g. "A1+POSITIVE", "AA+WATCHNEG"). Only runs when Pass 1 finds nothing so that
    # an exact "A1" token never competes with a prefix hit from the same string.
    if not matched:
        for grade in _RATING_GRADES:
            for token in tokens:
                if token.startswith(grade):
                    matched.add(grade)
    if not matched:
        return ""
    # Return the most conservative grade (highest _GRADE_CONSERVATISM rank).
    # For a single rating this is a no-op; for dual-agency ratings it picks the lower one.
    worst = max(matched, key=lambda g: _GRADE_CONSERVATISM.get(g, -1))
    return f"{worst}{enhancement}"


def _category(
    group: str,
    rating: str,
    issuer: str,
    paper: str,
    paper_rating: str = "",
    group_rating: str = "",
    maturity: date | None = None,
    asset_class: str = "",
    asset_type: str = "",
    structure: str = "",
) -> str:
    """Classify a position.

    ``rating`` is the issuer-specific curated rating (from seed_mappings), if any.
    Together with ``paper_rating`` (the broker's own fresh rating for this exact paper)
    it decides the special buckets (SDL, Govt, Cash, Not Disclosed) and, subject to
    ``paper_rating`` taking precedence when present, the grade rung too.

    ``group_rating`` is the group-level aggregate rating (the largest holding in the
    group's rating, per parsers.py) and is NOT specific to this paper at all. It must
    never be able to trigger a special bucket for an unrelated paper -- e.g. a paper
    with no rating of its own inheriting a much larger, differently-rated holding's
    "Not Disclosed" text from the same group. It is only used as a last-resort
    fallback to pick a grade rung (AAA/AA/...) when the paper has no rating of its own
    anywhere -- neither a fresh broker rating nor an issuer-specific curated one.
    """
    combined = normalize_text(f"{issuer} {paper}")
    normalized_rating = normalize_text(rating)
    normalized_paper_rating = normalize_text(paper_rating)
    if (normalized_paper_rating in {"equity", "equities"}
            or normalize_text(asset_class) in {"equity", "equities"}
            or normalize_text(asset_type) in {"equity", "equities", "domestic equities"}):
        return "Others"
    if re.search(r"(?<![A-Z0-9])(?:BBB|BB|B|A[234]|A|C|D)[+-]?(?![A-Z0-9])", paper_rating.upper()):
        raise WorkbookSchemaError(
            f"Unsupported broker credit rating {paper_rating!r}. Add the appropriate rating category "
            "and limit before generating this report; a historical grade cannot replace it."
        )
    if normalized_paper_rating == "derivatives":
        return "Others"
    cash_rating = any(
        value.startswith("cash") or "cash and equivalent" in value
        for value in (normalized_rating, normalized_paper_rating)
        if value
    )
    cash_paper = normalize_text(paper) in {
        "cash", "cash and equivalent", "current assets", "net current asset",
        "net current assets", "other assets", "net receivables", "net receivable",
    }
    if cash_paper:
        return "Cash & Equivalent"
    # Unit holdings do not acquire a bond grade from an unrelated security in the
    # same parent group. Explicit current credit ratings on REIT-issued bonds win.
    if (not maturity and not rating_grade(paper_rating)
            and re.search(r"\b(?:reit|invit)(?: units)?$", normalize_text(paper))
            and not re.search(r"\b(?:bond|debenture|ncd|sr)\b", normalize_text(paper))):
        return "Unrated"
    if group == "Others":
        if cash_rating or any(
            term in combined for term in ("cash", "treps", "triparty repo", "cblo", "net receivable")
        ):
            return "Cash & Equivalent"
        if "not disclosed" in normalized_rating or "not disclosed" in normalized_paper_rating:
            return "Not Disclosed for Papers"
        # group == "Others" covers two very different situations: a deliberate classification
        # (rating is non-empty -- a curated issuer/alias entry or a canonical rule like the
        # BHARAT Bond ETF one, e.g. equity/cash satellite holdings) versus _classify()'s
        # "review" fallback, where no group could be identified at all and rating is "".
        # Not knowing the parent conglomerate is not evidence that the paper's own credit
        # rating is unknown too -- a brand-new issuer not yet in any mapping file can still
        # carry a perfectly good fresh broker rating (e.g. "AAA"). Only fall back to "Others"
        # here when the classification was deliberate, or when there is truly no gradable
        # rating anywhere; otherwise fall through below to grade it from paper_rating, same
        # as a known group would be.
        if normalized_rating == "others":
            return "Others"
        if normalized_paper_rating in {"equity", "unrated", "others", "mutual fund units", "reit", "invit"}:
            return "Unrated"
        if rating or not rating_grade(paper_rating):
            return "Others"
    if normalized_paper_rating in {"equity", "unrated", "others", "mutual fund units", "reit", "invit", "reit units", "invit units"}:
        return "Unrated"
    if group == "GOI":
        return "Govt"
    if group in {f"{state} Govt" for state in _STATE_NAMES.values()}:
        return "SDL"
    if group == "PSU" or "govt owned" in normalized_rating:
        return "Govt Owned"
    if group == "SDL" or "sdl" in normalized_rating or normalized_paper_rating in {"sov", "sovereign", "sdl"} and "sdl" in combined:
        return "SDL"
    if "sov" in normalized_rating or normalized_rating == "govt":
        return "Govt"
    # Cash and undisclosed papers keep their own buckets outside the Others group too,
    # so Others stays a genuine residual rather than a catch-all.
    if "not disclosed" in normalized_rating or "not disclosed" in normalized_paper_rating:
        return "Not Disclosed for Papers"
    if cash_rating:
        return "Cash & Equivalent"
    bank = (" bank" in f" {combined}" and "non bank" not in combined) or any(
        " bank" in f" {value}" and "non bank" not in value
        for value in (normalized_rating, normalized_paper_rating)
        if value
    )
    if "non bank" in normalized_rating:
        bank = False
    suffix = "Bank" if bank else "Non-Bank"
    paper_grade = rating_grade(paper_rating)
    curated_grade = rating_grade(rating)
    # CE is a credit-enhancement structure, not a separate risk bucket — strip it so
    # AAA(CE) papers land in the plain AAA bucket alongside other AAA papers.
    paper_grade = re.sub(r"\(CE\)", "", paper_grade).strip()
    curated_grade = re.sub(r"\(CE\)", "", curated_grade).strip()
    # A fresh, paper-specific broker rating always takes precedence over historical/
    # curated data when present — no conservatism comparison, even if the fresh rating
    # is actually better (or worse) than the curated one. The one narrow exception:
    # when the base grades are equal, keep a "(SO)" structured-obligation annotation
    # from the curated issuer mapping if the paper's own rating text lacks it (protects
    # the AAA(SO) fix from commit 622988e).
    if paper_grade:
        if curated_grade:
            paper_base = re.sub(r"\([^)]*\)", "", paper_grade).strip()
            curated_base = re.sub(r"\([^)]*\)", "", curated_grade).strip()
            if paper_base == curated_base and "(SO)" in curated_grade and "(SO)" not in paper_grade:
                grade = curated_grade
            else:
                grade = paper_grade
        else:
            grade = paper_grade
    else:
        # The paper carries no rating of its own anywhere (no fresh broker rating, no
        # issuer-specific curated one). Only now may the group-level aggregate rating
        # step in, and only to pick a grade rung — it can still never set a special
        # bucket (that was decided above, using `rating`/`paper_rating` alone).
        group_grade = re.sub(r"\(CE\)", "", rating_grade(group_rating)).strip()
        grade = curated_grade or group_grade
    if grade:
        if structure == "SO" and "(SO)" not in grade:
            grade = f"{grade}(SO)"
        return f"{grade} {suffix}"
    return "Unrated" if not normalized_rating else "Others"


def _movement_display(value: object) -> str:
    # Group labels come from the uploaded quarter's approved mapping. Do not
    # rewrite them through a code-maintained list: that list becomes stale and
    # can merge distinct borrowers (for example individual banks into PSU).
    return str(value or "").strip()


def _movement_identity(value: object) -> str:
    return group_identity(_movement_display(value))


def _mapping_gap(position: BrokerPosition, conflict: tuple[str, str] | None = None) -> MappingGap:
    if conflict:
        master_group, seed_group = conflict
        reason = (
            f"The current mapping workbook says this bond's issuer belongs to '{master_group}', but our saved "
            f"history has a different, previously-confirmed group for the same issuer: '{seed_group}'. We do not "
            "know which one is right this quarter, so rather than silently picking one, its value is still "
            "counted in the total exposure figure but grouped under 'Others' for now, and neither borrower's "
            "concentration reflects it until you decide."
        )
    else:
        reason = (
            "We could not tell which borrower group this bond belongs to: it is not in the saved history and no "
            "reviewed issuer rule covers it. Its value is still counted in the total exposure figure, but it is "
            "grouped under 'Others' instead of the real borrower, so that borrower's concentration looks smaller "
            "than it is and a limit breach could be missed."
        )
    return MappingGap(
        paper=position.paper,
        issuer=position.issuer,
        maturity=position.maturity.isoformat() if position.maturity else "",
        rating=position.rating,
        suggested_group="Others",
        reason=reason,
        is_conflict=conflict is not None,
    )


def _bharat_proxy_matches(holdings, unmatched, current_positions, historical_positions):
    originals = {}
    for holding in holdings:
        if holding.fund_name not in unmatched:
            continue
        key = normalize_fund_name(holding.fund_name)
        found = re.fullmatch(r"(?:edelweiss )?bharat bond (?:etf )?fof april (20[0-9]{2})", key)
        if found:
            originals[f"bharat bond etf april {found[1]}"] = holding
    virtual = tuple(Holding(key, holding.value_crore) for key, holding in originals.items())
    current, remaining = match_funds(virtual, current_positions)
    historical, _ = match_funds(tuple(h for h in virtual if h.fund_name in remaining), historical_positions)
    details = []
    converted = []
    for source, matches in (("Current broker ETF portfolio", current), ("Historical ETF portfolio", historical)):
        output = []
        for virtual_holding, scheme, positions in matches:
            holding = originals[virtual_holding.fund_name]
            output.append((holding, f"{holding.fund_name} [ETF estimate]", positions))
            details.append({"fund": holding.fund_name, "etf": scheme, "source": source, "exposure": holding.value_crore})
        converted.append(tuple(output))
    matched_names = {item["fund"] for item in details}
    return *converted, tuple(name for name in unmatched if name not in matched_names), details


def generate_exposure(
    master: MasterData,
    holdings: Iterable[Holding],
    positions: Iterable[BrokerPosition],
    previous_groups: Mapping[str, float] | None = None,
    reference_date: date | None = None,
    issuer_overrides: Iterable[Mapping[str, object]] | None = None,
    baseline_date: date | None = None,
    estimate_bharat_fof: bool = False,
    limit_policy: str = "standard",
) -> ExposureResult:
    limit_policy = validated_policy(limit_policy)
    holding_values = tuple(holdings)
    raw_position_values = tuple(positions)
    # Brokers 360 is the source of the current paper issuer. If its issuer cell is
    # blank, recover the issuer label from June BASE by exact paper identity. Never
    # use the paper description itself as a substitute issuer/group label.
    def resolve_issuer(position: BrokerPosition) -> BrokerPosition:
        broker_issuer = (position.issuer or "").strip()
        # Security identity is stronger than the broker's free-text issuer label.
        # Prefer the previous-quarter canonical GROUP EXPOSURE for a verified ISIN,
        # then for an unambiguous exact paper-name match. This prevents aliases and
        # broker spelling variants from creating false additions/exits.
        historical = (
            master.isin_issuers.get(normalize_text(position.isin))
            if position.isin else None
        ) or master.paper_issuers.get(normalize_text(position.paper))
        if historical:
            issuer = historical
        else:
            issuer = broker_issuer
            if issuer:
                issuer = master.issuer_labels.get(normalize_text(issuer), issuer)
                # Match common legal-name spelling variants only when they resolve
                # to one historical label. Ambiguous aliases remain untouched for Review.
                alias = _issuer_alias_key(issuer)
                alias_matches = {
                    label for key, label in master.issuer_labels.items()
                    if _issuer_alias_key(key) == alias
                }
                if len(alias_matches) == 1:
                    issuer = next(iter(alias_matches))
        return replace(position, issuer=issuer) if issuer != position.issuer else position
    position_values = tuple(resolve_issuer(position) for position in raw_position_values)
    # Maturity is judged against the day the report is produced unless a caller pins it.
    reference = reference_date or date.today()
    if master.snapshot_date and master.snapshot_date > reference:
        raise WorkbookSchemaError(
            f"The mapping report is dated {master.snapshot_date.isoformat()}, after the selected report date "
            f"{reference.isoformat()}. Use the opening workbook for this period; a later report cannot supply historical allocations."
        )
    if master.snapshot_date and baseline_date and baseline_date != master.snapshot_date:
        raise WorkbookSchemaError(
            f"The opening date must match the mapping report date {master.snapshot_date.isoformat()}. "
            "Select that date or upload the correct opening workbook."
        )
    opening_date = master.snapshot_date or baseline_date
    overrides_map = validated_corrections(issuer_overrides, CATEGORY_LIMITS)
    override_sources = correction_source_urls(issuer_overrides)
    current_matches, current_unmatched = match_funds(holding_values, position_values)
    unmatched_keys = {normalize_fund_name(name) for name in current_unmatched}
    unmatched_holdings = tuple(
        holding for holding in holding_values if normalize_fund_name(holding.fund_name) in unmatched_keys
    )
    fallback_positions, stale_positions = _partition_fund_profiles(master, reference)
    fallback_matches, unmatched = match_funds(unmatched_holdings, fallback_positions)
    proxy_details = []
    if estimate_bharat_fof:
        proxy_current, proxy_fallback, unmatched, proxy_details = _bharat_proxy_matches(
            holding_values, unmatched, position_values, fallback_positions)
        current_matches = (*current_matches, *proxy_current)
        fallback_matches = (*fallback_matches, *proxy_fallback)
    # Funds still unmatched are re-checked against the profiles held back as stale, purely
    # so the review note can say the saved history is out of date rather than missing.
    still_unmatched_keys = {normalize_fund_name(name) for name in unmatched}
    stale_matches, _stale_unmatched = match_funds(
        tuple(
            holding
            for holding in holding_values
            if normalize_fund_name(holding.fund_name) in still_unmatched_keys
        ),
        stale_positions,
    )
    stale_profile_schemes = {
        normalize_fund_name(holding.fund_name): scheme for holding, scheme, _rows in stale_matches
    }
    fallback_match_keys = {
        (normalize_fund_name(holding.fund_name), scheme)
        for holding, scheme, _positions in fallback_matches
    }
    matches = (*current_matches, *fallback_matches)
    baseline = previous_groups if previous_groups is not None else master.previous_groups
    matched_positions = tuple(
        position
        for _holding, _scheme, scheme_positions in matches
        for position in scheme_positions
    )
    opening = master.opening_positions if previous_groups is None else ()
    if opening:
        paper_issuers: dict[str, dict[str, str]] = defaultdict(dict)
        isin_issuers: dict[str, dict[str, str]] = defaultdict(dict)
        paper_isins: dict[str, set[str]] = defaultdict(set)
        paper_maturities: dict[str, set[date]] = defaultdict(set)
        for position in matched_positions:
            paper_isins[normalize_text(position.paper)].add(position.isin.strip().upper())
            if position.maturity:
                paper_maturities[normalize_text(position.paper)].add(position.maturity)
            if position.issuer:
                paper_issuers[normalize_text(position.paper)][normalize_text(position.issuer)] = position.issuer
                if position.isin:
                    isin_issuers[normalize_text(position.isin)][normalize_text(position.issuer)] = position.issuer
        resolved_opening = []
        for item in opening:
            missing_issuer = not item.position.issuer or normalize_text(item.position.issuer) == normalize_text(item.position.paper)
            candidates = (isin_issuers[normalize_text(item.position.isin)] if item.position.isin
                          else paper_issuers[normalize_text(item.position.paper)])
            position = (replace(item.position, issuer=next(iter(candidates.values())))
                        if missing_issuer and len(candidates) == 1 else item.position)
            isins = paper_isins[normalize_text(position.paper)]
            # An exact, unambiguous current paper identifies the same opening
            # security. Never spread one trust's identity across similar names.
            compatible_issuer = (missing_issuer or _generic_issuer(position.issuer)
                                 or normalize_text(position.issuer) in paper_issuers[normalize_text(position.paper)])
            current_maturities = paper_maturities[normalize_text(position.paper)]
            compatible_maturity = not position.maturity or not current_maturities or current_maturities == {position.maturity}
            if not position.isin and len(isins) == 1 and compatible_issuer and compatible_maturity:
                isin = next(iter(isins))
                if isin in VERIFIED_SECURITIES:
                    position = replace(position, isin=isin)
            resolved_opening.append(replace(item, position=position))
        opening = tuple(resolved_opening)
    # Every ISIN participates: exact-security corrections must not leak through
    # a shared name/rating cache entry for two unrelated securitisation pools.
    def classification_key(position, as_of=reference):
        isin = position.isin.strip().upper()
        dated_key = normalize_text(f"isin {isin} {as_of.isoformat()}") if isin and as_of else ""
        scope = dated_key if dated_key in overrides_map else ""
        return position.paper, position.issuer, position.rating, isin if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) else "", scope
    dated_positions = (*((p, reference) for p in matched_positions),
                       *((item.position, opening_date) for item in opening))
    unique_positions = {classification_key(p, as_of): (p, as_of) for p, as_of in dated_positions}
    active_security_keys = {normalize_text(f"isin {p.isin} {as_of.isoformat()}")
                            for p, as_of in dated_positions if p.isin and as_of}
    _ensure_classification_budget(master, tuple(p for p, _as_of in unique_positions.values()), baseline)
    classifications = {}
    for key, (position, as_of) in unique_positions.items():
        group, source, rating, conflict = _classify(position, master, overrides_map, as_of)
        if not source.startswith("curated-") and source != "local-override":
            group = _canonical_group_cached(group, master.historical_groups)
        classifications[key] = group, source, rating, conflict
    # Curated labels need exact/verified alias consolidation too, but must never
    # pass through fuzzy historical matching (which can merge different parents).
    rating_groups = {key: value[0] for key, value in classifications.items()}
    displays = report_group_displays((*master.historical_groups,
                                     *(item[0] for item in classifications.values())))
    classifications = {
        key: (displays[report_group_identity(group)] if source != "local-override" else group,
              source, rating, conflict)
        for key, (group, source, rating, conflict) in classifications.items()
    }
    # Corrections retain deliberately different parent names, while casing and
    # punctuation alone must not split the same parent into multiple report rows.
    exact_displays = {}
    for group in (*master.historical_groups, *(item[0] for item in classifications.values())):
        exact_displays.setdefault(normalize_text(group), group)
    classifications = {
        key: (exact_displays[normalize_text(group)], source, rating, conflict)
        for key, (group, source, rating, conflict) in classifications.items()
    }
    classification_hints = {
        key: _master_classification(position, master, classifications[key][0])
        for key, (position, _as_of) in unique_positions.items()
    }
    opening_gaps = {}
    opening_gap_exposure: dict[str, float] = defaultdict(float)
    opening_categories = {item.position: _opening_category(item.position, master) for item in opening}
    movement_baseline: dict[str, float] = defaultdict(float)
    if opening:
        # Quarter-on-quarter group movement must use the opening report's own
        # Sheet-8 group totals as the authoritative opening snapshot. Rebuilding
        # opening totals from security rows reclassifies historical positions with
        # today's issuer mapping and can silently move exposure between groups; it
        # also loses historical/manual Sheet-8 adjustments. Keep opening BASE rows
        # only for security-level drill-down and review diagnostics.
        for item in opening:
            group, source, _rating, _conflict = classifications[classification_key(item.position, opening_date)]
            if source in {"review", "group-conflict"} and group_movement_eligible(
                'Unresolved issuer', opening_categories[item.position]
            ):
                key = normalize_text(item.position.paper)
                opening_gaps[key] = item.position
                opening_gap_exposure[key] += item.exposure

    # Sheet8 contains the prior quarter's consolidated parent-group totals.
    # BASE is used separately for security-level additions/exits; its GROUP EXPOSURE
    # field is often the issuer/subgroup label and must first be consolidated through
    # Sheet6 rather than mistaken for a parent group.
    movement_baseline = {
        group: amount for group, amount in baseline.items()
        if group_movement_eligible(_movement_display(group), master.group_ratings.get(group, "Unrated"))
    }
    unmatched_keys = {normalize_fund_name(name) for name in unmatched}
    total_investment = sum(holding.value_crore for holding in holding_values)
    unmatched_exposure = sum(
        holding.value_crore for holding in holding_values if normalize_fund_name(holding.fund_name) in unmatched_keys
    )
    rows: list[ExposureRow] = []
    gaps: dict[str, MappingGap] = {}
    gap_exposure: dict[str, float] = defaultdict(float)
    fund_allocations: dict[str, float] = {}

    fallback_exposure = 0.0
    resolved_categories = {}
    fallback_funds: list[tuple[Holding, str]] = []
    ignored_profile_categories: set[str] = set()
    allocation_residuals: list[tuple[Holding, str, float, float]] = []
    for holding, scheme, scheme_positions in matches:
        is_fallback = (normalize_fund_name(holding.fund_name), scheme) in fallback_match_keys
        if is_fallback:
            fallback_exposure += holding.value_crore
            fallback_funds.append((holding, scheme))
        fund_allocations[scheme] = holding.value_crore
        allocation_total = sum(position.allocation for position in scheme_positions)
        if not isfinite(allocation_total) or allocation_total <= 0 or allocation_total > 1.001 or allocation_total < 0.80:
            raise WorkbookSchemaError(
                f"Broker allocations for {scheme!r} are outside the supported 80%-100.1% range (received {allocation_total * 100:.4f}%)."
            )
        # Do not spread an incomplete broker allocation over the papers that happen
        # to be present. That overstates every known security. Small rounding noise
        # around 100% is normalized; a material shortfall is retained as an explicit
        # Not Disclosed residual row and raised in the Review queue.
        normalization = allocation_total if allocation_total >= 0.999 else 1.0
        residual_allocation = max(0.0, 1.0 - allocation_total) if allocation_total < 0.999 else 0.0
        if residual_allocation > 1e-9:
            allocation_residuals.append((holding, scheme, residual_allocation, allocation_total))
        for position in scheme_positions:
            group, source, seeded_rating, conflict = classifications[classification_key(position)]
            classification = classification_hints[classification_key(position)]
            security_key = normalize_text(f"isin {position.isin} {reference.isoformat()}")
            if classification is None and security_key in overrides_map and not overrides_map[security_key][1]:
                original_group = _classify(position, master, {k: v for k, v in overrides_map.items() if k != security_key}, reference)[0]
                classification = _master_classification(position, master, original_group)
            if source in {"canonical-rule", "client-rule", "curated-security"} and seeded_rating:
                classification = None  # A deliberate security rule outranks a historic issuer hint.
            if classification:
                seeded_rating = classification.rating
            # The issuer-specific curated rating (seeded_rating) still drives the special
            # buckets, but the grade comes from the paper itself: an issuer-level rating
            # would otherwise flatten every paper of that issuer onto one grade (an A1+ CD
            # reported as A1-). The group-level aggregate rating (largest holding in the
            # group wins, per parsers.py) is NOT specific to this paper at all and must
            # never be able to trigger a special bucket for it (e.g. an unrated paper
            # inheriting an unrelated, larger holding's "Not Disclosed" text) — it is
            # passed separately and only used inside _category() as a last-resort grade
            # fallback when the paper has no rating of its own anywhere.
            group_rating = master.group_ratings.get(rating_groups[classification_key(position)], "")
            structure, structure_evidence = VERIFIED_STRUCTURES.get(position.isin.strip().upper(), ("", ""))
            category = _category(group, seeded_rating, position.issuer, position.paper,
                                 position.rating, group_rating, maturity=position.maturity,
                                 asset_class=position.asset_class, asset_type=position.asset_type, structure=structure)
            if is_fallback and position.category:
                saved_category = _saved_exposure_category(position.category)
                if saved_category:
                    # A valid, explicitly supplied portfolio category is preserved.
                    category = saved_category
                else:
                    # Legacy BASE Category values such as SOV/NCD/MLD describe
                    # instrument structure, not rating buckets. Ignore rather than
                    # fail the entire run; current evidence above remains authoritative.
                    ignored_profile_categories.add(str(position.category).strip())
            override = _position_override(position, overrides_map, reference)
            if override and override[1]:
                category = override[1]
            resolved_categories[position] = category
            rating = override[1] if override and override[1] else position.rating or seeded_rating or group_rating
            if category == "Unrated" and not position.rating and rating_grade(rating):
                rating = "Unrated"
            normalized_allocation = position.allocation / normalization
            exposure = holding.value_crore * normalized_allocation
            row = ExposureRow(
                fund=scheme,
                holding_value=holding.value_crore,
                paper=position.paper,
                issuer=position.issuer,
                isin=position.isin,
                maturity=position.maturity,
                allocation=normalized_allocation,
                market_value_crore=position.market_value_crore,
                exposure=exposure,
                group=group,
                rating=rating,
                category=category,
                mapping_source="master-profile-fallback" if is_fallback else source,
                source=position.source,
                holding_sources=holding.sources,
                reported_rating=position.rating,
                rating_cell=position.rating_cell,
                allocation_total=allocation_total,
                classification_source=classification.source if classification else None,
                classification_evidence=(override_sources[security_key] if security_key in overrides_map and security_key in override_sources else
                                         VERIFIED_SECURITIES[position.isin.strip().upper()][2]
                                         if source == "curated-security" else structure_evidence),
                asset_class=position.asset_class,
                asset_type=position.asset_type,
                movement_eligible=group_movement_eligible(_movement_display(group), category),
                broker=position.broker or ("Historical BASE" if is_fallback else ""),
                source_scheme=position.source_scheme or position.scheme,
                position_level=position.position_level,
                client_amount_crore=position.client_amount_crore,
                fund_maturity=position.fund_maturity,
            )
            rows.append(row)
            if source in ("review", "group-conflict") and exposure != 0:
                identity = paper_identity(position.paper, position.isin)
                gaps.setdefault(identity, _mapping_gap(position, conflict))
                # A paper can appear in more than one fund's holdings (e.g. the same bond
                # held by two schemes), and each occurrence is a separate row under the one
                # MappingGap keyed by paper identity -- sum all of them so the review queue
                # shows the bond's real total impact, not just its first occurrence.
                gap_exposure[identity] += exposure

    for holding, scheme, residual_allocation, allocation_total in allocation_residuals:
        rows.append(ExposureRow(
            fund=scheme,
            holding_value=holding.value_crore,
            paper=f"Unallocated broker balance — {scheme}",
            issuer="Not Disclosed",
            isin="",
            maturity=None,
            allocation=residual_allocation,
            market_value_crore=None,
            exposure=holding.value_crore * residual_allocation,
            group="Others",
            rating="Not Disclosed",
            category="Not Disclosed for Papers",
            mapping_source="allocation-residual",
            source=None,
            holding_sources=holding.sources,
            reported_rating="",
            allocation_total=allocation_total,
            classification_source=None,
            classification_evidence="Broker allocation does not sum to 100%; unallocated balance is shown separately.",
            asset_class="Debt",
            asset_type="Unallocated broker balance",
            movement_eligible=False,
        ))

    total = sum(row.exposure for row in rows)
    grouped_rows: dict[str, list[ExposureRow]] = defaultdict(list)
    for row in rows:
        if row.group != "Others":
            grouped_rows[row.group].append(row)
    groups: list[GroupExposure] = []
    for group, members in grouped_rows.items():
        exposure = sum(item.exposure for item in members)
        category_exposure: dict[str, float] = defaultdict(float)
        for item in members:
            category_exposure[item.category] += item.exposure
        primary_category = max(category_exposure, key=category_exposure.get)
        category_labels = tuple(sorted({item.category for item in members if item.category}, key=str.casefold))
        groups.append(
            GroupExposure(
                group=group,
                exposure=exposure,
                percentage=exposure / total if total else 0.0,
                rating=" / ".join(category_labels),
                category=primary_category,
                paper_count=len({paper_identity(item.paper, item.isin) for item in members}),
                fund_count=len({normalize_fund_name(item.fund) for item in members}),
            )
        )
    groups.sort(key=lambda item: (-item.exposure, item.group.casefold()))

    category_totals: dict[str, float] = defaultdict(float)
    for row in rows:
        if row.exposure != 0:
            category_totals[row.category] += row.exposure
    category_rows = tuple(
        frozen_mapping(
            {
                "category": category,
                "exposure": exposure,
                "percentage": exposure / total if total else 0.0,
                "limit": CATEGORY_LIMITS.get(category),
                "breached": bool(CATEGORY_LIMITS.get(category) is not None and total and exposure / total > CATEGORY_LIMITS[category]),
            }
        )
        for category, exposure in sorted(category_totals.items())
    )
    limit_categories = evaluate_limits(category_rows, limit_policy, CATEGORY_LIMITS)
    breaches = tuple(item for item in limit_categories if item["breached"])
    cash_exposure = sum(row.exposure for row in rows if row.category == "Cash & Equivalent")
    cash_and_others_exposure = sum(row.exposure for row in rows if row.category in {"Cash & Equivalent", "Others"})

    previous_active: dict[str, str] = {}
    previous_totals: dict[str, float] = defaultdict(float)
    for group, amount in movement_baseline.items():
        display = group if opening else _canonical_group_cached(group, master.historical_groups)
        identity = _movement_identity(display)
        previous_totals[identity] += amount
        previous_active.setdefault(identity, _movement_display(display))
    previous_active = {key: value for key, value in previous_active.items() if abs(previous_totals[key]) > 1e-12}
    current_active: dict[str, str] = {}
    current_totals: dict[str, float] = defaultdict(float)
    for row in rows:
        if not row.movement_eligible:
            continue
        identity = _movement_identity(row.group)
        current_totals[identity] += row.exposure
        current_active.setdefault(identity, _movement_display(row.group))
    current_active = {key: value for key, value in current_active.items() if abs(current_totals[key]) > 1e-12}
    if movement_baseline:
        added = tuple(sorted((current_active[key] for key in current_active.keys() - previous_active.keys()), key=str.casefold))
        exited = tuple(sorted((previous_active[key] for key in previous_active.keys() - current_active.keys()), key=str.casefold))
    else:
        # No previous-period group data could be read from the mapping workbook at all (its
        # Sheet7/Group Category tab is missing or doesn't have the expected columns -- see
        # parse_master). Every current group would otherwise show as "added" and nothing
        # could ever show as "exited", which looks like real movement but is actually just
        # the absence of anything to compare against. An empty movement list is an honest
        # "we don't know", not a false "everything changed this quarter". A review note
        # below tells the user why, instead of silently showing this misleading result.
        added = ()
        exited = ()
    changes = tuple(frozen_mapping({
        "key": key,
        "group": current_active.get(key, previous_active.get(key)),
        "opening": previous_totals[key], "closing": current_totals[key],
        "change": current_totals[key] - previous_totals[key],
        "membershipStatus": membership_status(previous_totals[key], current_totals[key]),
        "status": trend_status(previous_totals[key], current_totals[key]),
    }) for key in sorted(previous_active.keys() | current_active.keys())) if movement_baseline else ()
    opening_details = tuple(frozen_mapping({
        "fund": item.position.scheme, "paper": item.position.paper, "isin": item.position.isin,
        "issuer": item.position.issuer,
        "maturity": item.position.maturity,
        "mapping_source": classifications[classification_key(item.position, opening_date)][1],
        "category": opening_categories[item.position],
        "group": _movement_display(classifications[classification_key(item.position, opening_date)][0]),
        "source_group": _movement_display(item.group),
        "movement_key": _movement_identity(classifications[classification_key(item.position, opening_date)][0]),
        "movement_eligible": group_movement_eligible(
            _movement_display(classifications[classification_key(item.position, opening_date)][0]), opening_categories[item.position]),
        "asset_class": item.position.asset_class, "asset_type": item.position.asset_type,
        "exposure": item.exposure, "source": asdict(item.position.source) if item.position.source else None,
    }) for item in opening)
    positive_papers = {paper_identity(row.paper, row.isin) for row in rows if row.exposure != 0}
    raw_papers = {paper_identity(row.paper, row.isin) for row in rows}
    review_issues: list[ReviewIssue] = [
        ReviewIssue(
            severity="REVIEW",
            issue_type="Group conflict: mapping file vs. saved history" if gap.is_conflict else "Bond not assigned to a group",
            fund="",
            paper=gap.paper,
            exposure=gap_exposure[identity],
            source=gap.issuer,
            issuer=gap.issuer,
            correction_key=normalize_text(gap.issuer or gap.paper),
            reason=gap.reason,
            action=(
                "Tell us which group this bond belongs to, then ask for the report to be regenerated. Until then it "
                f"stays under {gap.suggested_group}."
            ),
        )
        for identity, gap in gaps.items()
    ]
    review_issues.extend(
        ReviewIssue(severity="REVIEW", issue_type="Opening group needs confirmation", fund="",
            paper=position.paper, exposure=opening_gap_exposure[key], source=master.base_sheet,
            issuer=position.issuer, correction_key=normalize_text(position.issuer or position.paper),
            reason="This opening-period security remains in Others. Added/Exited attribution is uncertain until its group is confirmed.",
            action="Confirm the opening security's group and regenerate with the same input files.")
        for key, position in opening_gaps.items() if abs(opening_gap_exposure[key]) > 1e-12
    )
    # Safe as a plain last-wins lookup only because parse_holdings already aggregates every
    # holding row into one entry per normalized fund name, so no two holdings collide here.
    unmatched_by_key = {normalize_fund_name(holding.fund_name): holding for holding in holding_values}
    review_issues.extend(
        ReviewIssue(
            severity="REVIEW",
            issue_type="Fund not linked to broker data",
            fund=name,
            paper="",
            exposure=unmatched_by_key[normalize_fund_name(name)].value_crore,
            source=(
                f"Saved history out of date: {stale_profile_schemes[normalize_fund_name(name)]}"
                if normalize_fund_name(name) in stale_profile_schemes
                else "Current broker workbook"
            ),
            reason=(
                (
                    "This fund's holding could not be linked to a fund in the broker's portfolio file. There is "
                    "a saved history for it, but most of the bonds in that history have already matured, so it no "
                    "longer describes what the fund holds today and using it would report bonds that are gone. "
                    "Its value is left out of the total exposure figure, which understates your real exposure by "
                    "this amount."
                )
                if normalize_fund_name(name) in stale_profile_schemes
                else (
                    "This fund's holding could not be linked to a fund in the broker's portfolio file, and no "
                    "complete saved history was available for it either, so we cannot see which bonds it holds. "
                    "Its value is left out of the total exposure figure, which understates your real exposure by "
                    "this amount."
                )
            ),
            action=(
                "Send us the broker portfolio statement for this fund, or a mapping file that carries its full "
                "past bond breakdown, then ask for the report to be regenerated."
            ),
        )
        for name in unmatched
    )
    review_issues.extend(
        ReviewIssue(
            severity="REVIEW",
            issue_type="Incomplete broker allocation",
            fund=scheme,
            paper="Unallocated broker balance",
            exposure=holding.value_crore * residual_allocation,
            source="Broker workbook",
            issuer="",
            correction_key=normalize_text(scheme),
            reason=(f"The broker allocation for {scheme} totals {allocation_total * 100:.2f}%, leaving "
                    f"{residual_allocation * 100:.2f}% without a paper-level position. The missing balance "
                    "has been kept in Not Disclosed; known papers have not been scaled up."),
            action="Obtain the complete scheme holdings from the broker and regenerate the quarter.",
        )
        for holding, scheme, residual_allocation, allocation_total in allocation_residuals
    )
    if ignored_profile_categories:
        labels = ", ".join(sorted(ignored_profile_categories, key=str.casefold)[:12])
        review_issues.append(
            ReviewIssue(
                severity="INFO",
                issue_type="Ignored legacy BASE Category labels",
                fund="",
                paper="",
                exposure=0.0,
                source=master.base_sheet,
                reason=(
                    "The saved BASE Category column contains labels that are not recognised exposure/rating "
                    f"categories ({labels}). They were not applied as ratings; categories were derived from "
                    "current security, issuer and rating evidence instead. Update these labels only if they "
                    "are intended to be approved exposure categories."
                ),
                action="Review the BASE Category column if these labels were intended to represent rating categories.",
            )
        )
    review_issues.extend(
        ReviewIssue(
            severity="INFO",
            issue_type="Estimated from saved history",
            fund=holding.fund_name,
            paper="",
            exposure=holding.value_crore,
            source=f"Saved history: {scheme}",
            reason=(
                "The broker's portfolio file has no entry for this fund, so we used the last saved historical "
                "breakdown of the bonds it held. Its full value is counted in the total exposure figure, so the "
                "total is not understated, but the bond-level detail behind it may be out of date."
            ),
            action=(
                "Send us the current broker portfolio for this fund when it is available, then ask for the "
                "report to be regenerated."
            ),
        )
        for holding, scheme in fallback_funds
    )
    review_issues.extend(
        ReviewIssue("INFO", "Estimated using ETF portfolio", item["fund"], "", item["exposure"],
                    item["source"], "FoF exposure uses the matching maturity ETF portfolio. Wrapper cash and expenses are not supplied.",
                    "Supply the FoF portfolio to replace this optional estimate.")
        for item in proxy_details
    )
    if not baseline:
        review_issues.append(
            ReviewIssue(
                severity="INFO",
                issue_type="Added/Exited groups unavailable",
                fund="",
                paper="",
                exposure=0.0,
                source="Mapping workbook",
                reason=(
                    "The mapping workbook's historical group sheet is missing or doesn't have the columns we "
                    "expect, so there is nothing to compare this period's groups against. The Added and Exited "
                    "columns are left empty rather than showing every current group as \"added\", which would "
                    "look like real movement but would only reflect the missing history, not what actually "
                    "changed since last quarter."
                ),
                action=(
                    "Send us a mapping workbook whose historical sheet lists last quarter's groups with their "
                    "exposure, then ask for the report to be regenerated to see real Added/Exited groups."
                ),
            )
        )
    review_action_count = sum(issue.severity == "REVIEW" for issue in review_issues)
    sheet4_group_count = len({row.group if row.group != "Others" else row.category for row in rows})
    # Every distinct category among "Others"-group rows gets its own display-only pseudo-
    # group row (see special_category_display_groups below) -- not just the three canonical
    # residual names, since a paper with no known parent group can still carry a real,
    # fresh credit grade (e.g. "AAA Non-Bank"). Mirrors that function's row count exactly.
    special_category_count = len({row.category for row in rows if row.group == "Others" and row.exposure != 0})
    kpis = frozen_mapping(
        {
            "rawRows": len(rows),
            "baseRows": len(rows),
            "totalExposure": total,
            "totalInvestment": total_investment,
            "debtExposure": total - cash_exposure,
            "cashExposure": cash_exposure,
            "cashAndOthersExposure": cash_and_others_exposure,
            "totalFunds": len(holding_values),
            "totalPapers": len(positive_papers),
            "rawPapers": len(raw_papers),
            "totalGroups": len({normalize_text(row.issuer) for row in rows if row.issuer and normalize_text(row.issuer) not in {"others", "not disclosed"}}),
            "totalParentGroups": len(groups),
            "breachCount": len(breaches),
            "mappingGapCount": len(gaps),
            "unmatchedFundCount": len(unmatched),
            "unmatchedExposure": unmatched_exposure,
            "fallbackFundCount": len(fallback_funds),
            "fallbackExposure": fallback_exposure,
            "staleProfileFundCount": len(stale_profile_schemes),
            "reviewActionCount": review_action_count,
            "reviewIssueCount": len(review_issues),
            "sheet4Rows": len(rows) + sheet4_group_count + 2,
            "sheet8Rows": max(len(groups), len(added), len(exited)) + special_category_count + len(limit_categories) + 4,
        }
    )
    metrics = frozen_mapping(
        {
            "base": frozen_mapping(
                {
                    "generated_rows": len(rows),
                    "generated_prop_mkt_value_total": total,
                    "reference_prop_mkt_value_total": total_investment,
                    "difference": total - total_investment,
                }
            ),
            "sheet4": frozen_mapping({"generated_grand_total": sum(sum(m.exposure for m in members) for members in grouped_rows.values()) + sum(r.exposure for r in rows if r.group == "Others"), "reference_grand_total": total, "difference": sum(g.exposure for g in groups) + sum(r.exposure for r in rows if r.group == "Others") - total}),
            "sheet8": frozen_mapping({"generated_top_grand_total": sum(category_totals.values()), "reference_top_grand_total": total, "difference": sum(category_totals.values()) - total}),
        }
    )
    return ExposureResult(
        rows=tuple(rows),
        groups=tuple(groups),
        mapping_gaps=tuple(gaps.values()),
        unmatched_funds=unmatched,
        added_groups=added,
        exited_groups=exited,
        total_exposure=total,
        kpis=kpis,
        fund_allocations=frozen_mapping(fund_allocations),
        categories=category_rows,
        limit_categories=limit_categories,
        group_changes=changes,
        opening_details=opening_details,
        broker_comparison=broker_comparison(matched_positions, classifications, classification_key, resolved_categories),
        breaches=breaches,
        metrics=metrics,
        review_issues=tuple(review_issues),
        metadata=frozen_mapping({
            "classificationRuleVersion": 3,
            "limitPolicy": limit_policy,
            "limitPolicyName": POLICY_NAMES[limit_policy],
            "limitPolicyVersion": 1,
            "effectiveCategoryLimits": effective_limits(limit_policy, CATEGORY_LIMITS),
            "reportDate": reference.isoformat(),
            "reportDateExplicit": reference_date is not None,
            "baselineDate": opening_date.isoformat() if opening_date else None,
            "mappingReportDate": master.snapshot_date.isoformat() if master.snapshot_date else None,
            "baselineSheet": master.baseline_sheet,
            "baselineComparison": "reclassified-details" if opening else "summary",
            "openingMappingGapCount": sum(abs(amount) > 1e-12 for amount in opening_gap_exposure.values()),
            "openingUnmappedExposure": sum(opening_gap_exposure.values()),
            "baseSheet": master.base_sheet,
            "requestedBaseSheet": master.requested_base_sheet,
            "baselineGroupCount": len(previous_active),
            "baselineExposure": sum(baseline.values()),
            "movementScope": MOVEMENT_SCOPE,
            "movementRuleVersion": 4,
            "movementOpeningExposure": sum(previous_totals.values()),
            "movementClosingExposure": sum(current_totals.values()),
            "holdingsScales": tuple(sorted({holding.value_scale for holding in holding_values})),
            "estimatedEtfPortfolios": tuple(frozen_mapping(item) for item in proxy_details),
            "estimateBharatFof": estimate_bharat_fof,
            "appliedOverrides": tuple(frozen_mapping({"issuer_key": key, "group": value[0], "rating_bucket": value[1]})
                                      for key, value in overrides_map.items()
                                      if not _is_security_correction_key(key)
                                      or key in active_security_keys),
            "validationBasis": "Holdings vs positions; group and category aggregates vs positions. No external expected workbook is used.",
        }),
    )


def special_category_display_groups(result: ExposureResult) -> tuple[Mapping[str, object], ...]:
    """Display-only group rows for every category inside the residual 'Others' group.

    Cash & Equivalent, Not Disclosed for Papers, Others, and any real credit grade a paper
    in the residual "Others" group was classified into (e.g. "AAA Non-Bank" for an issuer
    with a fresh, gradable broker rating but no known parent group) all carry real exposure
    but are deliberately excluded from ``result.groups`` (which lists only named borrower
    groups). Without them the dashboard group view silently omits this exposure, so its
    bars do not add up to 100% and a client line such as "Not Disclosed for Papers" -- or a
    graded-but-ungrouped issuer's rating bucket -- goes missing. Surfacing them here gives
    the dashboard the same completeness as the Excel Sheet8 and the client's own reference
    sheet (Debt Exposure queries, 23-Jul-2026). These lines are presentation only; they
    never change ``result.groups`` or the exporter reconciliation.
    """
    members: dict[str, list[ExposureRow]] = defaultdict(list)
    for row in result.rows:
        if row.group == "Others" and row.exposure != 0:
            members[row.category].append(row)
    total = result.total_exposure
    display: list[Mapping[str, object]] = []
    # The three canonical residual names keep their existing display order for stability;
    # any other real grade category a formerly-"review" paper landed in is appended after
    # them, sorted for a deterministic order.
    ordered_categories = list(SPECIAL_CATEGORY_NAMES) + sorted(
        (category for category in members if category not in SPECIAL_CATEGORY_NAMES), key=str.casefold
    )
    for category in ordered_categories:
        rows = members.get(category)
        if not rows:
            continue
        exposure = sum(row.exposure for row in rows)
        display.append(
            frozen_mapping(
                {
                    "group": category,
                    "exposure": exposure,
                    "percentage": exposure / total if total else 0.0,
                    "rating": category,
                    "paper_count": len({paper_identity(row.paper, row.isin) for row in rows}),
                    "fund_count": len({normalize_fund_name(row.fund) for row in rows}),
                    "special": True,
                }
            )
        )
    return tuple(display)
