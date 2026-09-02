"""Transformation layer.

Turns raw API payloads into tidy, typed Pandas DataFrames and derives the
analytical metrics consumed by the dashboard:

* ``volume_to_mcap_ratio`` - liquidity relative to size
* ``market_share_pct``     - share of the tracked universe's market cap
* ``momentum_bucket``      - categorical label from the 24h price change
* ``ath_distance_pct``     - distance from all-time high
* indexed (base-100) 7-day price trend series
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd

from .config import Settings

log = logging.getLogger("etl.transform")

#: Canonical column map: raw CoinGecko field -> warehouse column.
MARKET_COLUMNS = {
    "id": "coin_id",
    "symbol": "symbol",
    "name": "name",
    "image": "image_url",
    "current_price": "current_price",
    "market_cap": "market_cap",
    "market_cap_rank": "market_cap_rank",
    "fully_diluted_valuation": "fully_diluted_valuation",
    "total_volume": "total_volume",
    "high_24h": "high_24h",
    "low_24h": "low_24h",
    "price_change_percentage_24h": "price_change_pct_24h",
    "market_cap_change_percentage_24h": "market_cap_change_pct_24h",
    "price_change_percentage_7d_in_currency": "price_change_pct_7d",
    "circulating_supply": "circulating_supply",
    "total_supply": "total_supply",
    "max_supply": "max_supply",
    "ath": "ath",
    "ath_change_percentage": "ath_distance_pct",
    "ath_date": "ath_date",
    "last_updated": "last_updated",
}

NUMERIC_COLUMNS = [
    "current_price", "market_cap", "fully_diluted_valuation", "total_volume",
    "high_24h", "low_24h", "price_change_pct_24h", "market_cap_change_pct_24h",
    "price_change_pct_7d", "circulating_supply", "total_supply", "max_supply",
    "ath", "ath_distance_pct",
]

CRITICAL_COLUMNS = [
    "coin_id", "symbol", "name", "current_price", "market_cap", "market_cap_rank",
]


def _momentum_bucket(pct: float) -> str:
    """Map a 24h percentage change to a human-readable momentum label."""
    if pd.isna(pct):
        return "n/a"
    if pct >= 3.0:
        return "strong gain"
    if pct >= 0.5:
        return "gain"
    if pct > -0.5:
        return "flat"
    if pct > -3.0:
        return "loss"
    return "strong loss"


def transform_markets(raw_markets: list[dict], cfg: Settings) -> pd.DataFrame:
    """Normalise the /coins/markets payload into the canonical schema."""
    df = pd.DataFrame(raw_markets)
    if df.empty:
        raise ValueError("markets payload is empty - nothing to transform")

    known = {col: name for col, name in MARKET_COLUMNS.items() if col in df.columns}
    df = df[list(known)].rename(columns=known)

    # --- typing ------------------------------------------------------------
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    df["market_cap_rank"] = pd.to_numeric(df["market_cap_rank"], errors="coerce")
    df["symbol"] = df["symbol"].astype(str).str.lower()
    df["coin_id"] = df["coin_id"].astype(str)
    df["last_updated"] = pd.to_datetime(df["last_updated"], utc=True, errors="coerce")
    df["ath_date"] = pd.to_datetime(df.get("ath_date"), utc=True, errors="coerce")

    # --- cleaning ----------------------------------------------------------
    before = len(df)
    df = df.drop_duplicates(subset="coin_id", keep="first")
    df = df.sort_values("market_cap_rank", na_position="last").reset_index(drop=True)
    if len(df) != before:
        log.warning("dropped %d duplicate coin rows", before - len(df))

    # --- derived metrics -----------------------------------------------------
    stable = {symbol.lower() for symbol in cfg.stablecoin_symbols}
    df["is_stablecoin"] = df["symbol"].isin(stable)

    total_mcap = df["market_cap"].sum()
    df["market_share_pct"] = (
        df["market_cap"] / total_mcap * 100.0 if total_mcap else float("nan")
    )
    df["volume_to_mcap_ratio"] = (
        (df["total_volume"] / df["market_cap"]).where(df["market_cap"] > 0)
    )
    df["momentum_bucket"] = df["price_change_pct_24h"].map(_momentum_bucket)

    log.info(
        "transformed %d coins (%d stablecoins) | total mcap $%.3fT",
        len(df), int(df["is_stablecoin"].sum()), total_mcap / 1e12,
    )
    return df


def transform_price_history(market_charts: dict[str, dict]) -> pd.DataFrame:
    """Long-format daily price history: coin_id, ts (UTC midnight), price.

    CoinGecko appends one intraday "current" point to the daily series; we
    floor every timestamp to its UTC day and keep the latest observation per
    (coin, day), so the series is strictly one point per day.
    """
    records: list[dict] = []
    for coin_id, payload in market_charts.items():
        for ts_ms, price in payload.get("prices", []):
            records.append(
                {
                    "coin_id": coin_id,
                    "ts": pd.to_datetime(ts_ms, unit="ms", utc=True),
                    "price": float(price),
                }
            )
    df = pd.DataFrame(records, columns=["coin_id", "ts", "price"])
    if not df.empty:
        df["ts"] = df["ts"].dt.floor("D")
        df = (
            df.sort_values(["coin_id", "ts"], kind="stable")
            .drop_duplicates(subset=["coin_id", "ts"], keep="last")
            .reset_index(drop=True)
        )
    log.info("transformed %d daily price points for %d coins",
             len(df), len(market_charts))
    return df


def transform_fear_greed(raw_rows: list[dict]) -> pd.DataFrame:
    """Daily sentiment observations: snapshot_date, value, classification."""
    records = []
    for row in raw_rows:
        try:
            ts = int(row["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        records.append(
            {
                "snapshot_date": datetime.fromtimestamp(ts, tz=UTC).date().isoformat(),
                "value": int(row["value"]),
                "classification": str(row.get("value_classification", "n/a")),
            }
        )
    df = pd.DataFrame(records, columns=["snapshot_date", "value", "classification"])
    if not df.empty:
        df = (
            df.drop_duplicates(subset="snapshot_date", keep="first")
            .sort_values("snapshot_date")
            .reset_index(drop=True)
        )
    log.info("transformed %d Fear & Greed observations", len(df))
    return df


def build_trend_series(df_history: pd.DataFrame, names: pd.DataFrame) -> dict:
    """Base-100 indexed 7-day series per coin for the dashboard line chart.

    Returns ``{"dates": [...iso...], "series": [{coin_id, symbol, name, index}]}``.
    """
    if df_history.empty:
        return {"dates": [], "series": []}

    lookup = {
        row["coin_id"]: (row["symbol"], row["name"])
        for row in names.to_dict("records")
    }
    dates = sorted(df_history["ts"].unique())
    series = []
    for coin_id, group in df_history.groupby("coin_id", sort=False):
        group = group.set_index("ts").reindex(dates)
        base = group["price"].dropna()
        if base.empty or base.iloc[0] == 0:
            continue
        indexed = (group["price"] / base.iloc[0] * 100.0).round(2)
        symbol, name = lookup.get(coin_id, (coin_id, coin_id))
        series.append(
            {
                "coin_id": coin_id,
                "symbol": str(symbol).upper(),
                "name": name,
                "index": [None if pd.isna(value) else value for value in indexed],
            }
        )
    return {
        "dates": [pd.Timestamp(ts).isoformat() for ts in dates],
        "series": series,
    }
