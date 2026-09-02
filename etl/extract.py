"""Extraction layer.

Pulls raw payloads from public REST APIs with retry/backoff, or replays them
from committed JSON fixtures (used by CI, tests and offline demos).

Sources
-------
* CoinGecko ``/coins/markets``            -> cross-sectional market snapshot
* CoinGecko ``/coins/{id}/market_chart``  -> 7-day daily price history
* alternative.me Fear & Greed Index       -> 30-day market sentiment
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import Settings

log = logging.getLogger("etl.extract")


class ExtractError(RuntimeError):
    """Raised when a source cannot be fetched after all retries."""


@dataclass
class RawData:
    """Unprocessed payloads produced by the extraction stage."""

    markets: list[dict[str, Any]]
    market_charts: dict[str, dict[str, Any]]
    fear_greed: list[dict[str, Any]]
    fetched_at: datetime
    source: str  # "live-api" | "fixture"
    calls_made: int = field(default=0)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def http_get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
    max_retries: int = 4,
    backoff_seconds: float = 1.5,
) -> Any:
    """GET ``url`` and decode JSON, retrying with exponential backoff + jitter."""
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code == 429:
                # Rate limited: honour Retry-After when provided, then back off.
                retry_after = response.headers.get("Retry-After")
                base = float(retry_after) if retry_after else backoff_seconds * (2 ** (attempt - 1))
                delay = base + random.uniform(0, 0.5)
                log.warning("rate-limited (429) on %s - waiting %.1fs", url, delay)
                time.sleep(delay)
                last_error = requests.HTTPError(f"429 Too Many Requests on {url}")
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt == max_retries:
                break
            delay = backoff_seconds * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            log.warning(
                "attempt %d/%d failed for %s (%s) - retrying in %.1fs",
                attempt, max_retries, url, exc, delay,
            )
            time.sleep(delay)
    raise ExtractError(f"Failed to fetch {url}: {last_error}") from last_error


# --------------------------------------------------------------------------- #
# Live API extractors
# --------------------------------------------------------------------------- #
def fetch_markets(cfg: Settings, session: requests.Session) -> list[dict[str, Any]]:
    """Top-N coins by market cap from CoinGecko."""
    url = f"{cfg.coingecko_base_url}/coins/markets"
    params = {
        "vs_currency": cfg.vs_currency,
        "order": "market_cap_desc",
        "per_page": cfg.market_limit,
        "page": 1,
        "sparkline": "false",
        "price_change_percentage": "24h,7d",
    }
    data = http_get_json(
        session, url, params=params,
        timeout=cfg.http_timeout_seconds,
        max_retries=cfg.http_max_retries,
        backoff_seconds=cfg.http_backoff_seconds,
    )
    if not isinstance(data, list):
        raise ExtractError(f"Unexpected /coins/markets payload: {type(data)!r}")
    log.info("fetched %d coins from %s", len(data), url)
    return data


def fetch_market_chart(
    cfg: Settings, session: requests.Session, coin_id: str
) -> dict[str, Any]:
    """Daily price history for one coin (last ``trend_days`` days)."""
    url = f"{cfg.coingecko_base_url}/coins/{coin_id}/market_chart"
    params = {"vs_currency": cfg.vs_currency, "days": cfg.trend_days, "interval": "daily"}
    data = http_get_json(
        session, url, params=params,
        timeout=cfg.http_timeout_seconds,
        max_retries=cfg.http_max_retries,
        backoff_seconds=cfg.http_backoff_seconds,
    )
    if not isinstance(data, dict) or "prices" not in data:
        raise ExtractError(f"Unexpected market_chart payload for {coin_id}")
    return data


def fetch_fear_greed(cfg: Settings, session: requests.Session) -> list[dict[str, Any]]:
    """Daily Fear & Greed Index history."""
    url = f"{cfg.fear_greed_base_url}/fng/"
    params = {"limit": cfg.fear_greed_days, "format": "json"}
    data = http_get_json(
        session, url, params=params,
        timeout=cfg.http_timeout_seconds,
        max_retries=cfg.http_max_retries,
        backoff_seconds=cfg.http_backoff_seconds,
    )
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ExtractError("Unexpected Fear & Greed payload")
    log.info("fetched %d Fear & Greed observations", len(rows))
    return rows


def _select_trend_coins(markets: list[dict[str, Any]], cfg: Settings) -> list[str]:
    """Top-N coin ids, skipping stablecoins (flat lines are uninteresting)."""
    stable = {symbol.lower() for symbol in cfg.stablecoin_symbols}
    selected: list[str] = []
    for coin in sorted(markets, key=lambda c: c.get("market_cap_rank") or 10_000):
        if str(coin.get("symbol", "")).lower() in stable:
            continue
        selected.append(str(coin["id"]))
        if len(selected) >= cfg.trend_coin_count:
            break
    return selected


def extract_from_api(cfg: Settings) -> RawData:
    """Run all live extractions in one session (connection reuse)."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "dataflow-mini-etl/1.0"})
    calls = 0

    markets = fetch_markets(cfg, session)
    calls += 1

    charts: dict[str, dict[str, Any]] = {}
    for coin_id in _select_trend_coins(markets, cfg):
        try:
            charts[coin_id] = fetch_market_chart(cfg, session, coin_id)
            calls += 1
        except ExtractError as exc:
            # Trend history is non-critical: log and continue.
            log.warning("skipping market chart for %s: %s", coin_id, exc)

    fear_greed = fetch_fear_greed(cfg, session)
    calls += 1

    return RawData(
        markets=markets,
        market_charts=charts,
        fear_greed=fear_greed,
        fetched_at=datetime.now(timezone.utc),
        source="live-api",
        calls_made=calls,
    )


# --------------------------------------------------------------------------- #
# Fixture replay (offline / CI)
# --------------------------------------------------------------------------- #
def _read_fixture(fixture_dir: Path, name: str) -> Any:
    path = fixture_dir / name
    if not path.is_file():
        raise ExtractError(f"Missing fixture: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def extract_from_fixtures(cfg: Settings) -> RawData:
    """Replay committed fixtures - identical code path, zero network."""
    fixture_dir = Path(cfg.fixture_dir)
    markets = _read_fixture(fixture_dir, "coins_markets_raw.json")
    fear_greed_payload = _read_fixture(fixture_dir, "fear_greed_raw.json")
    fear_greed = fear_greed_payload.get("data", []) if isinstance(fear_greed_payload, dict) else []

    charts: dict[str, dict[str, Any]] = {}
    for coin_id in _select_trend_coins(markets, cfg):
        try:
            charts[coin_id] = _read_fixture(fixture_dir, f"market_chart_{coin_id}.json")
        except ExtractError as exc:
            log.warning("skipping fixture market chart for %s: %s", coin_id, exc)

    log.info("replayed fixtures from %s (%d coins)", fixture_dir, len(markets))
    return RawData(
        markets=markets,
        market_charts=charts,
        fear_greed=fear_greed,
        fetched_at=datetime.now(timezone.utc),
        source="fixture",
        calls_made=0,
    )


def extract_all(cfg: Settings) -> RawData:
    """Dispatch to live-API or fixture extraction based on ``cfg.source_mode``."""
    if cfg.source_mode == "fixture":
        return extract_from_fixtures(cfg)
    return extract_from_api(cfg)
