"""Compatibility marker for the retired NISE browser/XHR scraper.

All Solar DCR source collection now runs through ``nise_http_capture`` using
direct HTTP requests. The Playwright/XHR capture implementation has been
removed from this module.
"""

def capture_manufacturing_view(*args, **kwargs):
    raise RuntimeError(
        "NISE browser/XHR capture has been retired; use nise_http_capture.scrape_product."
    )
