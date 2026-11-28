#!/usr/bin/env bash
# The vendor downloads the app depends on must still be there and match the pins in hub/externals.py.
# A removed plugin version would otherwise show only at the next install. Run weekly by .github/workflows/externals.yml.
set -euo pipefail
cd "$(dirname "$0")/.."
ext() { python3 -I -B -c "import sys; sys.path.insert(0, \"hub\"); import externals; print(externals.$1)"; }
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl -fsSL --retry 3 -o "$tmp" "$(ext PLUGIN_URL)" || { echo "::error::the plugin download is gone: $(ext PLUGIN_URL)"; exit 1; }
got=$(sha256sum "$tmp" | cut -d" " -f1)
[ "$got" = "$(ext PLUGIN_SHA256)" ] || { echo "::error::plugin SHA-256 is $got, hub/externals.py pins $(ext PLUGIN_SHA256)"; exit 1; }
curl -fsSI --retry 3 "$(ext CERT_URL)" >/dev/null || { echo "::error::the certificate URL does not answer: $(ext CERT_URL)"; exit 1; }
echo "ok: plugin hash matches, certificate URL answers"
