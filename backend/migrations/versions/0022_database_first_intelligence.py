"""Replace module JSON blobs/caches with normalized relational intelligence records.

Revision ID: 0022_database_first_intelligence
Revises: 0021_intelligence_pipeline
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from alembic import op
import sqlalchemy as sa

revision = "0022_database_first_intelligence"
down_revision = "0021_intelligence_pipeline"
branch_labels = None
depends_on = None


def _object(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _safe_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_int(value):
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_bool(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "promoter", "pac"}


def _threshold_status_text(value):
    """Flatten the legacy threshold-status object into its relational scalar.

    Older capital-action rows stored a structure such as
    {"client_threshold": 20, "sast_threshold": 25, "state": None, "states": []}.
    The new column is VARCHAR, so binding that structure directly raises an
    asyncpg TypeError. Keep only threshold state labels; do not serialize the
    old structure back into the database.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        states = value.get("states")
        if isinstance(states, (list, tuple)):
            labels = [str(item).strip() for item in states if item not in (None, "")]
            if labels:
                return ", ".join(labels)[:80] or None
        state = value.get("state")
        if isinstance(state, (list, tuple)):
            labels = [str(item).strip() for item in state if item not in (None, "")]
            return ", ".join(labels)[:80] or None
        if state not in (None, "") and isinstance(state, (str, int, float)):
            return str(state).strip()[:80] or None
        return None
    if isinstance(value, (list, tuple)):
        labels = [str(item).strip() for item in value if item not in (None, "")]
        return ", ".join(labels)[:80] or None
    if isinstance(value, (str, int, float)):
        return str(value).strip()[:80] or None
    return None



