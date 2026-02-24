FROM python:3.14.3-slim

RUN pip install --no-cache-dir requests pyyaml

COPY huntarr.py /app/huntarr.py

WORKDIR /config

# Default: run once. Override CMD or use cron in your orchestrator.
# Mount /config with config.yaml and huntarr.db for persistence.
ENTRYPOINT ["python", "/app/huntarr.py", "-c", "/config/config.yaml"]
