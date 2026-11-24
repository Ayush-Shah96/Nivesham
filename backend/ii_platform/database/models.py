from enum import Enum
from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


from datetime import date, datetime, timedelta, timezone
def utcnow(): return datetime.now(timezone.utc)
class Base(DeclarativeBase): pass
class UserRole(str, Enum): ADMIN="ADMIN"; INVESTMENT="INVESTMENT"; ANALYST="ANALYST"
class TrackMode(str, Enum): HT="HT"; LT="LT"; NT="NT"; PO="PO"
class ApprovalStatus(str, Enum):
    PENDING_AASHIL_APPROVAL="PENDING_AASHIL_APPROVAL"
    PENDING_APPROVAL="PENDING_APPROVAL"
    APPROVED="APPROVED"
    REJECTED="REJECTED"
 
class User(Base):
    __tablename__="users"
    id: Mapped[int]=mapped_column(primary_key=True)
    username: Mapped[str]=mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str]=mapped_column(String(255))
    role: Mapped[UserRole]=mapped_column(default=UserRole.ANALYST)
    is_active: Mapped[bool]=mapped_column(Boolean, default=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
 
class Company(Base):
    __tablename__="companies"
    id: Mapped[int]=mapped_column(primary_key=True)
    legal_name: Mapped[str]=mapped_column(String(255), unique=True)
    nse_symbol: Mapped[str|None]=mapped_column(String(40), unique=True)
    bse_code: Mapped[str|None]=mapped_column(String(40), unique=True)
    isin: Mapped[str|None]=mapped_column(String(20), unique=True)
    aliases: Mapped[list]=mapped_column(JSON, default=list)
    market_cap: Mapped[float|None]=mapped_column(Float)
    market_cap_bucket: Mapped[str|None]=mapped_column(String(20))
    market_cap_updated_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    website: Mapped[str|None]=mapped_column(String(500))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
 
class TargetCompany(Base):
    __tablename__="target_companies"
    id: Mapped[int]=mapped_column(primary_key=True)
    company_id: Mapped[int]=mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    analyst_id: Mapped[int|None]=mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    track_mode: Mapped[TrackMode]=mapped_column(default=TrackMode.LT)
    active: Mapped[bool]=mapped_column(Boolean, default=True)
    approval_status: Mapped[ApprovalStatus]=mapped_column(default=ApprovalStatus.PENDING_AASHIL_APPROVAL, index=True)
    approval_by: Mapped[int|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    approval_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    start_date: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    __table_args__=(
        UniqueConstraint("company_id", name="uq_target_company"),
        Index("ix_target_companies_active_approval", "active", "approval_status"),
    )
 
class Article(Base):
    __tablename__="articles"
    id: Mapped[int]=mapped_column(primary_key=True)
    url: Mapped[str]=mapped_column(String(1000), unique=True)
    title: Mapped[str]=mapped_column(String(1000))
    source: Mapped[str]=mapped_column(String(120))
    published_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    raw_content: Mapped[str|None]=mapped_column(Text)
    content_hash: Mapped[str]=mapped_column(String(64), index=True)
    processing_status: Mapped[str]=mapped_column(String(50), default="pending")
    summary: Mapped[str|None]=mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    relevance_score: Mapped[float|None]=mapped_column(Float)
    is_material: Mapped[bool]=mapped_column(Boolean, default=False)
    is_interview: Mapped[bool]=mapped_column(Boolean, default=False)
    content_type: Mapped[str]=mapped_column(String(20), default="NEWS", index=True, server_default="NEWS")
    matched_company_id: Mapped[int|None]=mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    news_alert_eligible: Mapped[bool]=mapped_column(Boolean, default=False, server_default="false", index=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__=(
        Index("ix_articles_source_published", "source", "published_at"),
        Index("ix_articles_published_at_id", "published_at", "id"),
    )

class ArticleCompanyMention(Base):
    """Relational company mentions attached to a NewsAlert article."""
    __tablename__ = "article_company_mentions"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    mention_name: Mapped[str] = mapped_column(String(255))
    match_method: Mapped[str] = mapped_column(String(40), default="legacy")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("article_id", "mention_name", name="uq_article_company_mention"),)

class Filing(Base):
    __tablename__="filings"
    id: Mapped[int]=mapped_column(primary_key=True)
    filing_id: Mapped[str]=mapped_column(String(200), unique=True)
    company_id: Mapped[int|None]=mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    filing_type: Mapped[str]=mapped_column(String(100))
    source: Mapped[str]=mapped_column(String(50))
    published_at: Mapped[datetime]=mapped_column(DateTime(timezone=True))
    document_url: Mapped[str]=mapped_column(String(1000))
    document_hash: Mapped[str|None]=mapped_column(String(64), index=True)
    extracted_text: Mapped[str|None]=mapped_column(Text)
    extracted_tables: Mapped[list]=mapped_column(JSON, default=list)
    transactions: Mapped[list]=mapped_column(JSON, default=list)
    processing_status: Mapped[str]=mapped_column(String(50), default="pending")
 
class RegulatoryFiling(Base):
    """Raw NSE filings retained as auditable evidence for normalized desk rows."""
    __tablename__ = "regulatory_filings"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_filing_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    nse_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    company_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    filing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(24), default="RECEIVED", index=True)
    classification_status: Mapped[str] = mapped_column(String(24), default="UNCLASSIFIED", index=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_regulatory_filings_source_date", "source", "filing_date"),
        Index("ix_regulatory_filings_symbol_date", "nse_symbol", "filing_date"),
    )


class RegulatoryCollectorRun(Base):
    """Durable execution log for each source/date window, including failures."""
    __tablename__ = "regulatory_collector_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    records_received: Mapped[int] = mapped_column(Integer, default=0)
    records_inserted: Mapped[int] = mapped_column(Integer, default=0)
    records_updated: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RegulatoryMarketPrice(Base):
    """Daily NSE market-price history used to enrich regulatory signals."""
    __tablename__ = "regulatory_market_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    nse_symbol: Mapped[str] = mapped_column(String(40), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(80), default="NSE-CM-UDIFF-BHAVCOPY")
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("nse_symbol", "trade_date", "source", name="uq_regulatory_market_price_symbol_date_source"),
        Index("ix_regulatory_market_prices_trade_date_symbol", "trade_date", "nse_symbol"),
    )


