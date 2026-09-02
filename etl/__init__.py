"""DataFlow Mini ETL.

A small, production-shaped ETL pipeline that:

1. **Extracts** data from public REST APIs (CoinGecko + Fear & Greed Index).
2. **Transforms** it with Pandas (typing, cleaning, derived metrics).
3. **Validates** it with a configurable data-quality gate.
4. **Loads** it into PostgreSQL (or SQLite for zero-dependency local runs),
   and publishes JSON/CSV artifacts that power the GitHub Pages dashboard.

Author: Prachyat Misra
"""

__version__ = "1.0.0"
__author__ = "Prachyat Misra"
