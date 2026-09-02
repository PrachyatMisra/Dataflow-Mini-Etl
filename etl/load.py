"""Load layer.

* **PostgreSQL** (primary, via ``DATABASE_URL``) - used by Docker Compose.
* **SQLite** (zero-dependency fallback) - local runs, tests and CI.
* **Artifact export** - CSV snapshot + the JSON payload that powers the
  GitHub Pages dashboard (``docs/data/latest.json``).

Writes are idempotent: every table has a natural key and rows are upserted,
so re-running the pipeline never duplicates data.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .config import Settings

log = logging.getLogger("etl.load")

ISO_DT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(value: Any) -> str | None:
    """Render timestamps (pandas or datetime) as UTC ISO-8601 strings."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.tz_localize("UTC").tz_localize(None).strftime(ISO_DT) if value.tzinfo is None \
            else value.tz_convert("UTC").strftime(ISO_DT)
    if isinstance(value, datetime):
        return value.astimezone().strftime(ISO_DT)
    return str(value)


def _float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _int(value: Any) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def _text(value: Any) -> str | None:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return str(value)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
DDL_COMMON = """
CREATE TABLE IF NOT EXISTS coins_snapshot (
    run_id                  TEXT NOT NULL,
    fetched_at              TEXT NOT NULL,
    coin_id                 TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    name                    TEXT NOT NULL,
    image_url               TEXT,
    market_cap_rank         INTEGER,
    current_price           REAL,
    market_cap              REAL,
    fully_diluted_valuation REAL,
    total_volume            REAL,
    high_24h                REAL,
    low_24h                 REAL,
    price_change_pct_24h    REAL,
    market_cap_change_pct_24h REAL,
    price_change_pct_7d     REAL,
    circulating_supply      REAL,
    total_supply            REAL,
    max_supply              REAL,
    ath                     REAL,
    ath_distance_pct        REAL,
    ath_date                TEXT,
    last_updated            TEXT,
    is_stablecoin           INTEGER NOT NULL DEFAULT 0,
    market_share_pct        REAL,
    volume_to_mcap_ratio    REAL,
    momentum_bucket         TEXT,
    PRIMARY KEY (run_id, coin_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    coin_id TEXT NOT NULL,
    ts      TEXT NOT NULL,
    price   REAL NOT NULL,
    PRIMARY KEY (coin_id, ts)
);

CREATE TABLE IF NOT EXISTS fear_greed (
    snapshot_date  TEXT PRIMARY KEY,
    value          INTEGER NOT NULL,
    classification TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etl_run_log (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,
    source          TEXT,
    rows_coins      INTEGER,
    rows_history    INTEGER,
    rows_fear_greed INTEGER,
    stages_json     TEXT,
    validation_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_coins_snapshot_fetched ON coins_snapshot (fetched_at);
CREATE INDEX IF NOT EXISTS idx_price_history_ts ON price_history (ts);
"""


class DatabaseBackend(ABC):
    """Common interface over the supported warehouses."""

    @abstractmethod
    def execute_many(self, sql: str, rows: list[tuple]) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def ensure_schema(self) -> None:
        self.execute_many_batch(DDL_COMMON)

    @abstractmethod
    def execute_many_batch(self, script: str) -> None: ...


class SqliteBackend(DatabaseBackend):
    """SQLite backend - zero-setup local warehouse."""

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        log.info("sqlite backend ready at %s", path)

    def execute_many(self, sql: str, rows: list[tuple]) -> None:
        with self.conn:
            self.conn.executemany(sql, rows)

    def execute_many_batch(self, script: str) -> None:
        with self.conn:
            self.conn.executescript(script)

    def close(self) -> None:
        self.conn.close()


class PostgresBackend(DatabaseBackend):
    """PostgreSQL backend - the production path (Docker Compose)."""

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "psycopg2 is required for the PostgreSQL backend. "
                "Install it with: pip install psycopg2-binary"
            ) from exc
        self.conn = psycopg2.connect(database_url)
        self.conn.autocommit = False
        log.info("postgres backend connected")

    def execute_many(self, sql: str, rows: list[tuple]) -> None:
        with self.conn, self.conn.cursor() as cur:
            cur.executemany(sql, rows)

    def execute_many_batch(self, script: str) -> None:
        with self.conn, self.conn.cursor() as cur:
            cur.execute(script)

    def close(self) -> None:
        self.conn.close()


def build_backend(cfg: Settings, backend_choice: str) -> DatabaseBackend | None:
    """Resolve the requested backend: postgres | sqlite | none."""
    if backend_choice == "none":
        log.info("backend=none: skipping warehouse load (artifacts only)")
        return None
    if backend_choice == "postgres" or (backend_choice == "auto" and cfg.database_url):
        if not cfg.database_url:
            raise RuntimeError("postgres backend requested but DATABASE_URL is not set")
        return PostgresBackend(cfg.database_url)
    return SqliteBackend(cfg.sqlite_path)


