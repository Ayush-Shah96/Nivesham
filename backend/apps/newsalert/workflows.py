from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import re

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ii_platform.database.models import Article, ArticleCompanyMention, Company, NewsArticleAnalysis, TrackMode
from ii_platform.events.schemas import Event, EventMetadata, EventType
from apps.newsalert.classification import classify_topic, NEWS_TYPES
from ii_platform.events.outbox import stage_event


def _hash_text(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _detect_interview(title: str, content: str = "") -> bool:
    """Detect if content appears to be an interview based on title and content patterns."""
    text = f"{title} {content}".lower()
    
    interview_keywords = [
        r'\binterview\b',
        r'\binterviewed\b',
        r'\binterview\s+with\b',
        r'\bconversation\s+with\b',
        r'\bfireside\s+chat\b',
        r'\bq&a\b',
        r'\bqa\s+session\b',
        r'\bexclusive\s+chat\b',
        r'\bspeaks?\s+to\b',
        r'\bvideo\s+interview\b',
        r'\bpodcast\s+interview\b',
        r'\bin\s+conversation\b',
    ]
    
    for pattern in interview_keywords:
        if re.search(pattern, text):
            return True
    
    return False


def public_news_url(item: dict[str, Any], *, nse_symbol: str | None = None) -> str:
    """Return a human-openable article URL.

    NSE corporate/XBRL feeds can expose a raw .xml attachment. That file is useful
    to the collector but is not a useful NewsAlert destination. Convert it to the
    public NSE corporate-filings page, keeping the matched symbol when available.
    """
    url = str(item.get("url") or "").strip()
    if not url:
        return url

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    source_key = str(item.get("source_key") or "").lower()

    nse_archive = host.endswith("nsearchives.nseindia.com") or host.startswith("nsearchives.")
    nse_raw_document = nse_archive and (
        path.endswith((".xml", ".html", ".htm"))
        or "/corporate/ixbrl/" in path
        or "/corporate/" in path and "webxml" in path
    )
    if host.endswith("nseindia.com") and (source_key == "nse_india" or nse_raw_document):
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["tabIndex"] = "equity"
        if nse_symbol:
            query["symbol"] = str(nse_symbol).strip().upper()
        else:
            query.pop("symbol", None)
        return urlunparse((
            "https",
            "www.nseindia.com",
            "/companies-listing/corporate-filings-announcements",
            "",
            urlencode(query),
            "",
        ))

    # Never expose an RSS/Atom document as the user-facing article URL.
    if path.endswith((".rss", ".atom", ".xml")) and source_key not in {"nse_india"} and "news.google.com" not in host:
        source_home = str(item.get("source_homepage") or "").strip()
        if source_home:
            return source_home

    return url


async def _score_persisted_article(db: AsyncSession, article: Article) -> None:
    """Persist a deterministic-only fundamental-news decision; no Groq calls."""
    if not article.matched_company_id:
        return
    existing = await db.scalar(
        select(NewsArticleAnalysis).where(NewsArticleAnalysis.article_id == article.id)
        .order_by(NewsArticleAnalysis.created_at.desc(), NewsArticleAnalysis.id.desc()).limit(1)
    )
    company = await db.get(Company, article.matched_company_id)
    from apps.newsalert.fundamental_relevance import assess_fundamental_news

    assessed = assess_fundamental_news(
        article.title or "",
        article.raw_content or article.summary or "",
        article.source or "",
        content_type=str(article.content_type or "NEWS").upper(),
        portfolio_connection=bool(company),
    )
    article.relevance_score = assessed.score
    article.is_material = bool(assessed.is_material)
    article.news_alert_eligible = bool(assessed.eligible)
    materiality = "HIGH" if assessed.is_material else "MEDIUM" if assessed.eligible else "LOW"
    rationale = assessed.reason or ", ".join(assessed.matched_events) or "No fundamental event matched"
    analysis_values = {
        "provider": "heuristic",
        "model": None,
        "prompt_version": "fundamental-rules-v2",
        "relevance_score": assessed.score,
        "materiality": materiality,
        "event_type": str(article.content_type or "NEWS").upper(),
        "rationale": rationale[:1200],
        "created_at": datetime.now(timezone.utc),
    }
    if existing is not None:
        for key, value in analysis_values.items():
            setattr(existing, key, value)
    else:
        db.add(NewsArticleAnalysis(article_id=article.id, **analysis_values))
    await db.flush()


async def _persist_article_mentions(
    db: AsyncSession,
    article: Article,
    mention_names: list[str] | tuple[str, ...] | None,
    matched_company_id: int | None = None,
) -> None:
    """Reconcile source mentions as rows; no JSON array is stored on Article."""
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in mention_names or []:
        name = " ".join(str(raw_name or "").split()).strip()[:255]
        key = name.casefold()
        if name and key not in seen:
            names.append(name)
            seen.add(key)
    matched = await db.get(Company, matched_company_id) if matched_company_id else None
    if matched and matched.legal_name.casefold() not in seen:
        names.insert(0, matched.legal_name[:255])
        seen.add(matched.legal_name.casefold())

    existing_rows = (await db.execute(
        select(ArticleCompanyMention).where(ArticleCompanyMention.article_id == article.id)
    )).scalars().all()
    desired = {name.casefold(): name for name in names}
    existing = {row.mention_name.casefold(): row for row in existing_rows}

    # Remove stale mention rows when a feed update changes its identified companies.
    for key, row in existing.items():
        if key not in desired:
            await db.delete(row)

    for key, name in desired.items():
        row = existing.get(key)
        company = matched if matched and name.casefold() == matched.legal_name.casefold() else None
        if company is None:
            company = await db.scalar(select(Company).where(Company.legal_name.ilike(name)))
        company_id = company.id if company else None
        if row is not None:
            row.mention_name = name
            row.company_id = company_id
            row.match_method = "matched_company" if company_id else "source_mention"
            row.confidence = 1.0 if company_id else None
        else:
            db.add(ArticleCompanyMention(
                article_id=article.id, company_id=company_id, mention_name=name,
                match_method="matched_company" if company_id else "source_mention",
                confidence=1.0 if company_id else None,
            ))
    await db.flush()


async def persist_article_item(
    db: AsyncSession,
    item: dict[str, Any],
    *,
    source_user_id: int | None = None,
) -> tuple[Article | None, bool]:
    """Persist one article idempotently by canonical URL.

    Returns (article, created). Existing URLs are skipped safely rather than
    raising PostgreSQL articles_url_key UniqueViolationError.
    """
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()

    if not url or not title:
        return None, False

    published_at = item.get("published_at")
    if not isinstance(published_at, datetime):
        published_at = datetime.now(timezone.utc)
    elif published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    raw_content = str(
        item.get("raw_content")
        or item.get("summary")
        or ""
    ).strip()

    source = str(item.get("source") or "Unknown").strip()
    companies = list(item.get("companies_mentioned") or [])
    original_url = str(item.get("source_url") or url).strip() or url
    # Keep the stable, source-specific URL as the relational identity key. In
    # particular, NEVER replace NSE announcement attachment URLs with the generic
    # company filings page here: that collapses distinct announcements for one symbol.
    # The frontend-facing URL is transformed only when serializing the API response.
    # Strip known analytics parameters and sort remaining query fields before using
    # the URL as the unique ingestion key. Pulse and a publisher RSS feed can point to
    # the same story with different UTM parameters; those must resolve to one row.
    url = url.split("#", 1)[0].strip()
    try:
        parsed_url = urlparse(url)
        tracking_names = {"fbclid", "gclid", "ref", "source", "ocid", "cmpid"}
        clean_query = sorted(
            (key, value) for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in tracking_names
        )
        url = urlunparse((
            parsed_url.scheme.casefold(), parsed_url.netloc.casefold(), parsed_url.path,
            parsed_url.params, urlencode(clean_query, doseq=True), ""
        ))
    except Exception:
        pass

    content_hash = str(item.get("content_hash") or "").strip()
    if not content_hash:
        content_hash = _hash_text(f"{title}\n{raw_content}")

    # Content type is source-driven. YouTube is an Interview; X/Twitter is a Post;
    # publisher articles remain News even if their body/headline contains the word
    # "interview". This prevents publisher interviews from leaking into the Interview filter.
    source_lower = source.casefold()
    source_key = str(item.get("source_key") or "").casefold()
    is_youtube = bool(item.get("is_youtube")) or source_lower.startswith("youtube") or source_key == "youtube"
    is_x_post = source_key in {"x_posts", "twitter", "x"} or any(token in source_lower for token in ("x / twitter", "twitter", "x.com"))
    # NewsAlert's top-level type contract is source based: YouTube = Interview,
    # X = Post, publisher/exchange sources = News. Topic classification is a separate
    # relevance feature and must not move publisher stories into the Interview tab.
    content_type = "INTERVIEW" if is_youtube else "POST" if is_x_post else "NEWS"
    is_interview = content_type == "INTERVIEW"
    matched_company_id = item.get("matched_company_id")
    try:
        matched_company_id = int(matched_company_id) if matched_company_id is not None else None
    except (TypeError, ValueError):
        matched_company_id = None

    values = {
        "url": url,
        "title": title,
        "source": source,
        "source_url": original_url[:1000],
        "published_at": published_at,
        "raw_content": raw_content,
        "content_hash": content_hash,
        "processing_status": "ingested",
        "summary": item.get("summary"),
        "relevance_score": item.get("relevance_score"),
        "is_material": bool(item.get("is_material", False)),
        "is_interview": is_interview,
        "content_type": content_type,
        "matched_company_id": matched_company_id,
        "news_alert_eligible": (
            bool(item.get("news_alert_eligible"))
            if item.get("news_alert_eligible") is not None
            else bool(matched_company_id is not None)
        ),
    }

    # Repair/upsert an existing canonical URL before attempting an insert. The previous
    # implementation treated every conflict as a no-op, so an issuer-only title and a
    # wrong interview flag could remain in the DB forever after the scraper learned the
    # real headline.
    article = await db.scalar(
        select(Article).where(Article.url == url)
    )
    if article is not None:
        source_facts_changed = any((
            article.content_hash != content_hash,
            article.title != title,
            (article.raw_content or "") != raw_content,
            article.source != source,
            article.content_type != content_type,
            article.matched_company_id != matched_company_id,
        ))
        if source_facts_changed:
            await db.execute(delete(NewsArticleAnalysis).where(NewsArticleAnalysis.article_id == article.id))
            article.relevance_score = None
            article.is_material = False
            article.news_alert_eligible = bool(matched_company_id)
        else:
            has_analysis = await db.scalar(
                select(NewsArticleAnalysis.id).where(NewsArticleAnalysis.article_id == article.id)
                .order_by(NewsArticleAnalysis.created_at.desc()).limit(1)
            )
            if has_analysis is None:
                if item.get("relevance_score") is not None:
                    article.relevance_score = item.get("relevance_score")
                if item.get("is_material") is not None:
                    article.is_material = bool(item.get("is_material"))
                article.news_alert_eligible = bool(
                    item.get("news_alert_eligible") if item.get("news_alert_eligible") is not None
                    else matched_company_id is not None
                )
        article.title = title
        article.source = source
        article.source_url = original_url[:1000]
        article.published_at = published_at
        article.raw_content = raw_content
        article.summary = item.get("summary") or article.summary
        article.is_interview = is_interview
        article.content_type = content_type
        article.matched_company_id = matched_company_id
        article.content_hash = content_hash
        article.processing_status = "ingested"
        await db.flush()
        await _persist_article_mentions(db, article, companies, matched_company_id)
        await _score_persisted_article(db, article)
        return article, False

    stmt = (
        pg_insert(Article)
        .values(**values)
        # Do not name a conflict target. This keeps ingestion idempotent for both
        # canonical URL and unique content_hash constraints.
        .on_conflict_do_nothing()
        .returning(Article.id)
    )

    result = await db.execute(stmt)
    created_id = result.scalar_one_or_none()

    if created_id is not None:
        article = await db.get(Article, created_id)
        if article is not None:
            await _persist_article_mentions(db, article, companies, matched_company_id)
            await _score_persisted_article(db, article)
        return article, True

    # A conflict can still be caused by content_hash because another publisher/feed
    # already stored the same content at a different URL. In that case preserve the
    # existing canonical article rather than overwriting it with a duplicate.
    article = await db.scalar(
        select(Article).where(Article.content_hash == content_hash)
    )
    if article is not None:
        await _persist_article_mentions(db, article, companies, matched_company_id)
        await _score_persisted_article(db, article)
    return article, False


async def process_article_event(
    event: Event,
    db: AsyncSession,
    bus,
):
    """Consume an article event and stage downstream processing exactly once."""
    p = event.payload

    existing = None

    if p.get("article_id"):
        existing = await db.get(
            Article,
            int(p["article_id"]),
        )

    if existing is None and p.get("url"):
        existing = await db.scalar(
            select(Article).where(
                Article.url == p["url"]
            )
        )

    if existing is None:
        title = p.get("title") or ""
        raw_content = p.get("raw_content") or ""
        source_name = str(p.get("source") or event.source)
        explicit_type = str(p.get("content_type") or "").strip().upper()
        inferred = classify_topic(title, raw_content, source_type=source_name)
        is_interview = explicit_type == "INTERVIEW" or bool(p.get("is_youtube")) or inferred == "INTERVIEW"
        article_type = explicit_type if explicit_type in NEWS_TYPES else (inferred if inferred in NEWS_TYPES else "NEWS")
        
        article = Article(
            url=p["url"],
            title=title,
            source=p.get("source") or event.source,
            published_at=p["published_at"],
            raw_content=raw_content,
            content_hash=p.get("content_hash")
            or _hash_text(
                f"{title}\n{raw_content}"
            ),
            source_url=p.get("source_url"),
            is_material=bool(
                p.get("is_material", False)
            ),
            is_interview=is_interview,
            content_type=article_type,
            matched_company_id=(int(p["matched_company_id"]) if p.get("matched_company_id") is not None else None),
            news_alert_eligible=bool(
                p.get("news_alert_eligible", False)
            ),
            processing_status="ingested",
        )
        db.add(article)
        await db.flush()
    else:
        article = existing
        article.processing_status = "ingested"

        incoming_title = str(p.get("title") or "").strip()
        if incoming_title:
            article.title = incoming_title
            source_name = str(p.get("source") or article.source or "")
            explicit_type = str(p.get("content_type") or "").strip().upper()
            inferred = classify_topic(article.title, p.get("raw_content") or article.raw_content or p.get("summary") or "", source_type=source_name)
            article.is_interview = explicit_type == "INTERVIEW" or bool(p.get("is_youtube")) or inferred == "INTERVIEW"
            article.content_type = explicit_type if explicit_type in NEWS_TYPES else (inferred if inferred in NEWS_TYPES else "NEWS")

        if p.get("source"):
            article.source = str(p["source"])

        if p.get("published_at"):
            article.published_at = p["published_at"]

        if p.get("raw_content") is not None:
            article.raw_content = str(p.get("raw_content") or "")

        if p.get("summary") is not None:
            article.summary = p.get("summary")

        if p.get("matched_company_id") is not None:
            article.matched_company_id = int(p["matched_company_id"])

        if p.get("news_alert_eligible") is not None:
            article.news_alert_eligible = bool(
                p.get("news_alert_eligible")
            )

        if p.get("is_material"):
            article.is_material = True

    await _persist_article_mentions(db, article, p.get("companies_mentioned"), article.matched_company_id)
    await _score_persisted_article(db, article)

    # Do not create a duplicate downstream event if the article is already
    # marked processed. Redis/outbox consumers can safely receive this event
    # more than once; persistence remains idempotent.
    if article.processing_status != "processed":
        await stage_event(
            db,
            Event(
                event_type=EventType.ARTICLE_PROCESSED,
                source="newsalert",
                payload={
                    "article_id": article.id,
                    "track_mode": (
                        TrackMode.HT.value
                        if article.is_material
                        else TrackMode.LT.value
                    ),
                },
                metadata=EventMetadata(
                    source_system="newsalert"
                ),
            ),
        )

    return article


def choose_batch(track_mode):
    return str(track_mode).upper() == TrackMode.LT.value

async def process_news_ingest_event(event: Event, db: AsyncSession, bus) -> dict[str, Any]:
    """Run a queued NewsAlert ingest from the dedicated worker.

    The API only creates the ProcessingJob and outbox event. This function is the
    actual scrape boundary so browser/network work never occupies an HTTP request.
    """
    from ii_platform.database.models import ProcessingJob
    from apps.newsalert.routes import ingest_configured_news

    request_id = event.metadata.request_id
    job = await db.scalar(select(ProcessingJob).where(ProcessingJob.request_id == request_id))
    if job is None:
        return {"status": "ignored", "reason": "job_not_found", "request_id": request_id}
    if str(job.status).lower() == "completed":
        return job.result or {"status": "completed"}
    if str(job.status).lower() == "running":
        return {"status": "already_running", "run_id": request_id}

    job_id = int(job.id)
    prior_retry_count = int(job.retry_count or 0)
    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.error_message = None
    job.retry_count = prior_retry_count
    await db.flush()

    payload = event.payload or {}
    try:
        # Persist the running state and release this transaction before the
        # network-heavy scrape. The pipeline itself never commits/rolls back.
        await db.commit()
        result = await ingest_configured_news(
            db,
            feed_urls=payload.get("feed_urls"),
            user_id=(int(payload["user_id"]) if payload.get("user_id") is not None else None),
            limit_per_feed=int(payload.get("limit_per_feed") or 30),
            source_ids=payload.get("source_ids"),
            include_youtube=bool(payload.get("include_youtube", True)),
            include_x_posts=bool(payload.get("include_x_posts", True)),
            include_regulatory=bool(payload.get("include_regulatory", False)),
        )
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        job.result = result
        await db.flush()
        return {"status": "completed", **result}
    except Exception as exc:
        await db.rollback()
        # Rollback expires ORM instances even when expire_on_commit=False. Reload
        # the job by its materialized primary key so error recording does not
        # trigger an implicit async lazy-load (MissingGreenlet) while handling
        # the original ingestion exception.
        failed_job = await db.get(ProcessingJob, job_id)
        if failed_job is not None:
            failed_job.status = "failed"
            failed_job.completed_at = datetime.now(timezone.utc)
            failed_job.error_message = str(exc)[:4000]
            failed_job.retry_count = prior_retry_count + 1
            failed_job.result = {"status": "failed", "error": str(exc)[:4000]}
            await db.flush()
            await db.commit()
        raise