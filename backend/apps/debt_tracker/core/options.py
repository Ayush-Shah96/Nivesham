"""Validated Debt Tracker generation settings used by the FastAPI service."""
from datetime import date
import re
from urllib.parse import urlsplit
from typing import Mapping

from .errors import WorkbookSchemaError
from .models import frozen_mapping
from .normalization import normalize_text
from .policies import validated_policy


def generation_options(values=None):
    if values is None:
        values = {}
    if not isinstance(values, Mapping) or set(values) - {"report_date", "baseline_date", "base_sheet", "estimate_bharat_fof", "limit_policy"}:
        raise WorkbookSchemaError("Unsupported report settings.")
    result = {}
    for key in ("report_date", "baseline_date"):
        raw = values.get(key)
        if raw in (None, ""):
            result[key] = None
            continue
        if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            raise WorkbookSchemaError("Report dates must use YYYY-MM-DD.")
        try:
            result[key] = date.fromisoformat(raw)
        except ValueError as exc:
            raise WorkbookSchemaError("Report dates must use YYYY-MM-DD.") from exc
    if result["baseline_date"] and not result["report_date"]:
        raise WorkbookSchemaError("Select a report date when specifying an opening date.")
    if result["baseline_date"] and result["baseline_date"] > result["report_date"]:
        raise WorkbookSchemaError("The opening date cannot be after the report date.")
    sheet = values.get("base_sheet", "")
    if sheet is None:
        sheet = ""
    if not isinstance(sheet, str) or len(sheet) > 31 or any(ord(c) < 32 for c in sheet):
        raise WorkbookSchemaError("The mapping base sheet name is invalid.")
    result["base_sheet"] = sheet.strip() or None
    proxy = values.get("estimate_bharat_fof", False)
    if not isinstance(proxy, bool):
        raise WorkbookSchemaError("The ETF estimation setting must be true or false.")
    result["estimate_bharat_fof"] = proxy
    result["limit_policy"] = validated_policy(values.get("limit_policy", "standard"))
    return result


def validated_corrections(values, category_limits):
    if values is None:
        return frozen_mapping()
    if not isinstance(values, (list, tuple)) or len(values) > 1000:
        raise WorkbookSchemaError("The correction list must contain at most 1000 entries.")
    result = {}
    for value in values:
        if not isinstance(value, Mapping):
            raise WorkbookSchemaError("Each correction must contain an issuer and a group or category.")
        key, group, rating = (value.get("issuer_key", ""), value.get("group", ""), value.get("rating_bucket", ""))
        if any(not isinstance(item, str) for item in (key, group, rating)):
            raise WorkbookSchemaError("Invalid correction text.")
        if len(key) > 512 or len(group) > 120 or len(rating) > 80 or any(ord(c) < 32 for c in key + group + rating):
            raise WorkbookSchemaError("The correction exceeds the supported text limits.")
        key, group, rating = normalize_text(key), group.strip(), rating.strip()
        if not key or len(key) > 256 or not (group or rating) or (rating and rating not in category_limits):
            raise WorkbookSchemaError("Select a valid group or rating category for the correction.")
        if key in result and result[key] != (group, rating):
            raise WorkbookSchemaError("Conflicting corrections exist for the same issuer.")
        result[key] = (group, rating)
    return frozen_mapping(result)


def correction_source_urls(values):
    """Retain public source attribution separately from the user's group decision."""
    sources = {}
    domains = ("icra.in", "crisil.com", "crisilratings.com", "careratings.com", "indiaratings.co.in",
               "bseindia.com", "nseindia.com", "sebi.gov.in")
    for value in values or ():
        research = value.get("research")
        if not isinstance(research, Mapping) or not isinstance(research.get("evidence"), (list, tuple)):
            continue
        for item in research["evidence"][:3]:
            if not isinstance(item, Mapping) or item.get("verified") is not True:
                continue
            url = item.get("url")
            if not isinstance(url, str) or len(url) > 2000:
                continue
            try:
                parsed = urlsplit(url)
                host = parsed.hostname or ""
                allowed = parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.port
            except ValueError:
                continue
            if allowed and any(host == domain or host.endswith("." + domain) for domain in domains):
                sources[normalize_text(value["issuer_key"])] = url
                break
    return frozen_mapping(sources)
