"""
config.py - Central, environment-driven configuration for HLRN.

Every tunable lives here so that behaviour (thresholds, limits, TTLs, budgets)
can be changed from `.env` without touching code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Tuple

try:  # python-dotenv is optional; plain environment variables also work
    from dotenv import load_dotenv

    # override=True: the project's .env ALWAYS wins over stale shell / system variables
    # (a leftover `GEMINI_MODEL` exported in your terminal used to silently beat .env).
    load_dotenv(override=True)
except ImportError:  # pragma: no cover
    pass


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _model_name(name: str, default: str) -> str:
    """
    Clean a model id typed into .env: strips quotes, inline `# comments`, whitespace and the
    `models/` prefix that the model-listing API shows (the API call itself wants the bare id).
    """
    raw = os.getenv(name, "") or default
    raw = raw.split(" #")[0].strip().strip("\"'").strip()
    if raw.lower().startswith("models/"):
        raw = raw[len("models/"):]
    return raw or default


def _model_name_value(raw: str) -> str:
    raw = raw.strip().strip("\"'").strip()
    return raw[len("models/"):] if raw.lower().startswith("models/") else raw


def _int(name: str, default: int) -> int:
    try:
        return int(_str(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return _str(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str = "") -> Tuple[str, ...]:
    return tuple(x.strip() for x in _str(name, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # ---- Gemini / Layer 2 -------------------------------------------------
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_models: Tuple[str, ...]   # tried in order if the primary model is rejected
    gemini_auto_discover: bool                # ask the API which models this key can really use
    # --- quota / rate-limit resilience (free-tier friendly) ---
    allow_ungrounded_fallback: bool           # on search-quota 429, answer WITHOUT search (never a verdict)
    grounding_cooldown_sec: int               # how long live search stays paused after a quota error
    llm_min_interval_sec: float               # client-side pacing between Gemini calls (free tier: ~6)
    llm_max_concurrency: int                  # simultaneous Gemini calls
    llm_max_retry_wait_sec: int               # never sleep longer than this on a 429 'retry in Ns'
    degraded_ttl_sec: int                     # how long a no-search answer is reused before re-checking
    gemini_timeout_sec: int
    llm_max_output_tokens: int
    llm_max_retries: int

    # ---- Storage ----------------------------------------------------------
    db_path: str

    # ---- Guardrails -------------------------------------------------------
    confidence_threshold: float          # below this -> never "Fake"/"True"
    high_risk_confidence: float          # stricter bar for sensitive categories
    high_risk_categories: Tuple[str, ...]
    require_trusted_source: bool         # definitive verdict needs official/fact-check source
    resolve_grounding_urls: bool
    extra_trusted_domains: Tuple[str, ...]

    # ---- Layer 1 ----------------------------------------------------------
    fuzzy_threshold: float
    fuzzy_min_tokens: int
    cache_ttl_sec: int
    cache_max_items: int
    ai_result_ttl_days: float
    pending_ttl_hours: float

    # ---- Input / media limits --------------------------------------------
    max_input_chars: int
    max_upload_mb: int
    max_keyframes: int
    frame_max_side: int
    max_video_seconds: int
    max_image_pixels: int
    media_neardup_hamming: int

    # ---- Anti-abuse -------------------------------------------------------
    rate_limit_requests: int
    rate_limit_window_sec: int
    llm_calls_per_client_hour: int
    global_llm_calls_per_day: int
    max_workers: int
    max_queue: int

    # ---- Privacy / admin --------------------------------------------------
    audit_retention_days: int
    audit_hash_salt: str
    admin_password: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            gemini_api_key=_str("GEMINI_API_KEY"),
            gemini_model=_model_name("GEMINI_MODEL", ""),      # empty = auto-discover
            gemini_fallback_models=tuple(
                m for m in (_model_name_value(x) for x in _list("GEMINI_FALLBACK_MODELS")) if m),
            gemini_auto_discover=_bool("GEMINI_AUTO_DISCOVER", True),
            allow_ungrounded_fallback=_bool("ALLOW_UNGROUNDED_FALLBACK", True),
            grounding_cooldown_sec=_int("GROUNDING_COOLDOWN_SEC", 600),
            llm_min_interval_sec=_float("LLM_MIN_INTERVAL_SEC", 0.0),
            llm_max_concurrency=_int("LLM_MAX_CONCURRENCY", 2),
            llm_max_retry_wait_sec=_int("LLM_MAX_RETRY_WAIT_SEC", 15),
            degraded_ttl_sec=_int("DEGRADED_TTL_SEC", 600),
            gemini_timeout_sec=_int("GEMINI_TIMEOUT_SEC", 60),
            llm_max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", 1200),
            llm_max_retries=_int("LLM_MAX_RETRIES", 3),
            db_path=_str("HLRN_DB_PATH", "data/hlrn.db"),
            confidence_threshold=_float("CONFIDENCE_THRESHOLD", 75.0),
            high_risk_confidence=_float("HIGH_RISK_CONFIDENCE", 90.0),
            high_risk_categories=_list(
                "HIGH_RISK_CATEGORIES", "Communal / Religious,Politics / Elections"
            ),
            require_trusted_source=_bool("REQUIRE_TRUSTED_SOURCE", True),
            resolve_grounding_urls=_bool("RESOLVE_GROUNDING_URLS", True),
            extra_trusted_domains=_list("EXTRA_TRUSTED_DOMAINS"),
            fuzzy_threshold=_float("FUZZY_THRESHOLD", 0.82),
            fuzzy_min_tokens=_int("FUZZY_MIN_TOKENS", 5),
            cache_ttl_sec=_int("CACHE_TTL_SEC", 3600),
            cache_max_items=_int("CACHE_MAX_ITEMS", 5000),
            ai_result_ttl_days=_float("AI_RESULT_TTL_DAYS", 7),
            pending_ttl_hours=_float("PENDING_TTL_HOURS", 12),
            max_input_chars=_int("MAX_INPUT_CHARS", 4000),
            max_upload_mb=_int("MAX_UPLOAD_MB", 40),
            max_keyframes=_int("MAX_KEYFRAMES", 8),
            frame_max_side=_int("FRAME_MAX_SIDE", 768),
            max_video_seconds=_int("MAX_VIDEO_SECONDS", 600),
            max_image_pixels=_int("MAX_IMAGE_PIXELS", 60_000_000),
            media_neardup_hamming=_int("MEDIA_NEARDUP_HAMMING", 8),
            rate_limit_requests=_int("RATE_LIMIT_REQUESTS", 10),
            rate_limit_window_sec=_int("RATE_LIMIT_WINDOW_SEC", 60),
            llm_calls_per_client_hour=_int("LLM_CALLS_PER_CLIENT_HOUR", 20),
            global_llm_calls_per_day=_int("GLOBAL_LLM_CALLS_PER_DAY", 2000),
            max_workers=_int("MAX_WORKERS", 4),
            max_queue=_int("MAX_QUEUE", 20),
            audit_retention_days=_int("AUDIT_RETENTION_DAYS", 30),
            audit_hash_salt=_str("AUDIT_HASH_SALT", "change-me"),
            admin_password=_str("ADMIN_PASSWORD"),
        )


settings = Settings.from_env()