def _safe_text(value, max_length: int = 200_000):
    """Return legitimate textual content, never a binary document payload.

    Legacy details sometimes contain a decoded PDF/ZIP byte stream. PostgreSQL
    text cannot contain NUL (0x00), and storing a ZIP/PDF byte stream as text is
    data corruption even when the NUL bytes are removed. Keep source URL/hash/size
    metadata and leave extracted text NULL for binary or malformed values.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if raw.startswith((b"PK\x03\x04", b"%PDF-", b"\x89PNG\r\n\x1a\n", b"\x1f\x8b")) or b"\x00" in raw:
            return None
        try:
            value = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
    text = str(value)
    if not text or "\x00" in text:
        return None
    # Catch payloads that have already been decoded with replacement characters.
    if text.startswith(("PK\x03\x04", "%PDF-", "\x89PNG", "\x1f\x8b")):
        return None
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f")
    if controls and controls / max(len(text), 1) > 0.01:
        return None
    return text[:max_length]


def _document_type(value, url):
    """Prefer the real file extension when legacy metadata mislabeled binary as text."""
    current = _safe_text(value, 120)
    clean_url = _safe_text(url, 1000) or ""
    path = clean_url.split("?", 1)[0].split("#", 1)[0].casefold()
    extension = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    known = {"pdf", "zip", "doc", "docx", "xls", "xlsx", "csv", "txt", "html", "htm", "xml"}
    if extension in known and (not current or current.casefold() in {"text", "txt", "text/plain", "unknown", "application/octet-stream"}):
        return extension
    return current

def _safe_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def upgrade() -> None:
    op.create_table(
        "article_company_mentions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("article_id", sa.Integer(), sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mention_name", sa.String(255), nullable=False),
        sa.Column("match_method", sa.String(40), nullable=False, server_default="legacy"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("article_id", "mention_name", name="uq_article_company_mention"),
    )
    op.create_index("ix_article_company_mentions_article_id", "article_company_mentions", ["article_id"])
    op.create_index("ix_article_company_mentions_company_id", "article_company_mentions", ["company_id"])

    op.create_table(
        "regulatory_sync_errors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("desk", sa.String(40), nullable=False),
        sa.Column("error_key", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_regulatory_sync_errors_desk", "regulatory_sync_errors", ["desk"])
    op.create_index("ix_regulatory_sync_errors_created_at", "regulatory_sync_errors", ["created_at"])

    op.add_column("articles", sa.Column("source_url", sa.String(1000), nullable=True))
    op.add_column("news_source_runs", sa.Column("window_start", sa.DateTime(timezone=True), nullable=True))
    op.add_column("news_source_runs", sa.Column("window_end", sa.DateTime(timezone=True), nullable=True))

    insider_cols = [
        ("holding_before", sa.Float()),
        ("holding_before_percent", sa.Float()),
        ("current_price", sa.Float()),
        ("current_price_change_percent", sa.Float()),
        ("current_price_fetched_at", sa.DateTime(timezone=True)),
    ]
    for name, col_type in insider_cols:
        op.add_column("insider_signals", sa.Column(name, col_type, nullable=True))

    capital_cols = [
        ("event_sub_type", sa.String(80)),
        ("pre_holding_pct", sa.Float()),
        ("post_holding_pct", sa.Float()),
        ("holding_change_pct", sa.Float()),
        ("promoter_or_pac", sa.Boolean()),
        ("severity", sa.String(40)),
        ("importance_score", sa.Float()),
        ("threshold_status", sa.String(80)),
        ("confidence", sa.Float()),
        ("purpose", sa.Text()),
        ("event_chain_key", sa.String(255)),
        ("investor_type", sa.String(120)),
        ("isin", sa.String(30)),
        ("document_url", sa.String(1000)),
        ("document_type", sa.String(120)),
        ("document_status", sa.String(80)),
        ("document_sha256", sa.String(64)),
        ("document_pages", sa.Integer()),
        ("document_bytes", sa.Integer()),
        ("announcement_subject", sa.String(1000)),
        ("announcement_details", sa.Text()),
        ("document_summary", sa.Text()),
        ("document_text", sa.Text()),
    ]
    for name, col_type in capital_cols:
        op.add_column("capital_action_signals", sa.Column(name, col_type, nullable=True))
    op.create_index("ix_capital_action_signals_event_chain_key", "capital_action_signals", ["event_chain_key"])

    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    # Move article mentions out of the old JSON column into a normal relation.
    result = bind.execute(sa.text("SELECT id, companies_mentioned, matched_company_id FROM articles"))
    for row in result:
        seen_legacy_names = set()
        for raw_name in _list(row.companies_mentioned):
            name = " ".join(str(raw_name or "").split()).strip()[:255]
            name_key = name.casefold()
            if not name or name_key in seen_legacy_names:
                continue
            seen_legacy_names.add(name_key)
            bind.execute(sa.text(
                "INSERT INTO article_company_mentions "
                "(article_id, company_id, mention_name, match_method, confidence, created_at) "
                "VALUES (:article_id, :company_id, :mention_name, :match_method, :confidence, :created_at)"
            ), {"article_id": row.id, "company_id": row.matched_company_id,
                "mention_name": name, "match_method": "legacy", "confidence": None, "created_at": now})
    op.drop_column("articles", "companies_mentioned")
    bind.execute(sa.text("UPDATE articles SET source_url=url WHERE source_url IS NULL"))
    # Normalize the public NewsAlert categories. Legacy values were topic classes,
    # while the new UI categories describe the source format.
    bind.execute(sa.text("""
        UPDATE articles SET content_type = CASE
          WHEN lower(coalesce(source, '')) LIKE '%youtube%' THEN 'INTERVIEW'
          WHEN lower(coalesce(source, '')) LIKE '%twitter%' OR lower(coalesce(source, '')) LIKE '%x.com%' OR lower(coalesce(source, '')) LIKE 'x /%' THEN 'POST'
          ELSE 'NEWS' END
    """))
    bind.execute(sa.text("UPDATE articles SET is_interview = (content_type = 'INTERVIEW')"))

    # Migrate only normalized quote values from the old raw insider source blob.
    result = bind.execute(sa.text("SELECT id, source_data FROM insider_signals"))
    for row in result:
        data = _object(row.source_data)
        bind.execute(sa.text(
            "UPDATE insider_signals SET current_price=:price, current_price_change_percent=:change, "
            "current_price_fetched_at=:fetched WHERE id=:id"
        ), {"price": _safe_float(data.get("current_price")),
            "change": _safe_float(data.get("current_price_change_percent")),
            "fetched": _safe_datetime(data.get("current_price_fetched_at")), "id": row.id})
    op.drop_column("insider_signals", "source_data")

    # Preserve existing sync errors as one row per error rather than a JSON array.
    result = bind.execute(sa.text("SELECT desk, errors, updated_at FROM regulatory_sync_state"))
    for row in result:
        errors = _list(row.errors)
        for pair in errors[:100]:
            if isinstance(pair, (tuple, list)) and len(pair) >= 2:
                bind.execute(sa.text(
                    "INSERT INTO regulatory_sync_errors (desk, error_key, message, created_at) "
                    "VALUES (:desk, :error_key, :message, :created_at)"
                ), {"desk": row.desk, "error_key": str(pair[0])[:255],
                    "message": str(pair[1])[:4000], "created_at": row.updated_at or now})
    op.drop_column("regulatory_sync_state", "errors")

    bind.execute(sa.text("UPDATE capital_action_signals SET event_type='STRATEGIC_ACQUISITION' WHERE upper(event_type) IN ('PE_TAKEOVER','STRATEGIC_ACQUISITION','ACQUISITION','TAKEOVER')"))
    bind.execute(sa.text("UPDATE capital_action_signals SET event_type='WARRANT' WHERE upper(event_type) IN ('WARRANT','WARRANT_ISSUE','WARRANTS')"))
    bind.execute(sa.text("UPDATE capital_action_signals SET event_type='PREFERENTIAL_ISSUE' WHERE upper(event_type) IN ('PREFERENTIAL_ISSUE','PREFERENTIAL','PREFERENTIAL_ALLOTMENT')"))

    # Convert JSON-encoded Text in CapitalActionSignal.details into real scalar fields
    # and plain text. This is a one-time migration only; runtime code never stores it.
    result = bind.execute(sa.text("SELECT id, details FROM capital_action_signals"))
    for row in result:
        payload = _object(row.details)
        if not payload:
            if row.details and str(row.details).strip().startswith(("{", "[")):
                # Do not retain an unparseable serialized payload in a free-text field.
                bind.execute(sa.text("UPDATE capital_action_signals SET details=:details, document_summary=:summary WHERE id=:id"),
                             {"details": "Legacy structured payload was not parseable; consult the source filing.",
                              "summary": None, "id": row.id})
            elif row.details:
                legacy_text = _safe_text(row.details, 10000)
                if legacy_text:
                    bind.execute(sa.text("UPDATE capital_action_signals SET document_summary=:summary WHERE id=:id"),
                                 {"summary": legacy_text, "id": row.id})
            continue
        parsed = payload.get("parsed") if isinstance(payload.get("parsed"), dict) else {}
        def value(*keys):
            for key in keys:
                if key in parsed and parsed[key] not in (None, ""):
                    return parsed[key]
                if key in payload and payload[key] not in (None, ""):
                    return payload[key]
            return None
        bind.execute(sa.text("""
            UPDATE capital_action_signals SET
                event_sub_type=:event_sub_type, pre_holding_pct=:pre_holding_pct,
                post_holding_pct=:post_holding_pct, holding_change_pct=:holding_change_pct,
                promoter_or_pac=:promoter_or_pac, severity=:severity, importance_score=:importance_score,
                threshold_status=:threshold_status, confidence=:confidence, purpose=:purpose,
                event_chain_key=:event_chain_key, investor_type=:investor_type, isin=:isin,
                document_url=:document_url, document_type=:document_type, document_status=:document_status,
                document_sha256=:document_sha256, document_pages=:document_pages, document_bytes=:document_bytes,
                announcement_subject=:announcement_subject, announcement_details=:announcement_details,
                document_summary=:document_summary, document_text=:document_text, details=:plain_details
            WHERE id=:id
        """), {
            "event_sub_type": _safe_text(value("event_sub_type"), 80),
            "pre_holding_pct": _safe_float(value("pre_holding_pct")),
            "post_holding_pct": _safe_float(value("post_holding_pct")),
            "holding_change_pct": _safe_float(value("holding_change_pct")),
            "promoter_or_pac": _safe_bool(value("promoter_or_pac")),
            "severity": _safe_text(value("severity"), 40), "importance_score": _safe_float(value("importance_score")),
            "threshold_status": _safe_text(_threshold_status_text(value("threshold_status", "threshold_state")), 80),
            "confidence": _safe_float(value("confidence")), "purpose": _safe_text(value("purpose"), 10000),
            "event_chain_key": _safe_text(value("event_chain_key"), 255), "investor_type": _safe_text(value("investor_type"), 120), "isin": _safe_text(value("isin"), 30),
            "document_url": _safe_text(value("document_url"), 1000), "document_type": _document_type(value("document_type"), value("document_url")),
            "document_status": _safe_text(value("document_status"), 80), "document_sha256": _safe_text(value("document_sha256"), 64),
            "document_pages": _safe_int(value("document_pages")), "document_bytes": _safe_int(value("document_bytes")),
            "announcement_subject": _safe_text(payload.get("announcement_subject"), 1000),
            "announcement_details": _safe_text(payload.get("announcement_details"), 10000),
            "document_summary": _safe_text(payload.get("document_summary"), 10000),
            # Never insert downloaded PDF/ZIP/Office bytes into a PostgreSQL TEXT column.
            # A null value here means textual extraction was not safely available; the
            # source URL, checksum, document type and size remain preserved above.
            "document_text": _safe_text(payload.get("document_text")),
            "plain_details": _safe_text(payload.get("document_summary") or payload.get("announcement_details"), 10000) or "",
            "id": row.id,
        })


def downgrade() -> None:
    # Data-normalizing migration is intentionally not reversible without reintroducing
    # the JSON blobs this migration removes. Keep the migration history explicit.
    op.drop_index("ix_capital_action_signals_event_chain_key", table_name="capital_action_signals")
    for name in ("document_text", "document_summary", "announcement_details", "announcement_subject", "document_bytes",
                 "document_pages", "document_sha256", "document_status", "document_type", "document_url", "isin",
                 "investor_type", "event_chain_key", "purpose", "confidence", "threshold_status", "importance_score",
                 "severity", "promoter_or_pac", "holding_change_pct", "post_holding_pct", "pre_holding_pct", "event_sub_type"):
        op.drop_column("capital_action_signals", name)
    op.add_column("regulatory_sync_state", sa.Column("errors", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("insider_signals", sa.Column("source_data", sa.JSON(), nullable=True))
    for name in ("current_price_fetched_at", "current_price_change_percent", "current_price", "holding_before_percent", "holding_before"):
        op.drop_column("insider_signals", name)
    op.drop_column("news_source_runs", "window_end")
    op.drop_column("news_source_runs", "window_start")
    op.drop_column("articles", "source_url")
    op.drop_index("ix_regulatory_sync_errors_created_at", table_name="regulatory_sync_errors")
    op.drop_index("ix_regulatory_sync_errors_desk", table_name="regulatory_sync_errors")
    op.drop_table("regulatory_sync_errors")
    op.drop_index("ix_article_company_mentions_company_id", table_name="article_company_mentions")
    op.drop_index("ix_article_company_mentions_article_id", table_name="article_company_mentions")
    op.drop_table("article_company_mentions")