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

# Tests and pytest config (outside services/api/, only available because
# the build context is the project root, not ./services/api)
COPY tests/ ./tests/
COPY pytest.ini ./pytest.ini

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# =============================================================================
# Stage 3: production — lean image, no test code, no pytest
#
# Switch docker-compose.yml target to "production" before a real deployment.
# =============================================================================
FROM base AS production

COPY services/api/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
