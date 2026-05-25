# syntax=docker/dockerfile:1
# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools and compile wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security
RUN groupadd --gid 1001 agent \
 && useradd --uid 1001 --gid agent --shell /bin/bash --create-home agent

WORKDIR /app

# Install pre-built wheels from builder — no compiler needed at runtime
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels /wheels/*.whl \
 && rm -rf /wheels

# Copy application source
COPY agent/ ./agent/

# Environment defaults (all secrets must be injected at runtime)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO \
    PORT=8000

USER agent

EXPOSE 8000

# Graceful shutdown: uvicorn handles SIGTERM by finishing in-flight requests
CMD ["sh", "-c", \
     "uvicorn agent.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-graceful-shutdown 30"]