class InsiderSignal(Base):
    __tablename__ = "insider_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    stock_name: Mapped[str] = mapped_column(String(255))
    person_name: Mapped[str] = mapped_column(String(255))
    client_category: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str | None] = mapped_column(String(120))
    transaction_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    quantity: Mapped[float | None] = mapped_column(Float)
    post_transaction_holding: Mapped[float | None] = mapped_column(Float)
    traded_percent: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    amount_inr: Mapped[float | None] = mapped_column(Float)
    period: Mapped[str | None] = mapped_column(String(120))
    transaction_mode: Mapped[str | None] = mapped_column(String(120))
    market_cap: Mapped[float] = mapped_column(Float)

    # NSE PIT facts are normalized into relational scalar columns. Raw source
    # payloads are not persisted as JSON blobs.
    holding_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_before_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price_change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    regulation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    submission_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    revision_remarks: Mapped[str | None] = mapped_column(String(255), nullable=True)
    broadcast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    nse_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    nse_pid: Mapped[str | None] = mapped_column(String(80), nullable=True)
    nse_did: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=utcnow)
    regulatory_filing_id: Mapped[int | None] = mapped_column(ForeignKey("regulatory_filings.id", ondelete="SET NULL"), nullable=True, index=True)
 
class CapitalActionSignal(Base):
    """Seven-year regulatory capital-ownership events used by the PE/Warrant desk."""
    __tablename__ = "capital_action_signals"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    stock_name: Mapped[str] = mapped_column(String(255))
    investor_name: Mapped[str | None] = mapped_column(String(255))
    investor_category: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str | None] = mapped_column(String(160))
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    quantity: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    amount_inr: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(String(1000))
    source_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    event_sub_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    pre_holding_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_holding_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    promoter_or_pac: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    severity: Mapped[str | None] = mapped_column(String(40), nullable=True)
    importance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    event_chain_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    investor_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    isin: Mapped[str | None] = mapped_column(String(30), nullable=True)
    document_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    document_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    document_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    document_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    announcement_subject: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    announcement_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    regulatory_filing_id: Mapped[int | None] = mapped_column(ForeignKey("regulatory_filings.id", ondelete="SET NULL"), nullable=True, index=True)
    transaction_status: Mapped[str] = mapped_column(String(24), default="REVIEW", index=True)
    classification_status: Mapped[str] = mapped_column(String(24), default="CLASSIFIED", index=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        Index("ix_capital_action_type_date", "event_type", "event_date"),
        Index("ix_capital_action_company_date", "company_id", "event_date"),
    )

