# Self-contained image for the Financial_Reporting_Bot silo.
# Consolidated 2026-07-07: this repo is now the SINGLE source of truth. The
# OpenClaw orchestration (docker-compose.yml, oc-manage, silo .env/data) lives in
# ~/Agents/openclaw/openclaw-infra and builds from this repo as its context.
# Lean Python-only image (chat is handled by the host-native OpenClaw gateway; the
# container just runs the pure-Python scheduler).
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 1000 openclaw && \
    useradd -u 1000 -g openclaw -m -s /bin/bash clawagent

WORKDIR /app

# Deps first (better layer caching), then the bot source.
# .dockerignore keeps .venv/, data/, .git/, secrets out of the build context.
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt schedule tenacity

COPY . /app/
RUN chown -R clawagent:openclaw /app
USER clawagent

RUN chmod +x start.sh

CMD ["bash", "start.sh"]