# --------------------------------------------------------------------------- #
# Upserts
# --------------------------------------------------------------------------- #
_COIN_COLS = [
    "coin_id", "symbol", "name", "image_url", "market_cap_rank", "current_price",
    "market_cap", "fully_diluted_valuation", "total_volume", "high_24h", "low_24h",
    "price_change_pct_24h", "market_cap_change_pct_24h", "price_change_pct_7d",
    "circulating_supply", "total_supply", "max_supply", "ath", "ath_distance_pct",
    "ath_date", "last_updated", "is_stablecoin", "market_share_pct",
    "volume_to_mcap_ratio", "momentum_bucket",
]

_UPSERT_COINS = f"""
INSERT INTO coins_snapshot (run_id, fetched_at, {", ".join(_COIN_COLS)})
VALUES ({", ".join(["?"] * (len(_COIN_COLS) + 2))})
ON CONFLICT (run_id, coin_id) DO UPDATE SET
    fetched_at = excluded.fetched_at,
    current_price = excluded.current_price,
    market_cap = excluded.market_cap,
    total_volume = excluded.total_volume,
    price_change_pct_24h = excluded.price_change_pct_24h,
    price_change_pct_7d = excluded.price_change_pct_7d,
    market_share_pct = excluded.market_share_pct,
    volume_to_mcap_ratio = excluded.volume_to_mcap_ratio,
    momentum_bucket = excluded.momentum_bucket
"""

_UPSERT_HISTORY = """
INSERT INTO price_history (coin_id, ts, price) VALUES (?, ?, ?)
ON CONFLICT (coin_id, ts) DO UPDATE SET price = excluded.price
"""

_UPSERT_FNG = """
INSERT INTO fear_greed (snapshot_date, value, classification) VALUES (?, ?, ?)
ON CONFLICT (snapshot_date) DO UPDATE SET
    value = excluded.value, classification = excluded.classification
"""

_UPSERT_RUN_LOG = """
INSERT INTO etl_run_log (run_id, started_at, finished_at, status, source,
                         rows_coins, rows_history, rows_fear_greed,
                         stages_json, validation_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (run_id) DO UPDATE SET
    finished_at = excluded.finished_at, status = excluded.status,
    rows_coins = excluded.rows_coins, rows_history = excluded.rows_history,
    rows_fear_greed = excluded.rows_fear_greed,
    stages_json = excluded.stages_json, validation_json = excluded.validation_json
"""


def load_coins(backend: DatabaseBackend, run_id: str, fetched_at: datetime,
               df: pd.DataFrame) -> int:
    text_cols = {"coin_id", "symbol", "name", "image_url", "momentum_bucket"}
    rows = []
    for record in df.to_dict("records"):
        rows.append(
            (run_id, _iso(fetched_at))
            + tuple(
                _int(record[c]) if c == "market_cap_rank"
                else int(bool(record[c])) if c == "is_stablecoin"
                else _iso(record[c]) if c in ("ath_date", "last_updated")
                else _float(record[c]) if c not in text_cols
                else _text(record[c])
                for c in _COIN_COLS
            )
        )
    backend.execute_many(_UPSERT_COINS.replace("?", "%s") if isinstance(backend, PostgresBackend)
                         else _UPSERT_COINS, rows)
    log.info("upserted %d coin rows into coins_snapshot", len(rows))
    return len(rows)


