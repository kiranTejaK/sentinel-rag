# ==============================================================================
# SentinelRAG Production Multistage Dockerfile (Powered by Astral's uv)
# Stage 1: Build virtual environment via uv with BuildKit caching
# ==============================================================================
FROM python:3.11-slim AS builder

# Copy uv binary from official Astral distribution image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Environment configurations for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.11

WORKDIR /app

# Install native compilation dependencies for C-extensions (asyncpg, hiredis)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml and deterministic lockfile
COPY pyproject.toml uv.lock ./

# Synchronize production dependencies into /app/.venv using BuildKit cache mount
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ==============================================================================
# Stage 2: Minimal Distroless/Slim Production Runtime Image
# ==============================================================================
FROM python:3.11-slim AS runner

WORKDIR /app

# Install minimal runtime shared libraries (libpq for postgresql client) and curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user and group (UID/GID 10001)
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/false -M -d /app appuser

# Copy isolated virtual environment prepared by uv from builder stage
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv

# Copy application source code and migrations with non-root ownership
COPY --chown=appuser:appgroup app /app/app
COPY --chown=appuser:appgroup alembic /app/alembic
COPY --chown=appuser:appgroup alembic.ini /app/alembic.ini

# Set runtime environment variables
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root security context
USER appuser

EXPOSE 8000

# Docker healthcheck querying /healthz
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Launch ASGI production server using uvloop
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--loop", "uvloop", "--no-access-log"]
