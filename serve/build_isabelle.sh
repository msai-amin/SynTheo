#!/usr/bin/env bash
# Build the syntheo-isabelle image (core/verify/isabelle_hol.py backend). [ADR-006]
#
# This is a LARGE, LONG build (Isabelle + AFP + prebuilt heaps — tens of GB, many
# minutes, needs network). Run it once; the image is then reused per proof. The
# runtime memory boundary is applied at `docker run` time by the verifier, not here.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${1:-syntheo-isabelle:latest}"

echo "Building $IMAGE from serve/isabelle.Dockerfile (this takes a while)..."
docker build -f "$HERE/isabelle.Dockerfile" -t "$IMAGE" "$HERE"

echo "Built $IMAGE:"
docker image inspect "$IMAGE" --format 'size={{.Size}} bytes'
echo "Next: python serve/isabelle_gate.py"
