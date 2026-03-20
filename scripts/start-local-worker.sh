#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
NO_BUILD=0
WORKER_DIR="${SENTINELCAM_WORKER_DIR:-$ROOT_DIR/../sentinelCam-worker}"

usage() {
  cat <<'EOF'
Usage: ./scripts/start-local-worker.sh [--worker-dir PATH] [--no-build] [-- EXTRA_WORKER_ARGS...]

Starts the web service with Docker and then launches the sibling sentinelCam-worker locally.
The script reads WORKER_TOKEN, WEB_PORT, WORKER_SOURCE, and WORKER_BIND_HOST from .env.
EOF
}

while (($#)); do
  case "$1" in
    --worker-dir)
      WORKER_DIR="$2"
      shift 2
      ;;
    --no-build)
      NO_BUILD=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

EXTRA_WORKER_ARGS=("$@")

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE. Copy .env.example to .env first." >&2
  exit 1
fi

if [[ ! -d "$WORKER_DIR" ]]; then
  echo "Worker repo not found at $WORKER_DIR" >&2
  echo "Set SENTINELCAM_WORKER_DIR or pass --worker-dir PATH." >&2
  exit 1
fi

if [[ ! -f "$WORKER_DIR/run.sh" ]]; then
  echo "Expected worker launcher at $WORKER_DIR/run.sh" >&2
  exit 1
fi

declare -A CONFIG=()
while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  line="${raw_line%$'\r'}"
  [[ -z "$line" ]] && continue
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ "$line" != *=* ]] && continue
  key="${line%%=*}"
  value="${line#*=}"
  key="$(echo "$key" | tr -d '[:space:]')"
  if [[ "$value" =~ ^\".*\"$ ]] || [[ "$value" =~ ^\'.*\'$ ]]; then
    value="${value:1:-1}"
  fi
  CONFIG["$key"]="$value"
done < "$ENV_FILE"

WORKER_TOKEN="${CONFIG[WORKER_TOKEN]:-}"
WEB_PORT="${CONFIG[WEB_PORT]:-3000}"
WORKER_SOURCE="${CONFIG[WORKER_SOURCE]:-}"
WORKER_BIND_HOST="${CONFIG[WORKER_BIND_HOST]:-0.0.0.0}"

if [[ -z "$WORKER_TOKEN" ]]; then
  echo "WORKER_TOKEN is missing in .env" >&2
  exit 1
fi

COMPOSE_ARGS=(up -d web)
if [[ $NO_BUILD -eq 0 ]]; then
  COMPOSE_ARGS+=(--build)
fi

echo "Starting web service from $ROOT_DIR"
(
  cd "$ROOT_DIR"
  docker compose "${COMPOSE_ARGS[@]}"
)

export WEB_AUTH_TOKEN="$WORKER_TOKEN"
export WEB_ALLOWED_ORIGINS="http://localhost:${WEB_PORT},http://127.0.0.1:${WEB_PORT}"

WORKER_ARGS=(--host "$WORKER_BIND_HOST" --no-window --stream auto)
if [[ -n "$WORKER_SOURCE" ]]; then
  WORKER_ARGS+=(--source "$WORKER_SOURCE")
fi
if ((${#EXTRA_WORKER_ARGS[@]})); then
  WORKER_ARGS+=("${EXTRA_WORKER_ARGS[@]}")
fi

echo "Web UI: http://localhost:${WEB_PORT}"
echo "Worker repo: $WORKER_DIR"
echo "Allowed origins: $WEB_ALLOWED_ORIGINS"
echo "Launching local worker..."

cd "$WORKER_DIR"
exec bash ./run.sh "${WORKER_ARGS[@]}"
