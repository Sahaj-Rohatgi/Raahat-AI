# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — frontend. The JSX is transpiled ahead of time so the browser never
# ships or runs a compiler, and so the CSP does not need 'unsafe-eval'.
# ---------------------------------------------------------------------------
FROM node:22-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json* ./
RUN npm ci --omit=optional || npm install
COPY build.mjs raahat.html ./
COPY static ./static
RUN node build.mjs

# ---------------------------------------------------------------------------
# Stage 2 — python dependencies, built separately so the runtime image carries
# no compiler toolchain.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps
WORKDIR /wheels
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 3 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Runs as an unprivileged user: a container process that cannot write its own code or
# escalate is one fewer thing standing between a dependency CVE and the database.
RUN useradd --create-home --uid 10001 raahat
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deps /wheels /wheels
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels requirements.txt

COPY --chown=raahat:raahat backend ./backend
COPY --chown=raahat:raahat alembic ./alembic
COPY --chown=raahat:raahat alembic.ini gunicorn.conf.py raahat.html ./
COPY --chown=raahat:raahat --from=frontend /build/static ./static

# Baked in so an image can never be started with demo accounts or the raw-table inspector.
ENV APP_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SEED_DEMO_DATA=0 \
    ENABLE_DB_INSPECTOR=0 \
    REQUIRE_FRONTEND_BUNDLE=1

USER raahat
EXPOSE 8000

# Hits the liveness endpoint, which deliberately does not touch the database — see /healthz.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["gunicorn", "backend.main:app", "-c", "gunicorn.conf.py"]
