"""Shared pytest fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture(scope="session")
def raw_markets() -> list[dict]:
    return json.loads((FIXTURE_DIR / "coins_markets_raw.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def raw_fear_greed() -> list[dict]:
    payload = json.loads((FIXTURE_DIR / "fear_greed_raw.json").read_text(encoding="utf-8"))
    return payload["data"]