class MarketMetric(Base):
    __tablename__="market_intelligence_metrics"
    id: Mapped[int]=mapped_column(primary_key=True)
    source_name: Mapped[str]=mapped_column(String(120))
    category: Mapped[str]=mapped_column(String(120))
    metric_name: Mapped[str]=mapped_column(String(255))
    metric_value: Mapped[float]=mapped_column(Float)
    unit: Mapped[str|None]=mapped_column(String(80))
    recorded_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), index=True)
 
class VahanObservation(Base):
    __tablename__ = "vahan_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_hash: Mapped[str] = mapped_column(String(64), index=True)
    scrape_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    view: Mapped[str] = mapped_column(String(40), index=True)
    display_type: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(160), index=True)
    rto: Mapped[str] = mapped_column(String(240), index=True)
    year_type: Mapped[str] = mapped_column(String(40))
    year: Mapped[str] = mapped_column(String(20), index=True)
    filter_dimension: Mapped[str | None] = mapped_column(String(80), nullable=True)
    filter_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    y_axis: Mapped[str] = mapped_column(String(80), index=True)
    y_value: Mapped[str] = mapped_column(String(255), index=True)
    x_axis: Mapped[str] = mapped_column(String(80), index=True)
    x_value: Mapped[str] = mapped_column(String(255), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(40), default="count")
    source_url: Mapped[str] = mapped_column(String(1000))
    source_sheet: Mapped[str | None] = mapped_column(String(160), nullable=True)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_row: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "query_hash", "scrape_date", "view", "y_axis", "y_value", "x_axis", "x_value",
            name="uq_vahan_observation_snapshot",
        ),
        Index("ix_vahan_query_xy", "query_hash", "y_axis", "x_axis"),
        Index("ix_vahan_state_year", "state", "year"),
        Index("ix_vahan_scrape_date", "scrape_date"),
    )

class TranscriptNote(Base):
    __tablename__="crisp_investor_notes"
    id: Mapped[int]=mapped_column(primary_key=True)
    # Nullable keeps pre-ownership notes accessible as legacy shared records.
    user_id: Mapped[int | None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str|None]=mapped_column(String(255), nullable=True)
    source_audio_uri: Mapped[str]=mapped_column(String(1000))
    transcript: Mapped[str|None]=mapped_column(Text)
    notes: Mapped[str|None]=mapped_column(Text)
    diarization: Mapped[list]=mapped_column(JSON, default=list)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
 
class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    notification_type: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id", ondelete="SET NULL"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("insider_signals.id", ondelete="SET NULL"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class NewsSourceRun(Base):
    """Relational audit record for one NewsAlert source/pipeline execution."""
    __tablename__ = "news_source_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items_fetched: Mapped[int] = mapped_column(Integer, default=0)
    articles_created: Mapped[int] = mapped_column(Integer, default=0)
    articles_skipped: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    lookback_days: Mapped[int] = mapped_column(Integer, default=2557)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_news_source_runs_started_status", "started_at", "status"),)

class NewsArticleAnalysis(Base):
    """Versioned and reproducible Groq/heuristic assessment for a persisted article."""
    __tablename__ = "news_article_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="heuristic")
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40), default="news-relevance-v1")
    relevance_score: Mapped[int] = mapped_column(Integer, default=0)
    materiality: Mapped[str] = mapped_column(String(30), default="unknown")
    event_type: Mapped[str] = mapped_column(String(40), default="NEWS")
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (Index("ix_news_article_analyses_article_created", "article_id", "created_at"),)

