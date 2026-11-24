from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "investment-intelligence"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./investment.db"

    jwt_secret_key: str = Field(
        default="CHANGE_ME_TO_A_64_CHAR_RANDOM_SECRET",
        min_length=32,
        validation_alias=AliasChoices("JWT_SECRET_KEY", "JWT_SECRET"),
    )
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 720
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str | None = None
    frontend_origin: str = "http://localhost:5173,http://127.0.0.1:5173"
    max_upload_bytes: int = 200_000_000

    news_feed_urls: str = ""
    news_source_registry_enabled: bool = True
    news_scrape_enabled: bool = True
    news_scrape_interval_seconds: int = 14400
    # Bound publisher fetches so a blocked/slow source can never hold a scheduler
    # connection open indefinitely. 0 used to mean unlimited and was a major source
    # of long-running background work.
    news_source_timeout_seconds: int = 20
    news_max_pages_per_source: int = 3
    background_collectors_start_delay_seconds: int = Field(default=5, ge=0, le=300)
    # Run scheduled collectors/event consumers in a separate worker process.
    # The web API should stay focused on HTTP request/response work.
    # Legacy flag retained for compatibility. Scheduled collectors are always worker-owned.
    background_workers_enabled: bool = False
    # One unified background cycle. The worker runs every enabled pipeline once
    # immediately after startup, then repeats on this interval.
    master_pipeline_enabled: bool = True
    master_pipeline_interval_seconds: int = Field(default=14400, ge=300, le=86400)
    master_pipeline_startup_immediate: bool = True
    master_pipeline_max_concurrent: int = Field(default=1, ge=1, le=4)
    # A stage timeout is intentionally NOT used to cancel the stage. It is only a
    # diagnostic threshold; individual network/database/AI operations own their
    # actual deadlines.
    master_pipeline_stage_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    pipeline_slow_warning_seconds: int = Field(default=120, ge=30, le=3600)
    news_source_budget_seconds: int = Field(default=60, ge=20, le=300)
    worker_redis_consumers_enabled: bool = False
    redis_status_enabled: bool = True
    # Per-stage deadlines. External sources are isolated so a slow provider cannot
    # cancel the entire four-hour cycle.
    pipeline_coverage_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    pipeline_newsalert_timeout_seconds: int = Field(default=420, ge=60, le=1800)
    pipeline_regulatory_timeout_seconds: int = Field(default=1200, ge=60, le=3600)
    pipeline_capital_actions_timeout_seconds: int = Field(default=1200, ge=60, le=3600)
    pipeline_notifications_timeout_seconds: int = Field(default=180, ge=30, le=900)
    news_cycle_limit_per_feed: int = Field(default=20, ge=5, le=100)

    # NewsAlert v2: direct writes to news_alert_items, with no legacy Article projection.
    newsalert_rss_feeds: str = Field(default="", validation_alias=AliasChoices("NEWSALERT_RSS_FEEDS"))
    newsalert_youtube_channels: str = Field(default="", validation_alias=AliasChoices("NEWSALERT_YOUTUBE_CHANNELS"))
    newsalert_x_accounts: str = Field(default="", validation_alias=AliasChoices("NEWSALERT_X_ACCOUNTS"))
    newsalert_x_feed_url_template: str = Field(default="", validation_alias=AliasChoices("NEWSALERT_X_FEED_URL_TEMPLATE"))
    newsalert_triage_model: str = Field(default="openai/gpt-oss-120b", validation_alias=AliasChoices("NEWSALERT_TRIAGE_MODEL"))
    newsalert_score_model: str = Field(default="openai/gpt-oss-120b", validation_alias=AliasChoices("NEWSALERT_SCORE_MODEL"))
    newsalert_min_score: int = Field(default=55, ge=0, le=100, validation_alias=AliasChoices("NEWSALERT_MIN_SCORE"))
    newsalert_groq_retries: int = Field(default=3, ge=1, le=8, validation_alias=AliasChoices("NEWSALERT_GROQ_RETRIES"))
    newsalert_groq_timeout_seconds: int = Field(default=35, ge=5, le=180, validation_alias=AliasChoices("NEWSALERT_GROQ_TIMEOUT_SECONDS"))
    newsalert_groq_concurrency: int = Field(default=4, ge=1, le=12, validation_alias=AliasChoices("NEWSALERT_GROQ_CONCURRENCY"))
    newsalert_nse_enabled: bool = Field(default=True, validation_alias=AliasChoices("NEWSALERT_NSE_ENABLED"))
    newsalert_configured_match_terms: str = Field(default="", validation_alias=AliasChoices("NEWSALERT_MATCH_TERMS"))

    # ============================================================
    # REGULATORY DESK / NSE PUBLIC API
    # ============================================================
    # These settings control the active four-hour worker pipeline. NSE public
    # filing endpoints are authenticated by a short-lived web session/cookie;
    # no NSE API key is required by this transport.
    regulatory_enabled: bool = True
    capital_action_enabled: bool = True
    regulatory_auto_historical_backfill: bool = False
    regulatory_incremental_history_days: int = Field(default=7, ge=1, le=30)
    capital_action_incremental_history_days: int = Field(default=7, ge=1, le=30)
    regulatory_full_reconcile_interval_seconds: int = Field(default=604800, ge=3600, le=604800)
    regulatory_full_retry_interval_seconds: int = Field(default=14400, ge=3600, le=604800)

    nse_api_request_retries: int = Field(default=5, ge=1, le=10)
    nse_api_session_ttl_seconds: int = Field(default=360, ge=30, le=3600)
    nse_api_min_request_interval_seconds: float = Field(default=0.50, ge=0.0, le=5.0)
    nse_api_connect_timeout_seconds: int = Field(default=20, ge=5, le=120)
    nse_api_read_timeout_seconds: int = Field(default=90, ge=10, le=300)
    nse_api_write_timeout_seconds: int = Field(default=30, ge=5, le=120)
    nse_api_pool_timeout_seconds: int = Field(default=60, ge=5, le=180)

    worker_queue_poll_seconds: int = Field(default=10, ge=2, le=60)
    youtube_enabled: bool = True
    # The collector reads public Atom feeds and public YouTube pages, with an
    # unauthenticated Playwright fallback; it does not require a YouTube API key.
    # These legacy settings remain for compatibility; NewsAlert uses its fixed
    # nine-publisher allowlist and ignores custom channel additions.
    youtube_channel_ids: str = ""
    youtube_channel_names: str = ""
    # Presenter names used for one targeted YouTube search per presenter.
    # This avoids one search per Coverage company while still finding videos that
    # are not among the channel's latest upload-feed entries.
    youtube_presenter_names: str = "Nigel D'Souza"
    # Example production value for the current monitored anchor:
    # YOUTUBE_PRESENTER_NAMES=Nigel D\'Souza
    youtube_public_search_enabled: bool = True
    # X/Twitter collection is allowlisted and uses Playwright public-page scraping.
    # X_BEARER_TOKEN is intentionally not a Settings field and is not loaded/used.
    x_allowed_accounts: str = "unseenvalue,Nigel__DSouza,ResearchSOIC,JoshiEien,HemindraHazari,soicfinance"
    x_post_feed_urls: str = ""
    x_recent_days: int = Field(default=7, ge=1, le=30)
    x_public_fallback_enabled: bool = False
    bse_feed_url: str | None = None
    bse_api_url: str | None = None

    # ============================================================
    # SECTOR SCREENER SOURCE OVERRIDES
    # ============================================================
    market_solar_url: str = "https://solardcrportal.nise.res.in/Summary/index"
    market_commodities_url: str = "https://tradingeconomics.com/commodities"
    market_lpg_url: str = "https://ppac.gov.in/archives/reports"
    market_gpu_url: str = "https://www.silicondata.com/products/silicon-index"
    market_battery_url: str = "https://www.mercomindia.com/research/report"

    # ============================================================
    # REDIS ONLY
    # ============================================================
    # The four-hour batch worker does not require Redis for its core pipeline.
    # Enable it only when you explicitly need event-driven consumers.
    event_bus_backend: Literal["redis", "disabled"] = "disabled"
    redis_enabled: bool = True
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_stream_prefix: str = "investment"
    redis_connect_timeout_seconds: float = 5.0
    redis_socket_timeout_seconds: float = 10.0
    redis_consumer_block_ms: int = 5000
    redis_message_batch_size: int = 20
    redis_claim_idle_ms: int = 60000

    # Worker-built UI snapshots. Keep the last good snapshot available through
    # the full four-hour cycle plus a grace window when a worker cycle fails.
    snapshot_cache_enabled: bool = True
    snapshot_cache_ttl_seconds: int = Field(default=21600, ge=300, le=86400)
    snapshot_cache_memory_max_age_seconds: int = Field(default=86400, ge=300, le=172800)
    snapshot_cache_warm_after_regulatory: bool = True
    snapshot_cache_warm_after_capital: bool = True
    snapshot_cache_warm_dashboard: bool = True

    # Fast authentication cache used by main.py/auth.py during startup and
    # login. Defaults preserve the existing cache behavior.
    auth_cache_enabled: bool = True
    auth_cache_ttl_seconds: int = Field(default=21600, ge=300, le=86400)

    sftp_host: str | None = None
    sftp_port: int = 22
    sftp_username: str | None = None
    sftp_password: str | None = None

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_vision_model: str = "gemini-2.5-flash"
    ai_timeout_seconds: int = 120
    ai_retry_attempts: int = 3

    # GROQ — retained for other platform features (not used by Crisp Notes).
    groq_api_key: str | None = Field(default=None, validation_alias=AliasChoices("GROQ_API_KEY"))
    groq_model: str = Field(default="openai/gpt-oss-120b", validation_alias=AliasChoices("GROQ_MODEL"))
    groq_timeout_seconds: int = Field(default=300, ge=30, le=3600, validation_alias=AliasChoices("GROQ_TIMEOUT_SECONDS"))
    groq_retry_attempts: int = Field(default=3, ge=1, le=8, validation_alias=AliasChoices("GROQ_RETRY_ATTEMPTS"))
    groq_max_tokens: int = Field(default=600, ge=400, le=8000, validation_alias=AliasChoices("GROQ_MAX_TOKENS"))
    groq_chunk_chars: int = Field(default=3000, ge=1500, le=100000, validation_alias=AliasChoices("GROQ_CHUNK_CHARS"))
    groq_reduce_chars: int = Field(default=3000, ge=1500, le=100000, validation_alias=AliasChoices("GROQ_REDUCE_CHARS"))

    # ============================================================
    # CRISP / DEEPGRAM
    # ============================================================
    # Transport timeout (seconds) for the synchronous /v1/listen batch request. It applies
    # to the full upload and the transcription wait, so it must cover the longest recording.
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3"
    # Deepgram batch diarization is versioned independently from the STT model.
    # "latest" is the supported auto-upgrading diarizer; "nova-3" here was invalid.
    deepgram_diarize_model: str = "latest"
    deepgram_timeout_seconds: int = Field(default=3600, ge=60, le=14400)
    deepgram_language: str = "multi"
    # Optional comma-separated language hints for Nova-3 multilingual requests.
    deepgram_language_hints: str = Field(default="", validation_alias=AliasChoices("DEEPGRAM_LANGUAGE_HINTS"))
    deepgram_keyterms: str = ""

    # Crisp summaries and transcript chat run through the local Ollama service.
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434",
        validation_alias=AliasChoices("OLLAMA_BASE_URL"),
    )
    ollama_model: str = Field(
        default="qwen3.5:4b",
        validation_alias=AliasChoices("OLLAMA_MODEL"),
    )
    ollama_timeout_seconds: int = Field(
        default=1800, ge=60, le=7200,
        validation_alias=AliasChoices("OLLAMA_TIMEOUT_SECONDS"),
    )
    ollama_num_ctx: int = Field(
        default=8192, ge=2048, le=131072,
        validation_alias=AliasChoices("OLLAMA_NUM_CTX"),
    )
    ollama_summary_max_tokens: int = Field(
        default=1800, ge=300, le=8192,
        validation_alias=AliasChoices("OLLAMA_SUMMARY_MAX_TOKENS"),
    )
    ollama_chat_max_tokens: int = Field(
        default=1000, ge=200, le=4096,
        validation_alias=AliasChoices("OLLAMA_CHAT_MAX_TOKENS"),
    )

    storage_type: Literal["local", "s3"] = "local"
    storage_path: str = "./data"
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_s3_bucket: str | None = None
    aws_s3_region: str = "ap-south-1"
    s3_endpoint_url: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()