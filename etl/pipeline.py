"""Pipeline orchestrator.

Runs the stages sequentially - extract, transform, validate, load, publish -
times each one, records them in ``etl_run_log``, and converts failures into
explicit exit codes:

* ``0`` success
* ``1`` pipeline failure (extraction/load error)
* ``2`` validation gate failure (data quality)
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import __version__
from .config import Settings
from .extract import RawData, extract_all
from .load import (
    build_backend,
    build_dashboard_payload,
    export_artifacts,
    load_coins,
    load_fear_greed,
    load_price_history,
    write_run_log,
)
from .transform import (
    build_trend_series,
    transform_fear_greed,
    transform_markets,
    transform_price_history,
)
from .validate import ValidationError, run_validation

log = logging.getLogger("etl.pipeline")


def new_run_id(now: datetime) -> str:
    return f"run-{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


@dataclass
class StageTimer:
    """Collects stage names, durations and statuses for observability."""

    stages: list[dict] = field(default_factory=list)

    def run(self, name: str, fn):
        started = time.perf_counter()
        try:
            result = fn()
        except Exception:
            self.stages.append(
                {"name": name, "duration_s": round(time.perf_counter() - started, 3),
                 "status": "failed"}
            )
            raise
        self.stages.append(
            {"name": name, "duration_s": round(time.perf_counter() - started, 3),
             "status": "ok"}
        )
        return result

    def as_list(self) -> list[dict]:
        return list(self.stages)


@dataclass
class PipelineResult:
    run_id: str
    status: str
    exit_code: int
    started_at: datetime
    finished_at: datetime
    rows: dict
    stages: list[dict]
    artifacts: dict[str, str]


def run_pipeline(cfg: Settings, backend_choice: str = "auto") -> PipelineResult:
    """Execute the full ETL run and return a machine-readable result."""
    started_at = datetime.now(UTC)
    run_id = new_run_id(started_at)
    timers = StageTimer()
    backend = None
    report = None
    rows = {"coins": 0, "history": 0, "fear_greed": 0}
    artifacts: dict[str, str] = {}
    status = "success"
    exit_code = 0

    log.info("=== DataFlow Mini ETL %s | run %s | source=%s ===",
             __version__, run_id, cfg.source_mode)

    try:
        # 1. EXTRACT --------------------------------------------------------
        raw: RawData = timers.run("extract", lambda: extract_all(cfg))

        # 2. TRANSFORM --------------------------------------------------------
        def _transform():
            return (
                transform_markets(raw.markets, cfg),
                transform_price_history(raw.market_charts),
                transform_fear_greed(raw.fear_greed),
            )

        df_markets, df_history, df_fear_greed = timers.run("transform", _transform)

        # 3. VALIDATE ---------------------------------------------------------
        report = timers.run("validate", lambda: run_validation(df_markets, df_fear_greed, cfg))
        if report.failed:
            status = "failed-validation"
            exit_code = 2
            raise ValidationError(
                f"{report.summary['failures']} validation check(s) failed - aborting load"
            )

        # 4. LOAD -------------------------------------------------------------
        def _load():
            nonlocal backend
            backend = build_backend(cfg, backend_choice)
            if backend is not None:
                backend.ensure_schema()
                rows["coins"] = load_coins(backend, run_id, raw.fetched_at, df_markets)
                rows["history"] = load_price_history(backend, df_history)
                rows["fear_greed"] = load_fear_greed(backend, df_fear_greed)
            return rows

        timers.run("load", _load)

        # 5. PUBLISH ------------------------------------------------------------
        def _publish():
            trend_payload = build_trend_series(df_history, df_markets[["coin_id", "symbol", "name"]])
            # Reserve the publish stage's slot so the exported artifact carries
            # complete observability data, including its own timing.
            stages_snapshot = timers.as_list() + [
                {"name": "publish", "duration_s": 0.0, "status": "ok"}
            ]
            payload = build_dashboard_payload(
                run_id=run_id,
                fetched_at=raw.fetched_at,
                source=raw.source,
                cfg=cfg,
                df_markets=df_markets,
                df_history=df_history,
                df_fear_greed=df_fear_greed,
                trend_payload=trend_payload,
                validation=report.to_dict(),
                stages=stages_snapshot,
            )
            started_publish = time.perf_counter()
            written = export_artifacts(cfg, payload, df_markets)
            stages_snapshot[-1]["duration_s"] = round(time.perf_counter() - started_publish, 3)
            export_artifacts(cfg, payload, df_markets)  # rewrite with final timing
            return written

        artifacts = timers.run("publish", _publish)

    except ValidationError as exc:
        log.error("pipeline aborted: %s", exc)
    except Exception:
        status = "failed"
        exit_code = 1
        log.exception("pipeline failed")
    finally:
        finished_at = datetime.now(UTC)
        report_dict = report.to_dict() if report is not None else {"summary": {}, "checks": []}
        if backend is not None:
            try:
                write_run_log(
                    backend, run_id, started_at, finished_at, status, cfg.source_mode,
                    rows["coins"], rows["history"], rows["fear_greed"],
                    timers.as_list(), report_dict,
                )
            except Exception:  # pragma: no cover - logging must never mask the result
                log.exception("failed to write etl_run_log")
            backend.close()

    duration = (finished_at - started_at).total_seconds()
    log.info(
        "=== %s | run %s finished in %.2fs | coins=%d history=%d fng=%d ===",
        status.upper(), run_id, duration, rows["coins"], rows["history"], rows["fear_greed"],
    )
    return PipelineResult(
        run_id=run_id,
        status=status,
        exit_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        rows=rows,
        stages=timers.as_list(),
        artifacts=artifacts,
    )
