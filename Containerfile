# ============================================================================
# Stage 1 — Builder
# Installs all Python dependencies into a virtual environment.
# This stage is discarded in the final image.
# ============================================================================
FROM python:3.14-slim-bookworm AS builder

# Avoid interactive prompts and set locale
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install only the system libs required to BUILD C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment for clean isolation
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python deps first (maximizes layer cache reuse)
COPY pyproject.toml setup.cfg ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir \
       fastapi uvicorn web3 pydantic \
       sqlalchemy asyncpg psycopg2-binary aiosqlite \
       celery redis hvac \
       cryptography python-dateutil httpx

# Copy the source code and install the package itself
COPY src/ ./src/
RUN pip install --no-cache-dir .


# ============================================================================
# Stage 2 — Runtime (Production)
# Minimal image with only runtime dependencies.
# ============================================================================
FROM python:3.14-slim-bookworm AS runtime

# Labels for OCI compliance
LABEL maintainer="candriam" \
      description="2D Link Fiat-to-Crypto Gateway (Enterprise)" \
      version="1.0.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/src"

# Install ONLY runtime C libraries (no compiler, no headers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libffi8 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy application source code
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY pyproject.toml setup.cfg /app/

WORKDIR /app

# Create non-root user for security (UID 1000)
RUN groupadd -r gateway && useradd -r -g gateway -u 1000 gateway \
    && chown -R gateway:gateway /app
USER gateway

# Healthcheck — FastAPI exposes /docs by default
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/docs || exit 1

# Default: start the API server
EXPOSE 8000
CMD ["uvicorn", "gateway.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
