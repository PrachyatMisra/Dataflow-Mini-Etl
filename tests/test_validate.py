"""Unit tests for the validation (data-quality gate) layer."""

from __future__ import annotations

import pandas as pd

from etl.config import Settings
from etl.transform import transform_markets
from etl.validate import (
    FAIL,
    PASS,
    WARN,
    check_critical_nulls,
    check_fear_greed,
    check_freshness,
    check_ranges,
    check_row_count,
    check_secondary_nulls,
    check_unique_ids,
    run_validation,
)


def _clean_df(raw_markets):
    return transform_markets(raw_markets, Settings())


class TestIndividualChecks:
    def test_critical_nulls_pass(self, raw_markets):
        assert check_critical_nulls(_clean_df(raw_markets)).status == PASS

    def test_critical_nulls_fail(self, raw_markets):
        df = _clean_df(raw_markets)
        df.loc[0, "current_price"] = None
        check = check_critical_nulls(df)
        assert check.status == FAIL
        assert check.rows_flagged == 1

    def test_secondary_nulls_within_tolerance(self, raw_markets):
        df = _clean_df(raw_markets)
        # The real fixture contains one coin without 24h stats -> tolerated.
        check = check_secondary_nulls(df, tolerance_pct=5.0)
        assert check.status in (PASS, WARN)

    def test_secondary_nulls_beyond_tolerance(self, raw_markets):
        df = _clean_df(raw_markets)
        df["price_change_pct_24h"] = None
        assert check_secondary_nulls(df, tolerance_pct=5.0).status == FAIL

    def test_ranges_fail_on_negative_price(self, raw_markets):
        df = _clean_df(raw_markets)
        df.loc[0, "current_price"] = -5.0
        assert check_ranges(df).status == FAIL

    def test_ranges_fail_on_implausible_change(self, raw_markets):
        df = _clean_df(raw_markets)
        df.loc[0, "price_change_pct_24h"] = -150.0
        assert check_ranges(df).status == FAIL

    def test_unique_ids(self, raw_markets):
        df = _clean_df(raw_markets)
        assert check_unique_ids(df).status == PASS
        duped = pd.concat([df, df.iloc[[0]]], ignore_index=True)
        assert check_unique_ids(duped).status == FAIL

    def test_row_count(self, raw_markets):
        df = _clean_df(raw_markets)
        assert check_row_count(df, min_rows=10).status == PASS
        assert check_row_count(df, min_rows=99).status == FAIL

    def test_freshness_strict_vs_relaxed(self, raw_markets):
        df = _clean_df(raw_markets).copy()
        old = pd.Timestamp("2020-01-01", tz="UTC")
        df["last_updated"] = old
        assert check_freshness(df, max_age_hours=24, strict=True).status == FAIL
        assert check_freshness(df, max_age_hours=24, strict=False).status == WARN

    def test_fear_greed_range(self):
        good = pd.DataFrame({"value": [10, 55, 90], "classification": ["Fear", "Neutral", "Greed"]})
        assert check_fear_greed(good).status == PASS
        bad = pd.DataFrame({"value": [101], "classification": ["?"]})
        assert check_fear_greed(bad).status == FAIL
        assert check_fear_greed(pd.DataFrame()).status == WARN


class TestFullGate:
    def test_clean_fixture_passes_gate(self, raw_markets, raw_fear_greed):
        from etl.transform import transform_fear_greed

        cfg = Settings(source_mode="fixture")
        df_markets = transform_markets(raw_markets, cfg)
        df_fng = transform_fear_greed(raw_fear_greed)
        report = run_validation(df_markets, df_fng, cfg)
        assert not report.failed
        assert report.summary["total"] == 8
        assert report.summary["passed"] >= 7

    def test_gate_fails_on_corrupted_data(self, raw_markets, raw_fear_greed):
        from etl.transform import transform_fear_greed

        cfg = Settings(source_mode="fixture")
        df_markets = transform_markets(raw_markets, cfg)
        df_markets.loc[0, "market_cap"] = None  # critical null -> hard fail
        report = run_validation(df_markets, transform_fear_greed(raw_fear_greed), cfg)
        assert report.failed
        failed_names = [c.name for c in report.checks if c.status == FAIL]
        assert "nulls.critical" in failed_names