class CapitalActionAssessment(Base):
    """Versioned decision support for capital-ownership events; event facts remain authoritative."""
    __tablename__ = "capital_action_assessments"
    id: Mapped[int] = mapped_column(primary_key=True)
    capital_action_id: Mapped[int] = mapped_column(ForeignKey("capital_action_signals.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(40), default="heuristic")
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assessment_version: Mapped[str] = mapped_column(String(40), default="capital-assessment-v1")
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    review_required: Mapped[bool] = mapped_column(Boolean, default=True)
    assessment: Mapped[str] = mapped_column(String(40), default="REVIEW")
    rationale: Mapped[str] = mapped_column(Text, default="")
    missing_information: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    __table_args__ = (Index("ix_capital_assessments_event_created", "capital_action_id", "created_at"),)

class RegulatorySyncError(Base):
    """Normalized run error rows; sync-state failures are not stored in a JSON column."""
    __tablename__ = "regulatory_sync_errors"
    id: Mapped[int] = mapped_column(primary_key=True)
    desk: Mapped[str] = mapped_column(String(40), index=True)
    error_key: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

class SourceDocument(Base):
    __tablename__="source_documents"
    id: Mapped[int]=mapped_column(primary_key=True)
    source_type: Mapped[str]=mapped_column(String(80))
    source_uri: Mapped[str]=mapped_column(String(1000), unique=True)
    content_hash: Mapped[str]=mapped_column(String(64), index=True)
    object_key: Mapped[str|None]=mapped_column(String(1000))
    mime_type: Mapped[str|None]=mapped_column(String(255))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
 
class EventOutbox(Base):
    __tablename__="event_outbox"
    id: Mapped[int]=mapped_column(primary_key=True)
    event_id: Mapped[str]=mapped_column(String(36), unique=True, index=True)
    event_type: Mapped[str]=mapped_column(String(80))
    subject: Mapped[str]=mapped_column(String(160))
    payload: Mapped[dict]=mapped_column(JSON, default=dict)
    status: Mapped[str]=mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int]=mapped_column(default=0)
    last_error: Mapped[str|None]=mapped_column(Text)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
 
class ProcessingJob(Base):
    __tablename__="processing_jobs"
    id: Mapped[int]=mapped_column(primary_key=True)
    request_id: Mapped[str]=mapped_column(String(36), unique=True)
    trace_id: Mapped[str]=mapped_column(String(36), index=True)
    event_id: Mapped[str]=mapped_column(String(36), index=True)
    application: Mapped[str]=mapped_column(String(80))
    workflow_stage: Mapped[str]=mapped_column(String(120))
    status: Mapped[str]=mapped_column(String(50))
    error_message: Mapped[str|None]=mapped_column(Text)
    retry_count: Mapped[int]=mapped_column(default=0)
    started_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True))
    result: Mapped[dict|None]=mapped_column(JSON, nullable=True)
 
class UserSession(Base):
    """Authenticated browser session used for admin usage analytics.

    One row is created when a user successfully logs in. The frontend updates
    last_seen_at and engaged-time counters while the session is active, and
    logout_at is populated on an explicit sign-out.
    """
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    logout_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )
    last_app: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
    )
    active_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    app_usage: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    # session_id, user_id, and last_seen_at already request indexes through
    # mapped_column(); defining them again here creates duplicate index names and
    # prevents automatic schema creation in a fresh SQLite development database.
    __table_args__ = (Index("ix_user_sessions_login_at", "login_at"),)

class AuditLog(Base):
    __tablename__="audit_logs"
    id: Mapped[int]=mapped_column(primary_key=True)
    user_id: Mapped[int|None]=mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str]=mapped_column(String(120))
    resource: Mapped[str]=mapped_column(String(120))
    resource_id: Mapped[str|None]=mapped_column(String(120))
    detail: Mapped[dict]=mapped_column(JSON, default=dict)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True), default=utcnow)
