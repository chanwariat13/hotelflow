FROM python:3.11-slim

# Don't write .pyc files, don't buffer stdout/stderr, don't keep pip cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime tooling for the healthcheck. python:3.11-slim does not ship
# curl by default; we install it without the recommended extras to keep the
# image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Run as a non-root user.
RUN useradd --create-home --shell /bin/bash app && chown -R app:app /app
USER app

EXPOSE 8000

# Liveness check. Adjust the URL if you front this with a sub-path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/ || exit 1

# NOTE: Secrets (DB_PASS, REDIS_PASS, EVOLUTION_API_KEY, ADMIN_PASSWORD,
# SECRET_KEY, etc.) MUST be injected at RUNTIME, not at build time.
# In Coolify: uncheck "Is Build Variable?" for those env vars so they are
# never baked into the image layers.

CMD ["python", "main.py"]
