# HAT backend image (HAT-OPS-040). Build context: the repository root
# (deploy/hat/docker-compose.yml sets it); backend.Dockerfile.dockerignore keeps the context small.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# curl for the healthcheck. (Backups run on the host: scripts/hat_backup.py, runbook §9.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Same versions CI tests: the requirements pinned to the CI lock, then fyers-apiv3 with
# --no-deps (it pins an aiohttp that does not build on Python 3.14), as
# scripts/install_backend_deps.py does.
COPY backend/requirements.txt backend/requirements-ci.lock backend/
RUN sed -e '/^#/d' -e 's/ .*//' backend/requirements-ci.lock > /tmp/constraints.txt \
    && pip install --only-binary :all: -r backend/requirements.txt -c /tmp/constraints.txt \
    && pip install --only-binary :all: --no-deps "fyers-apiv3==3.2.0" \
    && rm /tmp/constraints.txt

COPY backend/ backend/
COPY scripts/ scripts/

# The service user. The Fyers token (fyers/config), logs and HAT_DATA_ROOT are volumes it owns.
RUN useradd --system --uid 10001 --home-dir /app hat \
    && mkdir -p /app/fyers/config /app/logs /data/hat \
    && chown -R hat:hat /app/fyers /app/logs /data/hat
USER hat

# Inside the container the server listens on all interfaces; the compose file publishes the
# port on 127.0.0.1 only, so nothing outside the host (or the VPN tunnel) reaches it.
ENV BACKEND_HOST=0.0.0.0 \
    BACKEND_PORT=8000 \
    HAT_DATA_ROOT=/data/hat

EXPOSE 8000
CMD ["python", "backend/start_server.py"]
