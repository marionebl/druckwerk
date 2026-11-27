#!/bin/bash
# Builds the base image (if BASE is not set), then the app image $IMG. Env: IMG, BUILD_ARGS, BASE (a ready base image, e.g. from ghcr).
set -euo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
if [ -z "${BASE:-}" ]; then
  BASE=druckwerk-base:local
  docker build -q -f "$REPO/hub/base.Dockerfile" -t "$BASE" "$REPO" >/dev/null
fi
docker build -q -f "${DOCKERFILE:-$REPO/hub/Dockerfile}" --build-arg "BASE=$BASE" ${BUILD_ARGS:-} -t "${IMG:-druckwerk-test}" "$REPO" >/dev/null
