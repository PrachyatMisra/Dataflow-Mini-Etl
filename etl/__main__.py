"""Command-line interface.

Examples
--------
Live run into PostgreSQL (Docker Compose):
    python -m etl run --backend postgres

Zero-dependency local run (SQLite + dashboard artifacts):
    python -m etl run

Offline replay of committed fixtures (CI / air-gapped demo):
    python -m etl run --source fixture --backend sqlite
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from . import __version__
from .config import load_settings
from .log import setup_logging
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dataflow-mini-etl",
        description="Mini ETL pipeline: public REST APIs -> Pandas -> PostgreSQL/SQLite + dashboard artifacts",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute one full ETL run")
    run.add_argument(
        "--source", choices=["api", "fixture"], default=None,
        help="extract from live APIs or replay committed fixtures (default: SOURCE_MODE env or api)",
    )
    run.add_argument(
        "--backend", choices=["postgres", "sqlite", "none", "auto"], default="auto",
        help="warehouse backend (default: postgres when DATABASE_URL is set, else sqlite)",
    )
    run.add_argument("--fixture-dir", default=None, help="directory containing fixture JSON files")
    run.add_argument("--export-dir", default=None, help="dashboard JSON destination (default docs/data)")
    run.add_argument("--limit", type=int, default=None, help="override number of coins to fetch")
    run.add_argument("--log-level", default=None, help="DEBUG | INFO | WARNING | ERROR")

    sub.add_parser("show-config", help="print the effective configuration and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()

    if args.command == "show-config":
        print(json.dumps(dataclasses.asdict(settings), indent=2, default=str))
        return 0

    overrides = {}
    if args.source:
        overrides["source_mode"] = args.source
    if args.fixture_dir:
        overrides["fixture_dir"] = args.fixture_dir
    if args.export_dir:
        overrides["export_dir"] = args.export_dir
    if args.limit:
        overrides["market_limit"] = args.limit
    if args.log_level:
        overrides["log_level"] = args.log_level
    if overrides:
        settings = dataclasses.replace(settings, **overrides)

    setup_logging(settings.log_level)
    result = run_pipeline(settings, backend_choice=args.backend)

    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_s": round((result.finished_at - result.started_at).total_seconds(), 3),
                "rows": result.rows,
                "stages": result.stages,
                "artifacts": sorted(set(result.artifacts.values())),
            },
            indent=2,
        )
    )
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
