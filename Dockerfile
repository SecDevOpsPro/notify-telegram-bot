FROM python:3.11-slim

ARG COMMIT_HASH

# Set environment variables
ENV COMMIT_HASH=${COMMIT_HASH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    APP_PORT=5000 \
    APP_THREADS=1 \
    APP_ENV=prod \
    LOGLEVEL=WARNING

WORKDIR /app

COPY notify_bot/requirements.txt /app

# Copy the rest of the application code
COPY . .

# Install dependencies
# https://packaging.python.org/en/latest/discussions/install-requires-vs-requirements/#requirements-files
# Whereas install_requires metadata is automatically analyzed by pip during an install, requirements files are not, and only are used when a user specifically installs them using python -m pip install -r.
RUN pip install --no-cache-dir -r requirements.txt /app/notify_bot && \
    chmod 755 -R /app/* && \
    touch .env

# Start the application
CMD ["python", "-m", "notify_bot.run_bot"]
