from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NewsSourceProfile:
    key: str
    name: str
    domains: tuple[str, ...]
    homepage_url: str
    discovery_urls: tuple[str, ...] = ()
    article_urls: tuple[str, ...] = ()
    feed_urls: tuple[str, ...] = ()
    source_type: str = "publisher"
    enabled: bool = True
    max_pages: int = 3
    tags: tuple[str, ...] = field(default_factory=tuple)


NEWS_SOURCES: tuple[NewsSourceProfile, ...] = (
    NewsSourceProfile(
        key="economic_times",
        name="The Economic Times",
        domains=("economictimes.indiatimes.com", "indiatimes.com"),
        homepage_url="https://economictimes.indiatimes.com/",
        discovery_urls=(
            "https://economictimes.indiatimes.com/rss.cms",
            "https://economictimes.indiatimes.com/rssfeedsitemap.cms",
        ),
        feed_urls=(
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
        ),
        article_urls=(
            "https://economictimes.indiatimes.com/",
            "https://economictimes.indiatimes.com/markets",
            "https://economictimes.indiatimes.com/industry",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="business_standard",
        name="Business Standard",
        domains=("business-standard.com",),
        homepage_url="https://www.business-standard.com/",
        discovery_urls=("https://www.business-standard.com/rss-feeds/listing",),
        feed_urls=(
            "https://www.business-standard.com/rss/markets-106.rss",
            "https://www.business-standard.com/rss/companies-101.rss",
        ),
        article_urls=(
            "https://www.business-standard.com/companies/news",
            "https://www.business-standard.com/markets",
            "https://www.business-standard.com/finance",
        ),
        tags=("news", "companies", "markets"),
    ),
    NewsSourceProfile(
        key="zerodha_pulse",
        name="Zerodha Pulse",
        domains=("pulse.zerodha.com",),
        homepage_url="https://pulse.zerodha.com/",
        article_urls=("https://pulse.zerodha.com/",),
        tags=("news", "fundamental", "aggregator"),
        max_pages=1,
    ),
    NewsSourceProfile(
        key="livemint",
        name="Livemint",
        domains=("livemint.com",),
        homepage_url="https://www.livemint.com/",
        discovery_urls=("https://www.livemint.com/rss",),
        feed_urls=(
            "https://www.livemint.com/rss/companiesRSS",
            "https://www.livemint.com/rss/newsRSS",
            "https://www.livemint.com/rss/marketsRSS",
            "https://www.livemint.com/rss/industryRSS",
            "https://www.livemint.com/rss/moneyRSS",
        ),
        article_urls=(
            "https://www.livemint.com/",
            "https://www.livemint.com/market",
            "https://www.livemint.com/companies",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="hindu_businessline",
        name="The Hindu BusinessLine",
        domains=("thehindubusinessline.com",),
        homepage_url="https://www.thehindubusinessline.com/",
        discovery_urls=("https://www.thehindubusinessline.com/",),
        feed_urls=("https://www.thehindubusinessline.com/markets/feeder/default.rss",),
        article_urls=(
            "https://www.thehindubusinessline.com/markets/",
            "https://www.thehindubusinessline.com/companies/",
            "https://www.thehindubusinessline.com/economy/",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="business_today",
        name="Business Today",
        domains=("businesstoday.in",),
        homepage_url="https://www.businesstoday.in/",
        discovery_urls=("https://www.businesstoday.in/",),
        article_urls=(
            "https://www.businesstoday.in/markets",
            "https://www.businesstoday.in/latest",
            "https://www.businesstoday.in/industry",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="moneycontrol",
        name="Moneycontrol",
        domains=("moneycontrol.com",),
        homepage_url="https://www.moneycontrol.com/",
        discovery_urls=("https://www.moneycontrol.com/news/",),
        feed_urls=(
            "https://www.moneycontrol.com/rss/latestnews.xml",
            "https://www.moneycontrol.com/rss/business.xml",
            "https://www.moneycontrol.com/rss/results.xml",
            "https://www.moneycontrol.com/rss/economy.xml",
        ),
        article_urls=(
            "https://www.moneycontrol.com/news/business/",
            "https://www.moneycontrol.com/news/markets/",
            "https://www.moneycontrol.com/news/india/",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="ndtv_profit",
        name="NDTV Profit",
        domains=("ndtvprofit.com", "ndtv.com"),
        homepage_url="https://www.ndtvprofit.com/",
        # NDTV Profit returns 403 to this environment for its RSS endpoint;
        # use public pages as the configured fallback and surface source errors.
        discovery_urls=(),
        article_urls=(
            "https://www.ndtvprofit.com/",
            "https://www.ndtvprofit.com/business",
            "https://www.ndtvprofit.com/markets",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="bloomberg_india",
        name="Bloomberg India",
        domains=("bloomberg.com",),
        homepage_url="https://www.bloomberg.com/asia",
        discovery_urls=(),
        article_urls=(
            "https://www.bloomberg.com/asia",
            "https://www.bloomberg.com/markets",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="screener_india",
        name="Screener India",
        domains=("screener.in",),
        homepage_url="https://www.screener.in/",
        discovery_urls=(),
        article_urls=("https://www.screener.in/",),
        source_type="company-research",
        tags=("research", "filings", "companies"),
    ),
    NewsSourceProfile(
        key="cnbc_tv18",
        name="CNBC-TV18",
        domains=("cnbctv18.com",),
        homepage_url="https://www.cnbctv18.com/",
        discovery_urls=(),
        article_urls=(
            "https://www.cnbctv18.com/market/",
            "https://www.cnbctv18.com/business/",
            "https://www.cnbctv18.com/companies/",
        ),
        tags=("news", "markets", "companies"),
    ),
    NewsSourceProfile(
        key="rbi",
        name="Reserve Bank of India (RBI)",
        domains=("rbi.org.in",),
        homepage_url="https://www.rbi.org.in/",
        discovery_urls=("https://www.rbi.org.in/Scripts/rss.aspx",),
        tags=("regulatory", "central-bank"),
    ),
    NewsSourceProfile(
        key="sebi",
        name="Securities and Exchange Board of India (SEBI)",
        domains=("sebi.gov.in",),
        homepage_url="https://www.sebi.gov.in/",
        feed_urls=("https://www.sebi.gov.in/sebirss.xml",),
        discovery_urls=("https://www.sebi.gov.in/rss.html",),
        tags=("regulatory", "securities"),
    ),
    NewsSourceProfile(
        key="nse_india",
        name="National Stock Exchange (NSE India)",
        domains=("nseindia.com",),
        homepage_url="https://www.nseindia.com/",
        discovery_urls=("https://www.nseindia.com/static/rss-feed",),
        article_urls=("https://www.nseindia.com/companies-listing/corporate-filings-announcements",),
        source_type="exchange-rss",
        tags=("exchange", "filings", "corporate-actions"),
    ),
    NewsSourceProfile(
        key="bse_india",
        name="Bombay Stock Exchange (BSE India)",
        domains=("bseindia.com",),
        homepage_url="https://www.bseindia.com/",
        discovery_urls=(),
        article_urls=("https://www.bseindia.com/corporates/ann.html",),
        source_type="exchange-configurable",
        tags=("exchange", "filings", "corporate-announcements"),
    ),
    # Additional fundamental-news sources requested for NewsAlert. These public
    # pages are deliberately bounded; access-restricted pages remain marked in the
    # source-status report rather than being silently treated as successful.
    NewsSourceProfile(
        key="reuters_india",
        name="Reuters India",
        domains=("reuters.com",),
        homepage_url="https://www.reuters.com/world/india/",
        article_urls=("https://www.reuters.com/world/india/", "https://www.reuters.com/business/"),
        tags=("news", "companies", "fundamental"),
        max_pages=2,
    ),
    NewsSourceProfile(
        key="mca_india",
        name="Ministry of Corporate Affairs (MCA)",
        domains=("mca.gov.in",),
        homepage_url="https://www.mca.gov.in/content/mca/global/en/home.html",
        article_urls=("https://www.mca.gov.in/content/mca/global/en/home.html",),
        tags=("official", "corporate-filings", "regulatory"),
        max_pages=2,
    ),
    NewsSourceProfile(
        key="cci_india",
        name="Competition Commission of India (CCI)",
        domains=("cci.gov.in",),
        homepage_url="https://www.cci.gov.in/public/media-gallery",
        article_urls=("https://www.cci.gov.in/public/media-gallery", "https://www.cci.gov.in/public/"),
        tags=("official", "competition", "regulatory"),
        max_pages=2,
    ),
    NewsSourceProfile(
        key="nclt_india",
        name="National Company Law Tribunal (NCLT)",
        domains=("nclt.gov.in",),
        homepage_url="https://nclt.gov.in/",
        article_urls=("https://nclt.gov.in/",),
        tags=("official", "insolvency", "corporate-law"),
        max_pages=2,
    ),
    NewsSourceProfile(
        key="cdsco_india",
        name="Central Drugs Standard Control Organisation (CDSCO)",
        domains=("cdsco.gov.in",),
        homepage_url="https://cdsco.gov.in/opencms/opencms/en/Home/",
        article_urls=(
            "https://cdsco.gov.in/opencms/opencms/en/Latest-Public-Notices/",
            "https://cdsco.gov.in/opencms/opencms/en/Latest-Circulars/",
            "https://cdsco.gov.in/opencms/opencms/en/Latest-Alerts/",
        ),
        tags=("official", "pharma", "healthcare", "regulatory"),
        max_pages=3,
    ),
    NewsSourceProfile(
        key="dgft_india",
        name="Directorate General of Foreign Trade (DGFT)",
        domains=("dgft.gov.in",),
        homepage_url="https://www.dgft.gov.in/CP/",
        article_urls=("https://www.dgft.gov.in/CP/",),
        tags=("official", "trade-policy", "import-export"),
        max_pages=2,
    ),
    NewsSourceProfile(
        key="crisil_india",
        name="CRISIL",
        domains=("crisil.com",),
        homepage_url="https://www.crisil.com/en/home/press-release.html",
        article_urls=(
            "https://www.crisil.com/en/home/press-release.html",
            "https://www.crisil.com/en/home/investors/corporate-announcements.html",
        ),
        tags=("ratings", "credit", "research", "fundamental"),
        max_pages=2,
    ),
    NewsSourceProfile(
        key="icra_india",
        name="ICRA",
        domains=("icra.in",),
        homepage_url="https://www.icra.in/",
        article_urls=("https://www.icra.in/",),
        tags=("ratings", "credit", "research", "fundamental"),
        max_pages=2,
    ),
)


def get_source(key: str) -> NewsSourceProfile:
    for source in NEWS_SOURCES:
        if source.key == key:
            return source
    raise KeyError(key)


def enabled_sources() -> list[NewsSourceProfile]:
    return [s for s in NEWS_SOURCES if s.enabled]