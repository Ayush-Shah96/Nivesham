from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


LEGAL_SUFFIXES = {
    "limited",
    "ltd",
    "ltd.",
    "private",
    "pvt",
    "pvt.",
    "inc",
    "inc.",
    "llp",
    "corporation",
    "corp",
    "corp.",
    "company",
    "co",
    "co.",
}

TRACK_PRIORITY = {
    "PO": 1,
    "HT": 2,
    "LT": 3,
    "NT": 4,
}

_WORD_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class CoverageMatch:
    company_id: int
    company_name: str
    track_mode: str
    priority: int
    analyst: str | None
    score: int
    matched_term: str
    nse_symbol: str | None = None
    bse_code: str | None = None


def normalize_text(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.casefold().replace("&", " and ")
    return _WORD_RE.sub(" ", text).strip()


def significant_tokens(company_name: str) -> list[str]:
    tokens = normalize_text(company_name).split()
    return [token for token in tokens if token not in LEGAL_SUFFIXES]


def build_company_terms(
    company_name: str,
    aliases: Iterable[object] | None = None,
    nse_symbol: object | None = None,
    bse_code: object | None = None,
    *,
    unique_keyword_tokens: Iterable[object] | None = None,
    acronym_terms: Iterable[object] | None = None,
) -> tuple[tuple[str, int], ...]:
    """Build conservative headline-only company terms.

    Strong terms are the full company name, aliases, exchange identifiers and
    explicit/acronym forms. Individual keywords are only accepted when the
    caller has proved they are unique across the active Coverage List.
    """
    candidates: dict[str, int] = {}

    def add(value: object, score: int, *, min_chars: int = 3) -> None:
        normalized = normalize_text(value)
        if not normalized:
            return
        if len(normalized) < min_chars and not normalized.isdigit():
            return
        candidates[normalized] = max(score, candidates.get(normalized, 0))

    tokens = significant_tokens(company_name)
    if tokens:
        add(" ".join(tokens), 100)

    # Preserve explicit all-caps tokens from the actual company name. The
    # normalized token list is lowercase, so checking it for uppercase would
    # never work. This supports headline forms such as HDFC, IIFL, ICICI and
    # SBI without turning ordinary lowercase words into standalone matches.
    for raw_piece in re.split(r"[^A-Za-z0-9]+", str(company_name or "")):
        raw_piece = raw_piece.strip()
        if len(raw_piece) >= 3 and raw_piece.isalpha() and raw_piece.upper() == raw_piece:
            add(raw_piece, 88, min_chars=3)

    if len(tokens) >= 2:
        acronym = "".join(token[0] for token in tokens)
        if len(acronym) >= 3:
            add(acronym, 84, min_chars=3)

    for acronym in acronym_terms or []:
        add(acronym, 86, min_chars=3)

    add(company_name, 96)
    for alias in aliases or []:
        add(alias, 92)

    # Unique natural-language keywords are useful for headlines such as
    # "Borosil rises..." but generic words like "bank", "power" and "finance"
    # remain excluded because they are shared by many Coverage List names.
    for keyword in unique_keyword_tokens or []:
        normalized = normalize_text(keyword)
        if len(normalized) >= 4:
            add(normalized, 72, min_chars=4)

    if nse_symbol:
        add(nse_symbol, 100, min_chars=3)
    if bse_code:
        add(bse_code, 100, min_chars=3)

    return tuple(sorted(candidates.items(), key=lambda x: (-x[1], -len(x[0]))))


def _term_matches_text(raw_text: str, term: str, *, allow_short_lowercase: bool = False) -> bool:
    raw = str(raw_text or "")
    normalized = normalize_text(raw)
    if not normalized:
        return False
    # Very short identifiers (e.g. LT, BEL, ITC) are high-risk false positives.
    # Require the stored representation in uppercase unless explicitly allowed.
    if len(term) <= 3 and not allow_short_lowercase:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(term.upper())}(?![A-Za-z0-9])"
        return re.search(pattern, raw) is not None
    pattern = rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])"
    return re.search(pattern, normalized) is not None


def _term_matches_title(title: str, term: str) -> bool:
    return _term_matches_text(title, term)


