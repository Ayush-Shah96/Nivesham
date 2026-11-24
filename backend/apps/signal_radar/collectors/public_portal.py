from __future__ import annotations

import hashlib
import logging
import re
import asyncio
import random
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag

import httpx
import fitz
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b"),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
]
EVENT_RULES = {
    "CapEx & Expansion": ["capex", "capital expenditure", "investment", "expansion", "new plant", "manufacturing facility", "capacity"],
    "Factories & Projects": ["factory", "industrial park", "land allotment", "project approval", "commissioning", "consent to establish"],
    "Tenders & Contracts": ["tender", "procurement", "contract award", "bid submission", "purchase order"],
    "Products & Technology": ["patent", "certification", "product launch", "technology", "medical device", "drug approval"],
    "Policy & Regulation": ["notification", "amendment", "policy", "import duty", "export restriction", "incentive"],
    "Competitor Activity": ["acquisition", "merger", "investor presentation", "board meeting", "order win", "strategic partnership"],
    "Supply Chain & Customers": ["import", "export", "shipment", "supplier", "customer", "trade statistics"],
}


def _event_type(text: str, category: str) -> str:
    hay = text.casefold()
    for label, words in EVENT_RULES.items():
        if any(word in hay for word in words):
            return label
    return category


def _parse_publication_date(soup: BeautifulSoup):
    # Only trust explicit publication metadata; do not mistake dates embedded in
    # body text (deadlines, historical references, footer copyright) for publish time.
    candidates = []
    for attrs in (
        {"property": "article:published_time"}, {"name": "datePublished"},
        {"itemprop": "datePublished"}, {"name": "pubdate"},
        {"name": "publish-date"}, {"name": "DC.date"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            candidates.append(str(tag.get("content")))
    time_tag = soup.find("time", attrs={"datetime": True})
    if time_tag:
        candidates.append(str(time_tag.get("datetime")))
    for raw in candidates:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        except ValueError:
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw[:10], fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def _labelled_value(text: str, labels: str, max_len: int = 180):
    match = re.search(rf"(?:{labels})\s*[:\-]\s*([^|;\n]{{2,{max_len}}})", text, re.I)
    return re.sub(r"\s+", " ", match.group(1)).strip(" .,:-") if match else None


def _impact_hypothesis(category: str) -> str:
    return {
        "CapEx & Expansion": "Potential implications for future capacity, depreciation, funding needs and competitive supply; validate project execution and company exposure.",
        "Factories & Projects": "Potential implications for regional capacity, commissioning timelines and supplier demand; approval does not establish that construction has started.",
        "Tenders & Contracts": "Potential implications for order inflow and revenue visibility only if the company is a confirmed bidder or awardee.",
        "Products & Technology": "Potential implications for product pipeline or technology positioning; a filing or certification alone does not prove commercial success.",
        "Policy & Regulation": "Potential implications for industry demand, compliance costs, pricing and market access; assess the final notified rule and effective date.",
        "Competitor Activity": "Potential implications for competitive intensity, capacity, market share and margins; corroborate the disclosed development.",
        "Supply Chain & Customers": "Potential implications for sourcing, customer concentration, input costs and trade exposure; shipment data may not identify beneficial ownership.",
    }.get(category, "Potential business implications require analyst verification.")


def _clean_url(url: str) -> str:
    url, _ = urldefrag(url)
    return url.split("#", 1)[0]


def _same_domain(seed: str, candidate: str) -> bool:
    a = (urlparse(seed).hostname or "").lower().removeprefix("www.")
    b = (urlparse(candidate).hostname or "").lower().removeprefix("www.")
    return bool(a and b and (a == b or a.endswith("." + b) or b.endswith("." + a)))


def _extract_page(url: str, html: str, source: dict, page_title: str | None = None):
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")
    title = re.sub(r"\s+", " ", title).strip()
    description = ""
    meta = soup.find("meta", attrs={"name": re.compile("description", re.I)}) or soup.find("meta", attrs={"property": "og:description"})
    if meta:
        description = str(meta.get("content") or "").strip()
    visible = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:5000]
    primary_text = " ".join([page_title or "", title, description, visible[:500]])
    combined = " ".join([primary_text, visible[:1800]])
    matched = [k for k in source["keywords"] if k.casefold() in primary_text.casefold()]
    # Avoid turning a portal homepage's navigation/footer into a fake event.
    if not matched:
        return None
    display_title = page_title or title or source["name"]
    if len(display_title) < 8:
        display_title = f"{source['name']}: {display_title}"
    summary = description or visible[:700] or None
    publication = _parse_publication_date(soup)
    company = _labelled_value(visible, r"(?:company name|project proponent|applicant name|bidder name|vendor name)")
    location = _labelled_value(visible, r"(?:project location|site location|district|location|state)")
    investment = _labelled_value(visible, r"(?:project cost|project investment|estimated investment|investment amount|capital investment)")
    capacity = _labelled_value(visible, r"(?:proposed capacity|production capacity|installed capacity|capacity)")
    status = "needs_review"
    lower_visible = visible.casefold()
    if any(term in lower_visible for term in ("approved", "granted", "awarded", "issued")):
        status = "source_mentions_approval"
    elif any(term in lower_visible for term in ("application submitted", "under process", "pending", "invited")):
        status = "application_or_tender_stage"
    canonical = soup.find("link", rel="canonical")
    canonical_url = _clean_url(urljoin(url, canonical.get("href"))) if canonical and canonical.get("href") else _clean_url(url)
    return {
        "source_id": source["id"], "source_name": source["name"],
        "category": source["category"], "event_type": _event_type(combined, source["category"]),
        "title": display_title[:1000], "summary": summary,
        "source_url": canonical_url, "publication_date": publication,
        "company_name": company, "location": location, "estimated_investment": investment, "estimated_capacity": capacity,
        "current_status": status, "potential_impact": _impact_hypothesis(source["category"]), "confidence": 0.25,
        "extraction_method": "public_html_metadata_and_link", "matched_terms": matched,
        "raw_metadata": {"page_title": title, "description": description, "matched_terms": matched, "access_mode": source["access"], "publication_date_extracted": publication is not None, "field_extraction": "explicit labels only; blank fields are not inferred"},
    }


def _extract_pdf(url: str, content: bytes, source: dict, link_title: str = ""):
    try:
        document = fitz.open(stream=content, filetype="pdf")
        text = " ".join(page.get_text("text") for page in document[:8])
        text = re.sub(r"\s+", " ", text).strip()
        metadata = document.metadata or {}
    except Exception as exc:
        logger.info("SIGNAL_RADAR_PDF_PARSE_FAILED source=%s url=%s error=%s", source["id"], url, str(exc)[:160])
        return None
    if not text:
        return None
    title = (metadata.get("title") or link_title or "").strip()
    preview = text[:1800]
    primary = " ".join([title, preview[:700]])
    matched = [word for word in source["keywords"] if word.casefold() in primary.casefold()]
    if not matched:
        return None
    company = _labelled_value(text, r"(?:company name|project proponent|applicant name|bidder name|vendor name)")
    location = _labelled_value(text, r"(?:project location|site location|district|location|state)")
    investment = _labelled_value(text, r"(?:project cost|project investment|estimated investment|investment amount|capital investment)")
    capacity = _labelled_value(text, r"(?:proposed capacity|production capacity|installed capacity|capacity)")
    return {
        "source_id": source["id"], "source_name": source["name"], "category": source["category"],
        "event_type": _event_type(primary, source["category"]),
        "title": (title or link_title or f"{source['name']} public document")[:1000],
        "summary": preview[:700] or None, "source_url": _clean_url(url),
        "publication_date": None, "company_name": company, "location": location,
        "estimated_investment": investment, "estimated_capacity": capacity,
        "current_status": "needs_review", "potential_impact": _impact_hypothesis(source["category"]),
        "confidence": 0.30, "extraction_method": "public_pdf_text_extraction",
        "matched_terms": matched, "raw_metadata": {"pdf_title": metadata.get("title"), "pdf_author": metadata.get("author"), "page_count": len(document), "publication_date_extracted": False, "field_extraction": "explicit labels only; blank fields are not inferred"},
    }


async def _get_with_retry(client: httpx.AsyncClient, url: str, *, attempts: int = 3):
    """Retry transient network/server failures; never retry access denials indefinitely."""
    last_error = None
    for attempt in range(attempts):
        try:
            response = await client.get(url)
            if response.status_code in (408, 425, 429, 500, 502, 503, 504) and attempt < attempts - 1:
                retry_after = response.headers.get("retry-after")
                try:
                    delay = min(float(retry_after), 12.0) if retry_after else (1.5 * (2 ** attempt))
                except ValueError:
                    delay = 1.5 * (2 ** attempt)
                await asyncio.sleep(delay + random.random() * 0.25)
                continue
            response.raise_for_status()
            return response
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt >= attempts - 1:
                raise
            await asyncio.sleep(1.0 * (2 ** attempt) + random.random() * 0.25)
    if last_error:
        raise last_error
    raise httpx.RequestError(f"Request failed after {attempts} attempts", request=None)


def _candidate_links(soup: BeautifulSoup, base_url: str, source: dict, max_links: int):
    """Discover relevant links by keyword, document type and common portal sections."""
    keywords = [str(x).casefold() for x in source.get("keywords", [])]
    section_terms = (
        "announcement", "notification", "circular", "tender", "procurement", "award",
        "project", "clearance", "environment", "consent", "download", "document",
        "report", "filing", "patent", "journal", "press-release", "press release",
        "investment", "allotment", "industrial", "trade", "export", "import",
    )
    candidates = []
    seen = set()
    for anchor in soup.find_all("a", href=True):
        raw_href = anchor.get("href", "").strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        href = _clean_url(urljoin(base_url, raw_href))
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        combined = f"{label} {href}".casefold()
        if not _same_domain(base_url, href) or href in seen or href == base_url:
            continue
        path = urlparse(href).path.casefold()
        relevant = any(term in combined for term in keywords + list(section_terms))
        document = path.endswith((".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx"))
        if not (relevant or document):
            continue
        seen.add(href)
        candidates.append((href, label[:240]))
        if len(candidates) >= max_links:
            break
    return candidates


async def collect_source(source: dict, *, timeout_seconds: float = 25.0, max_links: int = 12) -> dict:
    headers = {
        "User-Agent": "EquityResearchSignalRadar/1.1 (public-source-monitor; contact: administrator)",
        "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,text/csv,*/*;q=0.7",
        "Accept-Language": "en-IN,en;q=0.9",
    }
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 12.0), read=timeout_seconds, write=timeout_seconds, pool=timeout_seconds)
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=4)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers, limits=limits) as client:
        response = await _get_with_retry(client, source["url"])
        content_type = response.headers.get("content-type", "").lower()
        base_diagnostics = {
            "requested_url": source["url"], "final_url": str(response.url),
            "content_type": content_type, "http_status": response.status_code,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "adapter": "bounded_public_portal_discovery",
        }
        if "pdf" in content_type or str(response.url).lower().endswith(".pdf"):
            parsed = _extract_pdf(str(response.url), response.content, source, link_title=source["name"])
            records = [parsed] if parsed else []
            return {"http_status": response.status_code, "records": records,
                    "status": "success" if records else "no_records",
                    "diagnostics": {**base_diagnostics, "records_after_dedupe": len(records), "note": "Direct PDF fetched; scanned PDFs may require OCR."}}
        if "html" not in content_type and "xml" not in content_type and "text/plain" not in content_type:
            return {"http_status": response.status_code, "records": [], "status": "unsupported_content",
                    "diagnostics": {**base_diagnostics, "note": "Portal responded, but this collector cannot parse this content type; use a source-specific API/document adapter."}}

        soup = BeautifulSoup(response.text, "html.parser")
        candidates = _candidate_links(soup, str(response.url), source, max_links)
        records, link_errors, skipped = [], [], 0
        for link, link_label in candidates:
            try:
                item = await _get_with_retry(client, link)
                ctype = item.headers.get("content-type", "").lower()
                if "pdf" in ctype or item.url.path.lower().endswith(".pdf"):
                    parsed = _extract_pdf(str(item.url), item.content, source, link_title=link_label)
                elif "html" in ctype or "xml" in ctype or "text/plain" in ctype:
                    parsed = _extract_page(str(item.url), item.text, source, page_title=link_label)
                else:
                    parsed = None
                    skipped += 1
                if parsed:
                    parsed["raw_metadata"].update({"http_status": item.status_code, "content_type": ctype, "final_url": str(item.url)})
                    records.append(parsed)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response else None
                link_errors.append({"url": link, "error_type": "http_status", "http_status": status, "message": str(exc)[:240]})
            except httpx.TimeoutException as exc:
                link_errors.append({"url": link, "error_type": "timeout", "message": str(exc)[:240]})
            except httpx.TransportError as exc:
                link_errors.append({"url": link, "error_type": "network", "message": str(exc)[:240]})
            except Exception as exc:
                link_errors.append({"url": link, "error_type": type(exc).__name__, "message": str(exc)[:240]})

        unique = {}
        for record in records:
            digest = hashlib.sha256((record["source_url"] + "|" + record["title"]).encode()).hexdigest()
            record["content_hash"] = digest
            unique[record["source_url"]] = record
        status = "partial" if link_errors else ("success" if unique else "no_records")
        note = "Some linked pages failed; see link_errors." if link_errors else "No relevant records extracted from discovered public links. A 200 homepage response does not establish dataset coverage."
        return {
            "http_status": response.status_code, "records": list(unique.values()), "status": status,
            "diagnostics": {**base_diagnostics, "candidate_links": len(candidates),
                "records_after_dedupe": len(unique), "skipped_unsupported": skipped,
                "link_error_count": len(link_errors), "link_errors": link_errors[:20], "note": note},
        }
