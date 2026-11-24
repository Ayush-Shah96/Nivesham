from bs4 import BeautifulSoup
from apps.signal_radar.collectors.public_portal import _parse_publication_date, _labelled_value, _extract_page

def test_publication_date_requires_explicit_metadata():
    soup = BeautifulSoup('<html><body>Published 12/10/2026. Deadline 30/10/2026.</body></html>', 'html.parser')
    assert _parse_publication_date(soup) is None

def test_publication_date_reads_article_meta():
    soup = BeautifulSoup('<meta property="article:published_time" content="2026-10-12T10:00:00Z">', 'html.parser')
    assert _parse_publication_date(soup).year == 2026

def test_labelled_fields_are_not_guessed():
    assert _labelled_value('A factory may be built in Pune', r'(?:project location|location)') is None
    assert _labelled_value('Project Location: Pune, Maharashtra', r'(?:project location|location)') == 'Pune, Maharashtra'

def test_generic_portal_homepage_is_not_promoted_as_event():
    source = {'id':'x','name':'Example','category':'CapEx & Expansion','access':'public_portal','keywords':['environmental clearance','capacity']}
    assert _extract_page('https://example.com/', '<title>Welcome</title><body>About us Contact</body>', source) is None
