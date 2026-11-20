# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# BeaconOps — production image
#
# Multi-stage build:
#   1. `builder` installs build dependencies and the project into a virtualenv.
#   2. `runtime` copies only the virtualenv and application source, running as
#      an unprivileged user.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stage 1: builder
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build-time dependencies required to compile any wheels that lack binaries.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated virtualenv that will be copied into the runtime image.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Install runtime dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# Install the project itself (src-layout) without development extras.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    BEACONOPS_ENV=production \
    BEACONOPS_HOST=0.0.0.0 \
    BEACONOPS_PORT=8000 \
    BEACONOPS_RELOAD=false \
    BEACONOPS_DATABASE_URL=sqlite:////data/beaconops.db

# Runtime system dependencies (curl is used by the healthcheck).
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Create an unprivileged user and the data directory it owns.
RUN groupadd --system --gid 1001 beaconops \
    && useradd --system --uid 1001 --gid beaconops --create-home beaconops \
    && mkdir -p /data \
    && chown -R beaconops:beaconops /data

# Copy the pre-built virtualenv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy application source (needed for editable-style imports and tooling).
COPY --chown=beaconops:beaconops src ./src

# Drop privileges.
USER beaconops

# Persist the SQLite database (or mount a volume for PostgreSQL config).
VOLUME ["/data"]

EXPOSE 8000

# Container-level healthcheck hitting the application's /health endpoint.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl --fail --silent --show-error --max-time 5 \
        "http://127.0.0.1:${BEACONOPS_PORT}/health" || exit 1

# Launch the ASGI application with Uvicorn.
CMD ["sh", "-c", "exec uvicorn beaconops.api.routes:app --host \"${BEACONOPS_HOST}\" --port \"${BEACONOPS_PORT}\""]