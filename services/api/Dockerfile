# =============================================================================
# Stage 1: base — Python deps only, no application code
# =============================================================================
FROM python:3.11-slim AS base

WORKDIR /app

# Copy and install dependencies first so this layer is cached on every
# code-only change (requirements.txt changes bust the cache, code changes don't)
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# =============================================================================
# Stage 2: test — full source + tests + pytest config
#
# Reviewers run:   docker compose exec api pytest tests/ -v
# or:              docker compose run --rm api pytest tests/ -v
#
# The CMD still starts the API server; tests are invoked via exec/run so the
# same container works for both serving traffic and running the suite.
# =============================================================================
FROM base AS test

# App source (from services/api/ relative to project-root build context)
COPY services/api/ .

# Copy all API-level tests (excludes pipeline tests that need heavy deps)
COPY tests/ ./tests/
COPY pytest.ini ./pytest.ini
RUN pip install --no-cache-dir pytest pytest-asyncio pytest-cov httpx

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# =============================================================================
# Stage 3: production — lean image, no test code, no pytest
#
# Switch docker-compose.yml target to "production" before a real deployment.
# =============================================================================
FROM base AS production

COPY services/api/ .

# Bake seed data into the image so Render (no persistent disk, no pre-deploy
# command on free tier) always boots with events + POS already generated.
COPY data/mock/ ./data/mock/
RUN mkdir -p data/generated \
 && python data/mock/generate_store_layout.py \
 && python data/mock/generate_sample_events.py \
 && python data/mock/generate_pos_transactions.py

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
