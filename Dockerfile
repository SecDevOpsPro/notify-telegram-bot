# syntax=docker/dockerfile:1@sha256:b6afd42430b15f2d2a4c5a02b919e98a525b785b1aaff16747d2f623364e39b6

# Comments are provided throughout this file to help you get started.
# If you need more help, visit the Dockerfile reference guide at
# docker buildx build . -f "Dockerfile"  --platform linux/amd64 --no-cache -t workflow --build-arg CREATED="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
# https://docs.docker.com/engine/reference/builder/

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim as builder


WORKDIR /app

COPY . .

RUN pip install --upgrade pip build &&\
    python -m build

FROM python:${PYTHON_VERSION}-slim as production

ARG COMMIT_HASH="d3faul7"
ARG APP_PORT=8000
ARG CREATED="0000-00-00T00:00:00Z"

# Set environment variables
ENV COMMIT_HASH=${COMMIT_HASH} \
    # Keeps Python from buffering stdout and stderr to avoid situations where
    # the application crashes without emitting any logs due to buffering.
    PYTHONUNBUFFERED=1 \
    # Prevents Python from writing pyc files.
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
    org.opencontainers.image.version="1.0.0"

# Create a non-privileged user that the app will run under.
# See https://docs.docker.com/develop/develop-images/dockerfile_best-practices/#user
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
    apt update && \
    apt install -y --no-install-recommends --no-install-suggests gettext-base bash && apt clean && \
    rm -rf /var/lib/apt/lists/*

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Copy the source code into the container.
COPY --from=builder /app/dist /app/dist
COPY --from=builder /app/entrypoint.sh /app/entrypoint.sh
COPY --from=builder /app/config.json.tpl /app/config.json.tpl

# Download dependencies as a separate step to take advantage of Docker's caching.
# Leverage a cache mount to /root/.cache/pip to speed up subsequent builds.
# Leverage a bind mount to requirements.txt to avoid having to copy them into
# into this layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir /app/dist/*-py3-none-any.whl && \
    chmod +x /app/entrypoint.sh && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app && \
    rm -rf /app/dist

# Switch to the non-privileged user to run the application.
USER appuser

# Expose the port that the application listens on.
EXPOSE ${APP_PORT}

ENTRYPOINT ["/app/entrypoint.sh"]

# Start the application
CMD ["python", "-m", "notify_bot.run_bot"]
