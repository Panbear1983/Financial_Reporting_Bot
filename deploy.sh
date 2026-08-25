#!/bin/bash
# deploy.sh — canonical build / verify / deploy wrapper for the Financial_Reporting_Bot.
#
# WHY THIS EXISTS
#   The live bot runs as a Docker container (openclaw-financial-bot). Its code is BAKED
#   INTO the image openclaw-hardened:latest — only data/, logs/, scripts/ are bind-mounted.
#   Editing this repo does NOT change the running bot until the image is rebuilt and the
#   container recreated. This script does that, through the CONSOLIDATED orchestrator at
#   ~/Agents/openclaw/openclaw-infra (which builds from THIS repo).
#
#   Do NOT use ~/Agents/openclaw/oc-manage — that is the retired pre-consolidation path
#   that builds from a stale code copy and will silently ship old code.
set -euo pipefail

AGENT_ID="financial-bot"
OC_INFRA="${OC_INFRA:-$HOME/Agents/openclaw/openclaw-infra}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OC="${OC_INFRA}/oc-manage"
IMAGE="openclaw-hardened:latest"

usage() {
  cat <<EOF
Usage: ./deploy.sh {build|sandbox|dev|deploy|logs} [morning|closing]

  build            Rebuild the image from this repo. Does NOT touch the running bot.
  sandbox [mode]   One-shot report from the BUILT image in a throwaway container.
                   No Telegram, no schedule. Run 'build' first to test new code.
  dev [mode]       One-shot report with THIS working tree bind-mounted over /app —
                   live code, no rebuild, throwaway container. No Telegram, no schedule.
                   Fastest edit->see loop. Never clashes with the live container.
  deploy           Rebuild + recreate the LIVE container (re-arms schedule, resumes
                   Telegram pushes). Snapshots the whole current working tree.
  logs             Follow the live container logs.

Recommended: ./deploy.sh dev  ->  eyeball output  ->  ./deploy.sh deploy
EOF
}

[ -x "$OC" ] || { echo "ERROR: canonical oc-manage not found/executable at $OC" >&2; exit 1; }

case "${1:-}" in
  build)   "$OC" build   "$AGENT_ID" ;;
  sandbox) "$OC" sandbox "$AGENT_ID" "${2:-morning}" ;;
  deploy)  "$OC" deploy  "$AGENT_ID" ;;
  logs)    "$OC" logs    "$AGENT_ID" ;;
  dev)
    # Mirror oc-manage's sandbox path (throwaway `docker run --rm`, no restart policy,
    # no fixed name -> cannot collide with or replace the live container) but bind-mount
    # the working tree so edits run WITHOUT a rebuild. Deps come from the existing image.
    SILO="${OC_INFRA}/agents/${AGENT_ID}"
    echo "--- DEV one-shot (${2:-morning}) — live code from ${REPO_DIR}, NO Telegram, NO schedule ---"
    docker run --rm \
      --env-file "${SILO}/.env" \
      -e OPENCLAW_DATA_DIR=/app/data \
      -e SANDBOX_MODE=true \
      -v "${REPO_DIR}:/app" \
      -v "${SILO}/data:/app/data" \
      -v "${SILO}/logs:/app/logs" \
      "${IMAGE}" \
      python scheduler.py --now="${2:-morning}" --sandbox
    ;;
  *) usage; exit 1 ;;
esac