# ---------------------------------------------------------------------------
# Primary, frontend-facing records for the News Alert and Regulatory Desk.
# Legacy Article/InsiderSignal/CapitalActionSignal tables remain during migration
# so other modules and historic evidence are not destroyed prematurely.
# ---------------------------------------------------------------------------
class NewsAlertItem(Base):
    __tablename__ = "news_alert_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255), index=True)
    nse_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    headline: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(1500))
    source: Mapped[str] = mapped_column(String(160), index=True)
    analyst: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    content_type: Mapped[str] = mapped_column(String(20), default="NEWS", index=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    materiality: Mapped[str | None] = mapped_column(String(30), nullable=True)
    relevance_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    processing_status: Mapped[str] = mapped_column(String(30), default="VISIBLE", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_news_alert_company_published", "company_id", "published_at"),
        Index("ix_news_alert_type_published", "content_type", "published_at"),
    )


class NewsAlertSource(Base):
    """User-managed public sources consumed by the unified NewsAlert pipeline."""
    __tablename__ = "news_alert_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(String(1500), nullable=False)
    normalized_value: Mapped[str] = mapped_column(String(1500), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_type", "normalized_value", name="uq_news_alert_source_type_value"),
        Index("ix_news_alert_sources_type_enabled", "source_type", "enabled"),
    )


class InsiderTransaction(Base):
    __tablename__ = "insider_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    legacy_signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    stock_name: Mapped[str] = mapped_column(String(255), index=True)
    nse_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    client_name: Mapped[str] = mapped_column(String(255), index=True)
    category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    transaction_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # NSE's filing/broadcast date is distinct from the date on which the trade
    # occurred. The Regulatory Desk is windowed by the disclosure date; retaining
    # it as a first-class column prevents recent filings about older trades from
    # being filtered out during ingestion and projection.
    broadcast_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    cmp_vs_avg_transaction_price_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_stock_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_transaction_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    analyst: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(80), default="NSE_PIT", index=True)
    source_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    source_filing_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_insider_transactions_company_date", "company_id", "transaction_date"),
        Index("ix_insider_transactions_symbol_date", "nse_symbol", "transaction_date"),
    )


class CapitalAction(Base):
    __tablename__ = "capital_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    legacy_signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    event: Mapped[str] = mapped_column(String(80), index=True)
    company_name: Mapped[str] = mapped_column(String(255), index=True)
    nse_symbol: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    investor_allottee: Mapped[str | None] = mapped_column(String(500), nullable=True)
    subtype: Mapped[str | None] = mapped_column(String(120), nullable=True)
    shares_warrants: Mapped[float | None] = mapped_column(Float, nullable=True)
    issue_transaction_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_issue_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    ownership_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    cmp: Mapped[float | None] = mapped_column(Float, nullable=True)
    importance: Mapped[str | None] = mapped_column(String(40), nullable=True)
    importance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    event_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyst: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(120), default="NSE_CA", index=True)
    source_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    source_announcement_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    transaction_status: Mapped[str] = mapped_column(String(24), default="REVIEW", index=True)
    classification_status: Mapped[str] = mapped_column(String(24), default="REVIEW", index=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_capital_actions_company_date", "company_id", "event_date"),
        Index("ix_capital_actions_type_date", "event", "event_date"),
        Index("ix_capital_actions_symbol_date", "nse_symbol", "event_date"),
    )
class EarningsCalendarEvent(Base):
    """Coverage-scoped results/concall events scraped from public calendar sources."""
    __tablename__ = "earnings_calendar_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    company_name: Mapped[str] = mapped_column(String(255))
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    pending_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str] = mapped_column(String(20), index=True)  # Results / Concall
    call_time: Mapped[str | None] = mapped_column(String(40), nullable=True)
    event_title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str] = mapped_column(String(1500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        Index("ix_earnings_calendar_date_type", "event_date", "event_type"),
        Index("ix_earnings_calendar_company_date", "company_id", "event_date"),
    )

class RegulatoryActivityEvent(Base):
    """Single normalized event stream used by the Regulatory Activity frontend."""
    __tablename__ = "regulatory_activity_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_exchange: Mapped[str] = mapped_column(String(12), default="NSE", index=True)
    activity_type: Mapped[str] = mapped_column(String(40), index=True)
    activity_sub_type: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    company_name: Mapped[str] = mapped_column(String(255), index=True)
    symbol: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    isin: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    event_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    disclosure_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    person_allottee_client: Mapped[str | None] = mapped_column(String(500), nullable=True)
    action: Mapped[str | None] = mapped_column(String(160), nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_inr: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    holding_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_market_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_price_change_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="PARTIAL", index=True)
    source_url: Mapped[str | None] = mapped_column(String(1500), nullable=True)
    details_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    source_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("source_key", name="uq_regulatory_activity_source_key"),
        Index("ix_reg_activity_type_date", "activity_type", "event_date"),
        Index("ix_reg_activity_symbol_date", "symbol", "event_date"),
        Index("ix_reg_activity_exchange_date", "source_exchange", "event_date"),
    )
