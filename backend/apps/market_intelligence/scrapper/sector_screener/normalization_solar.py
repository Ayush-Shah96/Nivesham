"""Compatibility marker for the retired NISE browser-response normalizer.

The direct HTTP Solar collector now emits the normalized Sector Screener
contract directly from ``nise_http_capture``.
"""

def normalize_nise_manufacturing(*args, **kwargs):
    raise RuntimeError(
        "The legacy NISE XHR normalizer has been retired; use nise_http_capture.scrape_product."
    )
