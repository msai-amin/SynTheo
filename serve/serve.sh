#!/usr/bin/env bash
# Launch the resident-trio model stack. Requires: uname -m == aarch64, docker with
# nvidia runtime, and weights already fetched (see download_models.py).
set -euo pipefail

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" ]]; then
  echo "ERROR: expected aarch64 (DGX Spark), got $ARCH" >&2
  exit 1
fi

cd "$(dirname "$0")"
mkdir -p ../data/hf-cache ../data/traces

docker compose -f docker-compose.yaml up -d
echo "Launched. Poll health with: python3 serve/healthcheck.py"