def match_headline(
    title: str,
    coverage_rows: Iterable[object],
    context: str = "",
    *,
    include_nt: bool = False,
) -> list[CoverageMatch]:
    """Match a news headline against active Coverage List rows only.

    Expected row attributes: id, legal_name/company_name, aliases, nse_symbol,
    bse_code, track_mode and optional analyst/username.
    """
    matches: list[CoverageMatch] = []

    coverage_list = list(coverage_rows)

    # Build a global token-frequency map once. Only tokens unique to one active
    # coverage company can be used as individual headline keywords.
    token_to_companies: dict[str, set[int]] = {}
    for row in coverage_list:
        row_company_id = int(getattr(row, "company_id", None) or getattr(row, "id", 0) or 0)
        row_name = str(getattr(row, "legal_name", None) or getattr(row, "company_name", None) or "")
        for token in set(significant_tokens(row_name)):
            if len(token) < 5:
                continue
            token_to_companies.setdefault(token, set()).add(row_company_id)

    for row in coverage_list:
        mode = str(getattr(row, "track_mode", "") or "").upper()
        if mode == "NT" and not include_nt:
            continue

        company_name = getattr(row, "legal_name", None) or getattr(row, "company_name", None)
        if not company_name:
            continue

        row_company_id = int(getattr(row, "company_id", None) or getattr(row, "id", 0) or 0)
        significant = significant_tokens(str(company_name))
        # A single shared-style word is not enough to identify a multi-word company.
        # This fixes false matches such as "Premier Explosives" -> "Premier Energies".
        # For one-word company names, a globally-unique token remains useful.
        unique_keywords = []
        if len(significant) == 1:
            unique_keywords = [
                token
                for token in significant
                if len(token) >= 4 and token_to_companies.get(token, set()) == {row_company_id}
            ]

        acronym_terms = []
        for token in significant_tokens(str(company_name)):
            raw = str(token).strip()
            if len(raw) >= 4 and raw.upper() == raw and raw.isalpha():
                acronym_terms.append(raw)

        # Exchange identifiers are handled separately and case-sensitively. A ticker
        # such as CLEAN must not match the ordinary English word "clean" in an unrelated
        # headline such as "Inox Green to enter in Clean Energy Space".
        safe_aliases = []
        for alias in getattr(row, "aliases", None) or []:
            raw_alias = str(alias or "").strip()
            normalized_alias = normalize_text(raw_alias)
            # A one-word lowercase alias is too ambiguous for an investment news
            # alert (e.g. "Clean" for Clean Science). Multi-word aliases and
            # explicit uppercase identifiers are retained.
            if len(normalized_alias.split()) >= 2 or (raw_alias.isalpha() and raw_alias.upper() == raw_alias and len(raw_alias) >= 3):
                safe_aliases.append(raw_alias)

        terms = build_company_terms(
            company_name,
            safe_aliases,
            None,
            None,
            unique_keyword_tokens=unique_keywords,
            acronym_terms=acronym_terms,
        )

        for term, score in terms:
            # NewsAlert identity is headline-only. Body/summary context is deliberately
            # ignored so a company mentioned somewhere in an article cannot become a
            # company alert when the headline itself does not identify it.
            matched_in_title = _term_matches_title(title, term)

            if matched_in_title:
                analyst_obj = getattr(row, "analyst", None)
                analyst = getattr(analyst_obj, "username", None) if analyst_obj else getattr(row, "analyst_name", None)
                matches.append(
                    CoverageMatch(
                        company_id=int(getattr(row, "company_id", None) or getattr(row, "id", 0)),
                        company_name=str(company_name),
                        track_mode=mode,
                        priority=TRACK_PRIORITY.get(mode, 99),
                        analyst=analyst,
                        score=score,
                        matched_term=term,
                        nse_symbol=str(getattr(row, "nse_symbol", None) or "").strip() or None,
                        bse_code=str(getattr(row, "bse_code", None) or "").strip() or None,
                    )
                )
                break

        for identifier, identifier_score in (
            (getattr(row, "nse_symbol", None), 100),
            (getattr(row, "bse_code", None), 99),
        ):
            identifier = str(identifier or "").strip()
            if not identifier or len(identifier) < 2:
                continue
            pattern = rf"(?<![A-Za-z0-9]){re.escape(identifier.upper())}(?![A-Za-z0-9])"
            if re.search(pattern, title):
                analyst_obj = getattr(row, "analyst", None)
                analyst = getattr(analyst_obj, "username", None) if analyst_obj else getattr(row, "analyst_name", None)
                matches.append(
                    CoverageMatch(
                        company_id=row_company_id,
                        company_name=str(company_name),
                        track_mode=mode,
                        priority=TRACK_PRIORITY.get(mode, 99),
                        analyst=analyst,
                        score=identifier_score,
                        matched_term=identifier.upper(),
                        nse_symbol=str(getattr(row, "nse_symbol", None) or "").strip() or None,
                        bse_code=str(getattr(row, "bse_code", None) or "").strip() or None,
                    )
                )
                break

    deduped: dict[int, CoverageMatch] = {}
    for match in matches:
        current = deduped.get(match.company_id)
        if current is None or (match.score, -match.priority) > (current.score, -current.priority):
            deduped[match.company_id] = match

    return sorted(
        deduped.values(),
        key=lambda item: (item.priority, -item.score, item.company_name.casefold()),
    )


