from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from typing import Iterable, Mapping

from .engine import match_funds
from .errors import WorkbookSchemaError
from .models import BrokerPosition, Holding
from .normalization import canonical_broker_name, normalize_fund_name, normalize_text


def _source_file(position: BrokerPosition) -> str:
    return position.source.file if position.source else (position.broker or "Broker source")


def _source_date(rows: tuple[BrokerPosition, ...]) -> date | None:
    dates = [row.allocation_date for row in rows if row.allocation_date]
    return max(dates) if dates else None


def _candidate_quality(rows: tuple[BrokerPosition, ...]) -> dict[str, object]:
    count = len(rows)
    if not count:
        return {"score": -1.0, "allocationTotalPct": 0.0, "positionCount": 0}
    allocation_total = sum(float(row.allocation or 0) for row in rows)
    def coverage(predicate):
        return sum(1 for row in rows if predicate(row)) / count
    security_share = coverage(lambda row: row.position_level != "issuer")
    isin = coverage(lambda row: bool(row.isin))
    issuer = coverage(lambda row: bool(row.issuer))
    rating = coverage(lambda row: bool(row.rating))
    maturity = coverage(lambda row: bool(row.maturity))
    market_value = coverage(lambda row: row.market_value_crore is not None)
    client_amount = coverage(lambda row: row.client_amount_crore is not None)
    # Security-level feeds are strongly preferred over issuer-only matrices.
    # Within the same granularity, prefer the most complete security identity.
    score = (
        40.0 * security_share + 18.0 * isin + 14.0 * issuer + 8.0 * rating
        + 7.0 * maturity + 7.0 * market_value + 6.0 * client_amount
    )
    score -= min(abs(1.0 - allocation_total) * 20.0, 15.0)
    return {
        "score": round(score, 4),
        "allocationTotalPct": round(allocation_total * 100, 4),
        "positionCount": count,
        "securityLevelShare": round(security_share, 4),
        "isinCoverage": round(isin, 4),
        "issuerCoverage": round(issuer, 4),
        "ratingCoverage": round(rating, 4),
        "maturityCoverage": round(maturity, 4),
        "marketValueCoverage": round(market_value, 4),
        "clientAmountCoverage": round(client_amount, 4),
        "allocationDate": _source_date(rows).isoformat() if _source_date(rows) else None,
        "validAllocation": 0.80 <= allocation_total <= 1.001,
    }


