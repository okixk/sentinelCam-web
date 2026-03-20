#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NO_BUILD=0

usage() {
  cat <<'EOF'
Usage: ./scripts/start-docker-worker.sh [--no-build]

Starts sentinelCam-web together with the Linux worker container from docker-compose.yml.
Use WORKER_VIDEO_DEVICE or WORKER_SOURCE in .env to adjust camera or remote-stream input.
EOF
}

while (($#)); do
  case "$1" in
    --no-build)
      NO_BUILD=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

COMPOSE_ARGS=(up -d)
if [[ $NO_BUILD -eq 0 ]]; then
  COMPOSE_ARGS+=(--build)
fi

echo "Starting Linux Docker stack from $ROOT_DIR"
(
  cd "$ROOT_DIR"
  docker compose "${COMPOSE_ARGS[@]}"
)

echo "Web UI: http://localhost:3000"
echo "Worker health: http://127.0.0.1:8080/health"