def load_price_history(backend: DatabaseBackend, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [(r["coin_id"], _iso(r["ts"]), _float(r["price"])) for r in df.to_dict("records")]
    sql = _UPSERT_HISTORY.replace("?", "%s") if isinstance(backend, PostgresBackend) else _UPSERT_HISTORY
    backend.execute_many(sql, rows)
    log.info("upserted %d price-history points", len(rows))
    return len(rows)


def load_fear_greed(backend: DatabaseBackend, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [(r["snapshot_date"], _int(r["value"]), r["classification"]) for r in df.to_dict("records")]
    sql = _UPSERT_FNG.replace("?", "%s") if isinstance(backend, PostgresBackend) else _UPSERT_FNG
    backend.execute_many(sql, rows)
    log.info("upserted %d Fear & Greed rows", len(rows))
    return len(rows)


def write_run_log(backend: DatabaseBackend, run_id: str, started_at: datetime,
                  finished_at: datetime, status: str, source: str,
                  rows_coins: int, rows_history: int, rows_fng: int,
                  stages: list[dict], validation: dict) -> None:
    params = (
        run_id, _iso(started_at), _iso(finished_at), status, source,
        rows_coins, rows_history, rows_fng,
        json.dumps(stages), json.dumps(validation),
    )
    sql = _UPSERT_RUN_LOG.replace("?", "%s") if isinstance(backend, PostgresBackend) else _UPSERT_RUN_LOG
    backend.execute_many(sql, [params])


# --------------------------------------------------------------------------- #
# Artifact export (CSV + dashboard JSON)
# --------------------------------------------------------------------------- #
def _round_price(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6) if abs(value) < 1 else round(value, 2)


def build_dashboard_payload(
    *,
    run_id: str,
    fetched_at: datetime,
    source: str,
    cfg: Settings,
    df_markets: pd.DataFrame,
    df_history: pd.DataFrame,
    df_fear_greed: pd.DataFrame,
    trend_payload: dict,
    validation: dict,
    stages: list[dict],
) -> dict:
    """Assemble the single JSON document consumed by the dashboard."""
    coins: list[dict] = []
    for r in df_markets.to_dict("records"):
        coins.append(
            {
                "rank": _int(r["market_cap_rank"]),
                "id": r["coin_id"],
                "symbol": str(r["symbol"]).upper(),
                "name": r["name"],
                "image": r.get("image_url"),
                "price": _round_price(_float(r["current_price"])),
                "market_cap": _int(r["market_cap"]),
                "volume_24h": _int(r["total_volume"]),
                "change_24h": _float(r["price_change_pct_24h"]),
                "change_7d": _float(r["price_change_pct_7d"]),
                "mcap_share_pct": round(_float(r["market_share_pct"]) or 0.0, 2),
                "vol_mcap": round(_float(r["volume_to_mcap_ratio"]) or 0.0, 4),
                "momentum": r["momentum_bucket"],
                "stablecoin": bool(r["is_stablecoin"]),
            }
        )

    total_mcap = int(df_markets["market_cap"].sum())
    total_volume = int(df_markets["total_volume"].sum())
    changes = df_markets["price_change_pct_24h"].dropna()
    movers = [c for c in coins if c["change_24h"] is not None]
    gainers = sorted(movers, key=lambda c: c["change_24h"], reverse=True)[:5]
    losers = sorted(movers, key=lambda c: c["change_24h"])[:5]

    top5 = coins[:5]
    dominance = [{"label": c["symbol"], "value": c["market_cap"]} for c in top5]
    rest = sum(c["market_cap"] or 0 for c in coins[5:])
    if rest:
        dominance.append({"label": "Others", "value": rest})

    btc = next((c for c in coins if c["id"] == "bitcoin"), None)
    fng_history = [
        {"date": r["snapshot_date"], "value": int(r["value"]), "classification": r["classification"]}
        for r in df_fear_greed.to_dict("records")
    ]

    return {
        "meta": {
            "run_id": run_id,
            "fetched_at": _iso(fetched_at),
            "source": source,
            "vs_currency": cfg.vs_currency.upper(),
            "coins_tracked": len(coins),
            "generator": "dataflow-mini-etl",
            "author": "Prachyat Misra",
            "schema_version": 1,
        },
        "kpis": {
            "total_market_cap": total_mcap,
            "total_volume_24h": total_volume,
            "avg_change_24h": round(float(changes.mean()), 2) if len(changes) else None,
            "bitcoin": {
                "price": btc["price"] if btc else None,
                "change_24h": btc["change_24h"] if btc else None,
                "dominance_pct": btc["mcap_share_pct"] if btc else None,
            },
            "fear_greed": fng_history[-1] if fng_history else None,
        },
        "coins": coins,
        "movers": {"gainers": gainers, "losers": losers},
        "dominance": dominance,
        "trends": trend_payload,
        "fear_greed": {"history": fng_history},
        "quality": validation,
        "stages": stages,
    }


def export_artifacts(
    cfg: Settings,
    payload: dict,
    df_markets: pd.DataFrame,
) -> dict[str, str]:
    """Write the CSV snapshot and dashboard JSON; return written paths."""
    written: dict[str, str] = {}

    artifact_dir = Path(cfg.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    export_dir = Path(cfg.export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    csv_path = artifact_dir / "coins_snapshot.csv"
    export_cols = [c for c in df_markets.columns if c != "image_url"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=export_cols, extrasaction="ignore")
        writer.writeheader()
        for record in df_markets.to_dict("records"):
            writer.writerow({k: ("" if pd.isna(v) else v) for k, v in record.items()})
    written["csv"] = str(csv_path)

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    for target in (artifact_dir / "latest.json", export_dir / "latest.json"):
        target.write_text(payload_json, encoding="utf-8")
        written[str(target)] = str(target)

    log.info("exported artifacts: %s", ", ".join(sorted(set(written.values()))))
    return written
