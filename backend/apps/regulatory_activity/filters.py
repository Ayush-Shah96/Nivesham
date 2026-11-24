def qualifies(market_cap, person_type, lookback_days=None):
    """Return whether a filing belongs to the Regulatory Desk universe.

    The date argument is retained for compatibility but is intentionally not a
    two-year gate. The UI date picker/API query controls the requested period.
    """
    try:
        market_ok = float(market_cap or 0) >= 10_000_000_000
    except (TypeError, ValueError):
        market_ok = False
    normalized = str(person_type or "").strip().lower().replace("_", " ")
    category_ok = normalized in {
        "promoter", "promoter group", "promoter and director", "promoter & director",
        "employee", "employees", "employee immediate relative", "employees immediate relative",
        "designated person", "designated persons", "trust", "trustee", "kmp",
        "key managerial personnel", "key insider",
    }
    return market_ok and category_ok
