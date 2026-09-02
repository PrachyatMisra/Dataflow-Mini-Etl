# ---------------------------------------------------------------------------
# DataFlow Mini ETL - pipeline image
# Small, non-root, deterministic. Default CMD runs one live ETL pass against
# the DATABASE_URL provided by docker-compose (PostgreSQL).
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the pipeline source.
COPY etl/ ./etl/

# Run as a non-root user.
RUN useradd --create-home --uid 10001 etl \
    && mkdir -p /app/data /app/artifacts /app/docs/data \
    && chown -R etl:etl /app
USER etl

ENTRYPOINT ["python", "-m", "etl"]
CMD ["run", "--backend", "postgres"]