def match_video_metadata(
    title: str,
    summary: str,
    coverage_rows: Iterable[object],
    *,
    include_nt: bool = False,
) -> list[CoverageMatch]:
    """Match YouTube metadata using strong Coverage identifiers in title/description.

    YouTube interviews frequently identify the company in the description or guest
    metadata rather than the video title. This matcher is deliberately stricter than
    social/news matching: it uses full company names, aliases, NSE/BSE identifiers,
    and acronym forms only, never generic unique-word heuristics.
    """
    title_text = normalize_text(title)
    summary_text = normalize_text(summary)
    combined = " ".join(part for part in (title_text, summary_text) if part).strip()
    if not combined:
        return []

    matches: list[CoverageMatch] = []
    for row in list(coverage_rows):
        mode = str(getattr(row, "track_mode", "") or "").upper()
        if mode == "NT" and not include_nt:
            continue
        company_name = getattr(row, "legal_name", None) or getattr(row, "company_name", None)
        if not company_name:
            continue
        company_id = int(getattr(row, "company_id", None) or getattr(row, "id", 0) or 0)
        terms = build_company_terms(
            str(company_name),
            getattr(row, "aliases", None),
            None,
            None,
        )
        candidates = []
        for term, score in terms:
            if score < 84:
                continue
            normalized_term = normalize_text(term)
            if not normalized_term:
                continue
            if normalized_term in title_text:
                candidates.append((score + 12, term))
            elif normalized_term in combined:
                candidates.append((score, term))
        for identifier, identifier_score in ((getattr(row, "nse_symbol", None), 100), (getattr(row, "bse_code", None), 99)):
            identifier = str(identifier or "").strip()
            if identifier and re.search(rf"(?<![A-Za-z0-9]){re.escape(identifier.upper())}(?![A-Za-z0-9])", f"{title} {summary}"):
                candidates.append((identifier_score, identifier.upper()))
        if not candidates:
            continue
        score, matched_term = max(candidates, key=lambda item: (item[0], len(normalize_text(item[1]))))
        analyst_obj = getattr(row, "analyst", None)
        analyst = getattr(analyst_obj, "username", None) if analyst_obj else getattr(row, "analyst_name", None)
        matches.append(
            CoverageMatch(
                company_id=company_id,
                company_name=str(company_name),
                track_mode=mode,
                priority=TRACK_PRIORITY.get(mode, 99),
                analyst=analyst,
                score=score,
                matched_term=str(matched_term),
                nse_symbol=str(getattr(row, "nse_symbol", None) or "").strip() or None,
                bse_code=str(getattr(row, "bse_code", None) or "").strip() or None,
            )
        )

    deduped: dict[int, CoverageMatch] = {}
    for match in matches:
        current = deduped.get(match.company_id)
        if current is None or (match.score, -match.priority) > (current.score, -current.priority):
            deduped[match.company_id] = match

    return sorted(
        deduped.values(),
        key=lambda item: (item.priority, -item.score, item.company_name.casefold()),
    )


def best_headline_match(title: str, coverage_rows: Iterable[object]) -> CoverageMatch | None:
    matches = match_headline(title, coverage_rows)
    return matches[0] if matches else None
