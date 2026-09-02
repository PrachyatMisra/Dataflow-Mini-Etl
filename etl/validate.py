"""Validation layer - a configurable data-quality gate.

Every check returns one of three statuses:

* ``pass`` - constraint holds
* ``warn`` - constraint violated within tolerance (pipeline continues)
* ``fail`` - constraint violated beyond tolerance (pipeline aborts)

The full check report is persisted with each run (``etl_run_log`` table and
the dashboard JSON), so data quality is observable, not implicit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .config import Settings
from .transform import CRITICAL_COLUMNS, MARKET_COLUMNS

log = logging.getLogger("etl.validate")

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass
class Check:
    """Outcome of a single validation check."""

    name: str
    status: str
    message: str
    rows_checked: int = 0
    rows_flagged: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "rows_checked": self.rows_checked,
            "rows_flagged": self.rows_flagged,
        }


@dataclass
class ValidationReport:
    """Aggregate result of all checks for one pipeline run."""

    checks: list[Check] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        return {
            "total": len(self.checks),
            "passed": sum(c.status == PASS for c in self.checks),
            "warnings": sum(c.status == WARN for c in self.checks),
            "failures": sum(c.status == FAIL for c in self.checks),
        }

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    def to_dict(self) -> dict:
        return {"summary": self.summary, "checks": [c.to_dict() for c in self.checks]}


class ValidationError(RuntimeError):
    """Raised when the quality gate hard-fails."""


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
def check_schema(df: pd.DataFrame) -> Check:
    """All canonical columns must be present after transformation."""
    expected = set(MARKET_COLUMNS.values())
    missing = sorted(expected - set(df.columns))
    if missing:
        return Check("schema.columns", FAIL, f"missing columns: {missing}", len(df), len(missing))
    return Check("schema.columns", PASS, f"all {len(expected)} canonical columns present", len(df))


def check_critical_nulls(df: pd.DataFrame) -> Check:
    """No nulls allowed in identity/price/market-cap columns."""
    cols = [c for c in CRITICAL_COLUMNS if c in df.columns]
    null_mask = df[cols].isna().any(axis=1)
    flagged = int(null_mask.sum())
    if flagged:
        offenders = df.loc[null_mask, "coin_id"].head(5).tolist()
        return Check("nulls.critical", FAIL,
                     f"{flagged} rows with nulls in critical cols (e.g. {offenders})",
                     len(df), flagged)
    return Check("nulls.critical", PASS, "no nulls in critical columns", len(df))


def check_secondary_nulls(df: pd.DataFrame, tolerance_pct: float) -> Check:
    """Limited null tolerance in secondary numeric columns (e.g. missing 24h stats)."""
    cols = ["price_change_pct_24h", "price_change_pct_7d", "high_24h", "low_24h"]
    cols = [c for c in cols if c in df.columns]
    if not cols:
        return Check("nulls.secondary", WARN, "no secondary columns to check", len(df))
    total_cells = len(df) * len(cols)
    flagged = int(df[cols].isna().sum().sum())
    pct = flagged / total_cells * 100.0 if total_cells else 0.0
    status = PASS if pct <= tolerance_pct else FAIL
    return Check("nulls.secondary", status,
                 f"{flagged}/{total_cells} cells null ({pct:.2f}%, tolerance {tolerance_pct}%)",
                 total_cells, flagged)


def check_ranges(df: pd.DataFrame) -> Check:
    """Sanity ranges: positive prices, non-negative caps, bounded percentages."""
    violations: list[str] = []
    n = len(df)

    bad_price = df["current_price"].notna() & (df["current_price"] <= 0)
    if bad_price.any():
        violations.append(f"{int(bad_price.sum())} non-positive prices")

    bad_mcap = df["market_cap"].notna() & (df["market_cap"] < 0)
    if bad_mcap.any():
        violations.append(f"{int(bad_mcap.sum())} negative market caps")

    bad_rank = df["market_cap_rank"].notna() & (df["market_cap_rank"] < 1)
    if bad_rank.any():
        violations.append(f"{int(bad_rank.sum())} invalid ranks")

    pct = df["price_change_pct_24h"]
    bad_pct = pct.notna() & ((pct < -100) | (pct > 1000))
    if bad_pct.any():
        violations.append(f"{int(bad_pct.sum())} implausible 24h percentages")

    if violations:
        return Check("ranges.values", FAIL, "; ".join(violations), n, sum(1 for _ in violations))
    return Check("ranges.values", PASS, "prices, caps, ranks and percentages within sane ranges", n)


def check_unique_ids(df: pd.DataFrame) -> Check:
    """Coin ids must be unique within a snapshot."""
    dupes = df["coin_id"].duplicated().sum()
    if dupes:
        return Check("keys.unique_coin_id", FAIL, f"{int(dupes)} duplicate coin ids", len(df), int(dupes))
    return Check("keys.unique_coin_id", PASS, "coin ids unique", len(df))


def check_row_count(df: pd.DataFrame, min_rows: int) -> Check:
    """The snapshot must contain a meaningful number of rows."""
    if len(df) < min_rows:
        return Check("volume.row_count", FAIL,
                     f"only {len(df)} rows (minimum {min_rows})", len(df))
    return Check("volume.row_count", PASS, f"{len(df)} rows >= minimum {min_rows}", len(df))


def check_freshness(df: pd.DataFrame, max_age_hours: float, strict: bool) -> Check:
    """Source timestamps should be recent (relaxed for fixture replays)."""
    latest = df["last_updated"].max()
    if pd.isna(latest):
        return Check("freshness.source", FAIL, "no parseable last_updated values", len(df), len(df))
    age_hours = (datetime.now(timezone.utc) - latest.to_pydatetime()).total_seconds() / 3600.0
    if age_hours <= max_age_hours:
        return Check("freshness.source", PASS,
                     f"newest source timestamp {age_hours:.1f}h old", len(df))
    status = FAIL if strict else WARN
    return Check("freshness.source", status,
                 f"newest source timestamp {age_hours:.1f}h old (max {max_age_hours}h)",
                 len(df), len(df))


def check_fear_greed(df: pd.DataFrame) -> Check:
    """Sentiment values must be integers within 0..100."""
    if df.empty:
        return Check("fear_greed.rows", WARN, "no Fear & Greed rows loaded", 0)
    out_of_range = ~df["value"].between(0, 100)
    if out_of_range.any():
        return Check("fear_greed.range", FAIL,
                     f"{int(out_of_range.sum())} values outside 0..100", len(df), int(out_of_range.sum()))
    return Check("fear_greed.range", PASS, f"{len(df)} sentiment values within 0..100", len(df))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_validation(
    df_markets: pd.DataFrame,
    df_fear_greed: pd.DataFrame,
    cfg: Settings,
) -> ValidationReport:
    """Execute the full quality gate and return an actionable report."""
    report = ValidationReport()
    strict = cfg.source_mode == "api"

    report.checks.append(check_schema(df_markets))
    report.checks.append(check_critical_nulls(df_markets))
    report.checks.append(check_secondary_nulls(df_markets, cfg.null_tolerance_pct))
    report.checks.append(check_ranges(df_markets))
    report.checks.append(check_unique_ids(df_markets))
    report.checks.append(check_row_count(df_markets, cfg.min_rows))
    report.checks.append(check_freshness(df_markets, cfg.freshness_max_hours, strict=strict))
    report.checks.append(check_fear_greed(df_fear_greed))

    for check in report.checks:
        log.log(
            logging.ERROR if check.status == FAIL else logging.INFO,
            "[%s] %-20s %s", check.status.upper(), check.name, check.message,
        )
    return report
