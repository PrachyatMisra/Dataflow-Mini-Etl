"""Pipeline configuration.

All settings can be overridden through environment variables (optionally via a
``.env`` file in the working directory), so the same code runs unchanged in a
local venv, inside Docker Compose, or in GitHub Actions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_TRUTHY = {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader (no third-party dependency).

    Existing environment variables always win, matching python-dotenv's
    default behaviour.
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration for one pipeline run."""

    # --- Sources -----------------------------------------------------------
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    fear_greed_base_url: str = "https://api.alternative.me"
    vs_currency: str = "usd"
    market_limit: int = 25          # coins pulled from /coins/markets
    trend_coin_count: int = 5       # coins with 7-day price history
    trend_days: int = 7
    fear_greed_days: int = 30
    stablecoin_symbols: list[str] = field(
        default_factory=lambda: ["usdt", "usdc", "dai", "usds", "usde", "usd1", "tusd"]
    )

    # --- HTTP behaviour ------------------------------------------------------
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 4
    http_backoff_seconds: float = 1.5

    # --- Warehousing ---------------------------------------------------------
    database_url: str = ""          # postgres://... -> PostgreSQL backend
    sqlite_path: str = "data/dataflow.db"

    # --- Outputs -------------------------------------------------------------
    artifact_dir: str = "artifacts"
    export_dir: str = "docs/data"   # consumed by the GitHub Pages dashboard

    # --- Data-quality gate -----------------------------------------------------
    min_rows: int = 10
    freshness_max_hours: float = 24.0
    null_tolerance_pct: float = 5.0  # tolerated % of nulls in non-critical cols

    # --- Misc ------------------------------------------------------------------
    log_level: str = "INFO"
    source_mode: str = "api"        # "api" | "fixture"
    fixture_dir: str = "tests/fixtures"


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment (and optional ``.env``)."""
    _load_dotenv(REPO_ROOT / ".env")
    _load_dotenv(Path.cwd() / ".env")

    return Settings(
        coingecko_base_url=_env_str("COINGECKO_BASE_URL", Settings.coingecko_base_url),
        fear_greed_base_url=_env_str("FEAR_GREED_BASE_URL", Settings.fear_greed_base_url),
        vs_currency=_env_str("VS_CURRENCY", "usd").lower(),
        market_limit=_env_int("MARKET_LIMIT", 25),
        trend_coin_count=_env_int("TREND_COIN_COUNT", 5),
        trend_days=_env_int("TREND_DAYS", 7),
        fear_greed_days=_env_int("FEAR_GREED_DAYS", 30),
        stablecoin_symbols=_env_list(
            "STABLECOIN_SYMBOLS",
            ["usdt", "usdc", "dai", "usds", "usde", "usd1", "tusd"],
        ),
        http_timeout_seconds=_env_float("HTTP_TIMEOUT_SECONDS", 20.0),
        http_max_retries=_env_int("HTTP_MAX_RETRIES", 4),
        http_backoff_seconds=_env_float("HTTP_BACKOFF_SECONDS", 1.5),
        database_url=_env_str("DATABASE_URL", ""),
        sqlite_path=_env_str("SQLITE_PATH", "data/dataflow.db"),
        artifact_dir=_env_str("ARTIFACT_DIR", "artifacts"),
        export_dir=_env_str("EXPORT_DIR", "docs/data"),
        min_rows=_env_int("MIN_ROWS", 10),
        freshness_max_hours=_env_float("FRESHNESS_MAX_HOURS", 24.0),
        null_tolerance_pct=_env_float("NULL_TOLERANCE_PCT", 5.0),
        log_level=_env_str("LOG_LEVEL", "INFO"),
        source_mode=_env_str("SOURCE_MODE", "api"),
        fixture_dir=_env_str("FIXTURE_DIR", "tests/fixtures"),
    )
