"""End-to-end test: fixture replay -> SQLite -> artifacts (no network)."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

from etl.config import REPO_ROOT, Settings, load_settings
from etl.pipeline import run_pipeline


def _offline_settings(tmp_path: Path) -> Settings:
    settings = load_settings()
    return dataclasses.replace(
        settings,
        source_mode="fixture",
        fixture_dir=str(REPO_ROOT / "tests" / "fixtures"),
        sqlite_path=str(tmp_path / "test.db"),
        artifact_dir=str(tmp_path / "artifacts"),
        export_dir=str(tmp_path / "export"),
        log_level="WARNING",
    )


class TestOfflinePipeline:
    def test_full_run(self, tmp_path):
        cfg = _offline_settings(tmp_path)
        result = run_pipeline(cfg, backend_choice="sqlite")

        assert result.status == "success"
        assert result.exit_code == 0
        assert result.rows == {"coins": 25, "history": 35, "fear_greed": 30}
        assert [s["name"] for s in result.stages] == [
            "extract", "transform", "validate", "load", "publish",
        ]
        assert all(s["status"] == "ok" for s in result.stages)

    def test_warehouse_contents(self, tmp_path):
        cfg = _offline_settings(tmp_path)
        run_pipeline(cfg, backend_choice="sqlite")

        conn = sqlite3.connect(tmp_path / "test.db")
        try:
            coins = conn.execute("SELECT COUNT(*) FROM coins_snapshot").fetchone()[0]
            history = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
            fng = conn.execute("SELECT COUNT(*) FROM fear_greed").fetchone()[0]
            run_log = conn.execute(
                "SELECT status, rows_coins FROM etl_run_log"
            ).fetchall()
            btc = conn.execute(
                "SELECT symbol, momentum_bucket FROM coins_snapshot WHERE coin_id='bitcoin'"
            ).fetchone()
        finally:
            conn.close()

        assert coins == 25
        assert history == 35  # 5 coins x 7 daily points (intraday point folded into its day)
        assert fng == 30
        assert run_log and run_log[0][0] == "success" and run_log[0][1] == 25
        assert btc[0] == "btc" and btc[1] in {"loss", "strong loss", "flat"}

    def test_idempotent_rerun(self, tmp_path):
        cfg = _offline_settings(tmp_path)
        first = run_pipeline(cfg, backend_choice="sqlite")
        second = run_pipeline(cfg, backend_choice="sqlite")
        assert first.status == second.status == "success"

        conn = sqlite3.connect(tmp_path / "test.db")
        try:
            # Price history and sentiment upsert on natural keys -> no growth.
            history = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
            fng = conn.execute("SELECT COUNT(*) FROM fear_greed").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM etl_run_log").fetchone()[0]
        finally:
            conn.close()
        assert history == 35
        assert fng == 30
        assert runs == 2  # one snapshot row-set per run, keyed by run_id

    def test_dashboard_payload(self, tmp_path):
        cfg = _offline_settings(tmp_path)
        run_pipeline(cfg, backend_choice="sqlite")

        payload_path = tmp_path / "export" / "latest.json"
        assert payload_path.is_file()
        payload = json.loads(payload_path.read_text(encoding="utf-8"))

        assert payload["meta"]["source"] == "fixture"
        assert payload["meta"]["coins_tracked"] == 25
        assert payload["meta"]["author"] == "Prachyat Misra"
        assert len(payload["coins"]) == 25
        assert payload["coins"][0]["id"] == "bitcoin"
        assert payload["kpis"]["total_market_cap"] > 0
        assert payload["kpis"]["fear_greed"]["value"] == 63
        assert payload["trends"]["series"], "expected trend series"
        assert len(payload["dominance"]) == 6  # top-5 + Others
        assert payload["quality"]["summary"]["failures"] == 0
        assert (tmp_path / "artifacts" / "coins_snapshot.csv").is_file()
