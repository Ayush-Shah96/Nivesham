from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Mapping


def frozen_mapping(values: Mapping | None = None) -> Mapping:
    return MappingProxyType(dict(values or {}))


@dataclass(frozen=True, slots=True)
class UploadMetadata:
    entry_count: int
    compressed_bytes: int
    uncompressed_bytes: int


@dataclass(frozen=True, slots=True)
class SourceReference:
    file: str
    sheet: str
    row: int
    cells: str
    value: float | None
    scale: float = 1.0


@dataclass(frozen=True, slots=True)
class ClassificationHint:
    group: str
    rating: str
    source: SourceReference


@dataclass(frozen=True, slots=True)
class PeriodComparison:
    previous_date: date
    current_date: date
    previous_allocation: float
    source: SourceReference


@dataclass(frozen=True, slots=True)
class Holding:
    fund_name: str
    value_crore: float
    layout: str = "mprofit-style"
    value_scale: float = 1.0
    sources: tuple[SourceReference, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    scheme: str
    paper: str
    allocation: float
    issuer: str
    rating: str
    maturity: date | None
    isin: str
    market_value_crore: float | None
    layout: str
    category: str = ""
    source: SourceReference | None = field(default=None, compare=False)
    rating_cell: str = field(default="", compare=False)
    comparison: PeriodComparison | None = field(default=None, compare=False)
    asset_class: str = ""
    asset_type: str = ""
    # Raw source fields retained through normalization and reporting.
    source_scheme: str = ""
    broker: str = ""
    position_level: str = "security"  # security | issuer
    client_amount_crore: float | None = None
    fund_maturity: date | None = None
    allocation_date: date | None = None


@dataclass(frozen=True, slots=True)
class OpeningPosition:
    position: BrokerPosition
    exposure: float
    group: str = ""


@dataclass(frozen=True, slots=True)
class MasterData:
    paper_groups: Mapping[str, str]
    subgroup_groups: Mapping[str, str]
    issuer_groups: Mapping[str, str]
    previous_groups: Mapping[str, float]
    group_ratings: Mapping[str, str]
    historical_groups: tuple[str, ...]
    stats: Mapping[str, int]
    fund_profiles: Mapping[str, tuple[BrokerPosition, ...]] = field(default_factory=frozen_mapping)
    base_sheet: str = "BASE"
    baseline_sheet: str = ""
    requested_base_sheet: str | None = None
    opening_positions: tuple[OpeningPosition, ...] = ()
    snapshot_date: date | None = None
    issuer_classifications: Mapping[str, ClassificationHint] = field(default_factory=frozen_mapping)
    paper_classifications: Mapping[str, ClassificationHint] = field(default_factory=frozen_mapping)
    # Historical BASE GROUP EXPOSURE is the issuer label attached to a paper.
    # Keep it separate from paper_groups, which is the consolidated parent group.
    paper_issuers: Mapping[str, str] = field(default_factory=frozen_mapping)
    isin_issuers: Mapping[str, str] = field(default_factory=frozen_mapping)
    issuer_labels: Mapping[str, str] = field(default_factory=frozen_mapping)


@dataclass(frozen=True, slots=True)
class ExposureRow:
    fund: str
    holding_value: float
    paper: str
    issuer: str
    isin: str
    maturity: date | None
    allocation: float
    market_value_crore: float | None
    exposure: float
    group: str
    rating: str
    category: str
    mapping_source: str
    source: SourceReference | None = field(default=None, compare=False)
    holding_sources: tuple[SourceReference, ...] = field(default=(), compare=False)
    reported_rating: str = field(default="", compare=False)
    rating_cell: str = field(default="", compare=False)
    allocation_total: float = field(default=1.0, compare=False)
    classification_source: SourceReference | None = field(default=None, compare=False)
    classification_evidence: str = field(default="", compare=False)
    asset_class: str = ""
    asset_type: str = ""
    movement_eligible: bool = False
    broker: str = ""
    source_scheme: str = ""
    position_level: str = "security"
    client_amount_crore: float | None = None
    fund_maturity: date | None = None


@dataclass(frozen=True, slots=True)
class GroupExposure:
    group: str
    exposure: float
    percentage: float
    rating: str
    category: str
    paper_count: int
    fund_count: int


@dataclass(frozen=True, slots=True)
class MappingGap:
    paper: str
    issuer: str
    maturity: str
    rating: str
    suggested_group: str
    reason: str
    is_conflict: bool = False


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    severity: str
    issue_type: str
    fund: str
    paper: str
    exposure: float
    source: str
    reason: str
    action: str
    issuer: str = ""
    correction_key: str = ""


@dataclass(frozen=True, slots=True)
class ExposureResult:
    rows: tuple[ExposureRow, ...]
    groups: tuple[GroupExposure, ...]
    mapping_gaps: tuple[MappingGap, ...]
    unmatched_funds: tuple[str, ...]
    added_groups: tuple[str, ...]
    exited_groups: tuple[str, ...]
    total_exposure: float
    kpis: Mapping[str, int | float]
    fund_allocations: Mapping[str, float]
    categories: tuple[Mapping[str, object], ...]
    breaches: tuple[Mapping[str, object], ...]
    metrics: Mapping[str, object]
    review_issues: tuple[ReviewIssue, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=frozen_mapping)
    limit_categories: tuple[Mapping[str, object], ...] = ()
    group_changes: tuple[Mapping[str, object], ...] = ()
    opening_details: tuple[Mapping[str, object], ...] = ()
    broker_comparison: Mapping[str, object] = field(default_factory=frozen_mapping)
