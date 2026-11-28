#!/bin/bash
# Prints the content tag of the base image: a hash of every file that goes into hub/base.Dockerfile.
# From hub/externals.py the base image reads only PRINTERS (engine presets); the rest must not rebuild it.
set -euo pipefail
cd "$(cd "$(dirname "$0")/../.." && pwd)"
{ git ls-files -z hub/base.Dockerfile hub/externals.env hub/slicer/engine hub/bridge/bridge.patch \
  | sort -z | xargs -0 sha256sum
  python3 -I -B -c 'import json, sys; sys.path.insert(0, "hub"); import externals; print(json.dumps(externals.PRINTERS, sort_keys=True))' | sha256sum; } | sha256sum | cut -c1-12
