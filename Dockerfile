# syntax=docker/dockerfile:1@sha256:b6afd42430b15f2d2a4c5a02b919e98a525b785b1aaff16747d2f623364e39b6

# docker buildx build . -f "Dockerfile" --platform linux/amd64 --no-cache -t notify-bot \
#   --build-arg CREATED="$(date -u +'%Y-%m-%dT%H:%M:%SZ')" --build-arg APP_VERSION=1.0.0
# https://docs.docker.com/engine/reference/builder/

ARG PYTHON_VERSION=3.12

# ── Builder stage — install deps with uv ─────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /app

# Install git + uv; git is needed so hatch-vcs can read a version tag
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv

# Copy project files
COPY . .

RUN uv pip install --system --no-cache .

# ── Production stage ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS production

# Re-declare so it is usable in COPY --from paths below
ARG PYTHON_VERSION=3.12
ARG COMMIT_HASH="d3faul7"
ARG APP_PORT=8000
ARG CREATED="0000-00-00T00:00:00Z"

ENV COMMIT_HASH=${COMMIT_HASH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    APP_PORT=${APP_PORT} \
    APP_THREADS=1 \
    APP_ENV=prod \
    LOGLEVEL=WARNING \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# https://github.com/opencontainers/image-spec/blob/main/annotations.md
LABEL org.opencontainers.image.authors="Daniel Ramirez <dxas90@gmail.com>" \
    org.opencontainers.image.created=${CREATED} \
    org.opencontainers.image.description="Personal telegram Bot." \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.source=https://github.com/dxas90/notify_bot \
    org.opencontainers.image.title="telegram Bot" \
    org.opencontainers.image.version=${COMMIT_HASH}

ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/app" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

RUN set -eux; \
    apt-get update && \
    apt-get install -y --no-install-recommends --no-install-suggests gettext-base bash && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Copy installed packages and scripts from the builder
COPY --from=builder /usr/local/lib/python${PYTHON_VERSION}/site-packages \
                    /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application support files
COPY --chown=appuser:appuser entrypoint.sh ./
COPY --chown=appuser:appuser config.json.tpl ./

RUN chmod +x /app/entrypoint.sh && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE ${APP_PORT}

VOLUME ["/app/data"]

ENTRYPOINT ["/app/entrypoint.sh"]

CMD ["python", "-m", "notify_bot.run_bot"]
