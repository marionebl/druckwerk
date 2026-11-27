#!/bin/bash
# Prints the content tag of the base image: a hash of every file that goes into hub/base.Dockerfile.
set -euo pipefail
cd "$(cd "$(dirname "$0")/../.." && pwd)"
{ git ls-files -z hub/base.Dockerfile hub/externals.env hub/externals.py hub/slicer/flatpak-app.sh hub/slicer/engine hub/bridge/bridge.patch.xz \
  | sort -z | xargs -0 sha256sum; } | sha256sum | cut -c1-12
