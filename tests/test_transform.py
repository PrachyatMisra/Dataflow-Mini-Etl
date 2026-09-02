"""Unit tests for the transformation layer."""

from __future__ import annotations

import pandas as pd
import pytest

from etl.config import Settings
from etl.transform import (
    build_trend_series,
    transform_fear_greed,
    transform_markets,
    transform_price_history,
)


class TestTransformMarkets:
    def test_canonical_schema(self, raw_markets):
        df = transform_markets(raw_markets, Settings())
        for col in ("coin_id", "symbol", "name", "current_price", "market_cap",
                    "market_cap_rank", "volume_to_mcap_ratio", "market_share_pct",
                    "momentum_bucket", "is_stablecoin", "last_updated"):
            assert col in df.columns, f"missing column {col}"
        assert len(df) == 25

    def test_numeric_typing(self, raw_markets):
        df = transform_markets(raw_markets, Settings())
        assert pd.api.types.is_float_dtype(df["current_price"])
        assert pd.api.types.is_float_dtype(df["market_cap"])
        assert pd.api.types.is_datetime64_any_dtype(df["last_updated"])

    def test_stablecoin_flagging(self, raw_markets):
        df = transform_markets(raw_markets, Settings())
        stable = set(df.loc[df["is_stablecoin"], "symbol"])
        assert {"usdt", "usdc", "dai"} <= stable
        assert "btc" not in stable

    def test_market_share_sums_to_100(self, raw_markets):
        df = transform_markets(raw_markets, Settings())
        assert df["market_share_pct"].sum() == pytest.approx(100.0, rel=1e-6)

    def test_sorted_by_rank(self, raw_markets):
        df = transform_markets(raw_markets, Settings())
        ranks = df["market_cap_rank"].dropna().tolist()
        assert ranks == sorted(ranks)

    def test_duplicates_are_removed(self, raw_markets):
        doubled = raw_markets + raw_markets[:3]
        df = transform_markets(doubled, Settings())
        assert len(df) == 25
        assert df["coin_id"].is_unique


class TestMomentum:
    def _df_with_change(self, pct):
        return transform_markets(
            [{
                "id": "coin", "symbol": "cn", "name": "Coin", "image": None,
                "current_price": 1.0, "market_cap": 100, "market_cap_rank": 1,
                "fully_diluted_valuation": 100, "total_volume": 10,
                "high_24h": 1.1, "low_24h": 0.9,
                "price_change_percentage_24h": pct,
                "market_cap_change_percentage_24h": pct,
                "price_change_percentage_7d_in_currency": pct,
                "circulating_supply": 100, "total_supply": 100, "max_supply": None,
                "ath": 2.0, "ath_change_percentage": -50.0, "ath_date": "2025-01-01T00:00:00Z",
                "last_updated": "2026-09-02T10:00:00Z",
            }],
            Settings(),
        )

    @pytest.mark.parametrize(
        ("pct", "bucket"),
        [(12.0, "strong gain"), (2.0, "gain"), (0.1, "flat"),
         (-2.0, "loss"), (-8.0, "strong loss"), (None, "n/a")],
    )
    def test_buckets(self, pct, bucket):
        assert self._df_with_change(pct)["momentum_bucket"].iloc[0] == bucket


class TestPriceHistory:
    def test_long_format(self):
        charts = {
            "bitcoin": {"prices": [[1788307200000, 100.0], [1788393600000, 101.0]]},
            "ethereum": {"prices": [[1788307200000, 10.0]]},
        }
        df = transform_price_history(charts)
        assert list(df.columns) == ["coin_id", "ts", "price"]
        assert len(df) == 3
        assert pd.api.types.is_datetime64_any_dtype(df["ts"])

    def test_trend_series_base_100(self, raw_markets):
        charts = {"bitcoin": {"prices": [[1788307200000, 50.0], [1788393600000, 60.0]]}}
        df_history = transform_price_history(charts)
        names = transform_markets(raw_markets, Settings())[["coin_id", "symbol", "name"]]
        payload = build_trend_series(df_history, names)
        series = payload["series"][0]
        assert series["symbol"] == "BTC"
        assert series["index"][0] == 100.0
        assert series["index"][1] == 120.0
        assert len(payload["dates"]) == 2


class TestFearGreed:
    def test_transform(self, raw_fear_greed):
        df = transform_fear_greed(raw_fear_greed)
        assert list(df.columns) == ["snapshot_date", "value", "classification"]
        assert len(df) == 30
        assert df["value"].between(0, 100).all()
        assert df["snapshot_date"].is_monotonic_increasing
        assert df["snapshot_date"].is_unique

    def test_malformed_rows_skipped(self):
        df = transform_fear_greed([
            {"value": "50", "value_classification": "Neutral", "timestamp": "1788307200"},
            {"value": "oops", "timestamp": "not-a-number"},
            {"no_timestamp": True},
        ])
        assert len(df) == 1