def resolve_provider_positions(
    holdings: Iterable[Holding],
    raw_positions: Iterable[BrokerPosition],
    *,
    approved_aliases: Mapping[str, str] | None = None,
    scheme_crosswalk: Mapping[str, str] | None = None,
) -> tuple[tuple[BrokerPosition, ...], list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    """Choose one authoritative provider feed per MProfit fund before exposure math.

    Broker files are alternative views of a mutual-fund scheme, not additional
    client investments. One feed is selected per canonical portfolio fund. All
    matched provider feeds remain available for broker-to-broker checks.
    """
    from itertools import combinations

    comparison_tolerance_cr = 2.0
    holding_values = tuple(holdings)
    positions = tuple(raw_positions)
    aliases = {normalize_fund_name(k): v for k, v in (approved_aliases or {}).items()}
    crosswalk = {normalize_fund_name(k): v for k, v in (scheme_crosswalk or {}).items()}

    # Keep each uploaded workbook isolated so same-scheme overlap is visible and
    # cannot accidentally be combined by match_funds().
    by_file: dict[str, list[BrokerPosition]] = defaultdict(list)
    for position in positions:
        by_file[_source_file(position)].append(position)

    candidates_by_fund: dict[str, list[dict[str, object]]] = defaultdict(list)
    file_matches: dict[str, list[dict[str, object]]] = defaultdict(list)
    file_matched_schemes: dict[str, set[str]] = defaultdict(set)
    for filename, file_positions in sorted(by_file.items(), key=lambda item: item[0].casefold()):
        normalized_positions = []
        for position in file_positions:
            original_scheme = position.source_scheme or position.scheme
            target = crosswalk.get(normalize_fund_name(original_scheme))
            target = aliases.get(normalize_fund_name(original_scheme), target or position.scheme)
            normalized_positions.append(replace(
                position,
                scheme=target,
                source_scheme=original_scheme,
                broker=position.broker or _provider_label(filename),
            ))
        matches, _unmatched = match_funds(holding_values, normalized_positions)
        for holding, matched_scheme, rows in matches:
            quality = _candidate_quality(rows)
            broker = rows[0].broker if rows else _provider_label(filename)
            candidate = {
                "holding": holding,
                "matched_scheme": matched_scheme,
                "rows": rows,
                "file": filename,
                "broker": canonical_broker_name(broker),
                "quality": quality,
            }
            candidates_by_fund[normalize_fund_name(holding.fund_name)].append(candidate)
            file_matches[filename].append({
                "portfolioFund": holding.fund_name,
                "brokerScheme": matched_scheme,
                "positionCount": len(rows),
                "positionLevel": "issuer" if all(row.position_level == "issuer" for row in rows) else "security",
            })
            file_matched_schemes[filename].update(
                normalize_fund_name(row.source_scheme or row.scheme) for row in rows
            )

    selected_positions: list[BrokerPosition] = []
    selections: list[dict[str, object]] = []
    portfolio_reconciliation: list[dict[str, object]] = []
    selected_by_fund: dict[str, dict[str, object]] = {}

    for holding in holding_values:
        key = normalize_fund_name(holding.fund_name)
        candidates = candidates_by_fund.get(key, [])
        # Invalid allocations cannot become an authoritative source. If every
        # provider is invalid, leave the fund unmatched for explicit review.
        valid_candidates = [c for c in candidates if c["quality"].get("validAllocation")]
        ranked = sorted(valid_candidates, key=lambda c: (
            float(c["quality"].get("score", -1)),
            c["quality"].get("allocationDate") or "",
            str(c["broker"]).casefold(),
        ), reverse=True)
        selected = ranked[0] if ranked else None
        if selected:
            selected_by_fund[key] = selected
            for row in selected["rows"]:
                selected_positions.append(replace(
                    row,
                    scheme=holding.fund_name,
                    source_scheme=row.source_scheme or row.scheme,
                    fund_maturity=row.fund_maturity,
                ))

        selected_file = selected["file"] if selected else None
        selections.append({
            "portfolioFund": holding.fund_name,
            "portfolioValueCr": holding.value_crore,
            "selectedBroker": selected["broker"] if selected else None,
            "selectedFile": selected_file,
            "selectedSourceScheme": selected["matched_scheme"] if selected else None,
            "selectedPositionCount": selected["quality"].get("positionCount", 0) if selected else 0,
            "selectedAllocationPct": selected["quality"].get("allocationTotalPct", 0) if selected else 0,
            "selectedAllocationDate": selected["quality"].get("allocationDate") if selected else None,
            "selectedGranularity": "issuer" if selected and selected["quality"].get("securityLevelShare", 0) == 0 else "security",
            "selectedSourceQuality": selected["quality"] if selected else None,
            "alternativeProviderCount": max(0, len(candidates) - (1 if selected else 0)),
            "status": "Selected" if selected else ("Provider allocation invalid" if candidates else "No broker match"),
        })

        if not candidates:
            portfolio_reconciliation.append({
                "portfolioFund": holding.fund_name, "portfolioSource": "MProfit",
                "portfolioAmountCr": holding.value_crore,
                "brokerFund": "No matching broker scheme", "broker": "—",
                "brokerAmountCr": 0.0, "gapCr": holding.value_crore,
                "allocationPct": 0.0, "selectedForBase": False,
                "status": "No broker match", "amountBasis": "No broker source",
                "sourceFile": "",
            })
            continue

        for candidate in candidates:
            rows = candidate["rows"]
            quality = candidate["quality"]
            allocation_total = sum(float(row.allocation or 0) for row in rows)
            explicit_amounts = [row.client_amount_crore for row in rows if row.client_amount_crore is not None]
            if explicit_amounts and len(explicit_amounts) == len(rows):
                broker_amount = sum(explicit_amounts)
                amount_basis = "Provider Amount column (client/fund-scaled)"
            else:
                broker_amount = holding.value_crore * allocation_total
                amount_basis = "MProfit value × provider allocation"
            gap = holding.value_crore - broker_amount
            is_selected = bool(selected and candidate["file"] == selected_file)
            portfolio_reconciliation.append({
                "portfolioFund": holding.fund_name, "portfolioSource": "MProfit",
                "portfolioAmountCr": holding.value_crore,
                "brokerFund": rows[0].source_scheme or candidate["matched_scheme"],
                "broker": candidate["broker"], "brokerAmountCr": broker_amount,
                "gapCr": gap, "allocationPct": allocation_total * 100,
                "selectedForBase": is_selected,
                "status": "Selected for BASE" if is_selected else "Alternative provider — not added",
                "amountBasis": amount_basis, "sourceFile": candidate["file"],
                "quality": quality,
            })

    # Compare only like-for-like amount bases within each broker pair. Prefer
    # explicit client-scaled Amount values when both providers publish them;
    # otherwise use parsed security market values when both providers publish
    # them; otherwise scale both allocation percentages by the same MProfit fund
    # value. MProfit is only a scale reference in that final fallback, never a
    # broker participant in the pairwise comparison.
    #
    # Security identity is strictly fund-scoped: ISIN first; when ISIN is absent,
    # the normalized paper + issuer pair is required. Issuer-only rows (such as the
    # ICICI wide matrix) cannot be represented as individual securities and are
    # excluded from this security-level comparison rather than being false-matched.
    reconciliation_sources: list[dict[str, object]] = []
    fallback_isins: dict[tuple[str, str], set[str]] = defaultdict(set)
    issuer_level_rows_skipped = 0
    missing_identity_rows_skipped = 0

    for holding in holding_values:
        fund_key = normalize_fund_name(holding.fund_name)
        for candidate in candidates_by_fund.get(fund_key, []):
            for position in candidate["rows"]:
                if position.position_level == "issuer":
                    issuer_level_rows_skipped += 1
                    continue
                paper_key = normalize_text(position.paper)
                issuer_key = normalize_text(position.issuer)
                isin = str(position.isin or "").strip().upper()
                fallback = f"paper:{paper_key}|issuer:{issuer_key}"
                if not isin and (not paper_key or not issuer_key):
                    missing_identity_rows_skipped += 1
                    continue
                if isin and (paper_key or issuer_key):
                    fallback_isins[(fund_key, fallback)].add(isin)
                provider_name = canonical_broker_name(candidate["broker"])
                if provider_name.casefold() in {"mprofit", "historical base", "broker source"}:
                    continue
                amount = float(holding.value_crore) * float(position.allocation or 0)
                reconciliation_sources.append({
                    "fundKey": fund_key, "fund": holding.fund_name,
                    "paper": str(position.paper or "").strip(),
                    "issuer": str(position.issuer or "").strip(),
                    "fallback": fallback, "isin": isin,
                    "broker": provider_name,
                    "allocationAmount": amount,
                    "marketValueCr": position.market_value_crore,
                    "clientAmountCr": position.client_amount_crore,
                    "file": candidate["file"],
                })

    grouped_securities: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for source in reconciliation_sources:
        fund_key = source["fundKey"]
        if source["isin"]:
            security_key = f"isin:{source['isin']}"
        else:
            known_isins = fallback_isins.get((fund_key, source["fallback"]), set())
            security_key = f"isin:{next(iter(known_isins))}" if len(known_isins) == 1 else source["fallback"]
        grouped_securities[(fund_key, security_key)].append(source)

    broker_reconciliation: list[dict[str, object]] = []
    within_tolerance_count = 0
    comparable_security_count = 0
    for (_fund_key, _security_key), sources in sorted(grouped_securities.items()):
        by_provider: dict[str, list[dict[str, object]]] = defaultdict(list)
        for source in sources:
            by_provider[str(source["broker"])].append(source)
        provider_totals = []
        for broker_name, rows in by_provider.items():
            market_values = [row["marketValueCr"] for row in rows]
            client_amounts = [row["clientAmountCr"] for row in rows]
            provider_totals.append({
                **rows[0],
                "allocationAmount": sum(float(row["allocationAmount"]) for row in rows),
                "marketValueAmount": sum(float(value) for value in market_values if value is not None),
                "marketValueComplete": all(value is not None for value in market_values),
                "clientAmount": sum(float(value) for value in client_amounts if value is not None),
                "clientAmountComplete": all(value is not None for value in client_amounts),
                "file": "; ".join(sorted({str(row["file"]) for row in rows}, key=str.casefold)),
                "paper": next((row["paper"] for row in rows if row["paper"]), ""),
                "issuer": next((row["issuer"] for row in rows if row["issuer"]), ""),
                "isin": next((row["isin"] for row in rows if row["isin"]), ""),
            })
        if len(provider_totals) >= 2:
            comparable_security_count += 1
        for left, right in combinations(sorted(provider_totals, key=lambda row: row["broker"].casefold()), 2):
            # Never compare different amount units. Prefer the broker's explicit
            # client-scaled amount when both providers publish it; otherwise use
            # security market value (already parsed in crore) when both publish
            # it; otherwise normalize both allocation percentages to the same
            # MProfit-held fund value.
            if left["clientAmountComplete"] and right["clientAmountComplete"]:
                amount_a, amount_b = left["clientAmount"], right["clientAmount"]
                amount_basis = "Provider client Amount column (Cr)"
            elif left["marketValueComplete"] and right["marketValueComplete"]:
                amount_a, amount_b = left["marketValueAmount"], right["marketValueAmount"]
                amount_basis = "Provider security market value (Cr)"
            else:
                amount_a, amount_b = left["allocationAmount"], right["allocationAmount"]
                amount_basis = "MProfit fund value × broker-reported holding allocation"
            gap = float(amount_b) - float(amount_a)
            difference = abs(gap)
            if difference <= comparison_tolerance_cr + 1e-9:
                within_tolerance_count += 1
                continue
            denominator = max(abs(float(amount_a)), abs(float(amount_b)), 1e-12)
            broker_reconciliation.append({
                "fund": left["fund"], "paper": left["paper"] or right["paper"],
                "issuer": left["issuer"] or right["issuer"],
                "isin": left["isin"] or right["isin"],
                "brokerA": left["broker"], "amountA": amount_a,
                "brokerB": right["broker"], "amountB": amount_b,
                "differenceCr": difference,
                "higherBroker": right["broker"] if gap > 1e-12 else left["broker"] if gap < -1e-12 else "Equal",
                "differencePct": difference / denominator * 100,
                "status": "Mismatch",
                "amountBasis": amount_basis,
                "sourceA": left["file"], "sourceB": right["file"],
            })

    # Material variances first; ties are stable and readable by fund/security.
    broker_reconciliation.sort(key=lambda row: (
        -float(row.get("differenceCr") or 0),
        str(row.get("fund", "")).casefold(),
        str(row.get("isin", "")).casefold(),
        str(row.get("paper", "")).casefold(),
        str(row.get("brokerA", "")).casefold(),
        str(row.get("brokerB", "")).casefold(),
    ))

    provider_file_diagnostics = []
    for filename, file_positions in sorted(by_file.items(), key=lambda item: item[0].casefold()):
        matched = file_matches.get(filename, [])
        brokers = sorted({canonical_broker_name(row.broker or _provider_label(filename)) for row in file_positions}, key=str.casefold)
        security_count = sum(1 for row in file_positions if row.position_level != "issuer")
        issuer_count = len(file_positions) - security_count
        matched_scheme_names = file_matched_schemes.get(filename, set())
        unmatched_schemes = sorted({
            row.scheme for row in file_positions
            if normalize_fund_name(row.source_scheme or row.scheme) not in matched_scheme_names
        }, key=str.casefold)
        if matched and security_count == 0:
            note = "Issuer-level feed only; it cannot be compared as individual securities."
        elif not matched:
            note = "No MProfit funds matched this file. Check scheme names or add a fund alias."
        elif unmatched_schemes:
            note = "Some schemes did not match MProfit fund names. Review the suggested fund aliases."
        else:
            note = "Matched; security-level positions are available for reconciliation."
        provider_file_diagnostics.append({
            "file": filename,
            "broker": brokers[0] if len(brokers) == 1 else ", ".join(brokers),
            "parsedPositionCount": len(file_positions),
            "parsedSchemeCount": len({normalize_fund_name(row.source_scheme or row.scheme) for row in file_positions}),
            "matchedFundCount": len({str(row["portfolioFund"]) for row in matched}),
            "matchedFunds": sorted({str(row["portfolioFund"]) for row in matched}, key=str.casefold),
            "unmatchedSchemes": unmatched_schemes,
            "securityPositionCount": security_count,
            "issuerPositionCount": issuer_count,
            "note": note,
        })

    summary = {
        "uploadedProviderFiles": len(by_file),
        "providerPositionCount": len(positions),
        "selectedPositionCount": len(selected_positions),
        "selectedFundCount": len(selected_by_fund),
        "fundsWithoutBrokerFeed": [h.fund_name for h in holding_values if normalize_fund_name(h.fund_name) not in selected_by_fund],
        "providerFileDiagnostics": provider_file_diagnostics,
        "securityRowsConsidered": len(reconciliation_sources),
        "comparableSecurityCount": comparable_security_count,
        "materialMismatchCount": len(broker_reconciliation),
        "withinTolerancePairCount": within_tolerance_count,
        "issuerLevelRowsNotComparable": issuer_level_rows_skipped,
        "rowsWithoutSecurityIdentity": missing_identity_rows_skipped,
        "differenceToleranceCr": comparison_tolerance_cr,
        "comparisonAmountBasis": "Per pair: explicit client Amount when both providers have it; otherwise security market value when both have it; otherwise MProfit fund value × allocation. Each comparison uses one common unit basis.",
        "sourceSelectionRule": "One provider feed per MProfit fund; security-level and more complete feeds rank ahead of issuer-level matrices. Alternate provider exports are shown in Broker Reconciliation and are not added.",
    }
    return tuple(selected_positions), selections, broker_reconciliation, summary


def _provider_label(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    return canonical_broker_name(stem)
