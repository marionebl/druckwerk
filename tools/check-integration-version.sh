#!/usr/bin/env bash
# A change under custom_components/ needs a new version in its manifest.json, or HACS users get no release for it
# (.github/workflows/release.yml tags a release only for a new manifest version).
# usage: check-integration-version.sh <base ref>   (the pull request's target branch)
set -euo pipefail
base=$(git merge-base HEAD "${1:?usage: $0 <base ref>}")
git diff --quiet "$base" HEAD -- custom_components && { echo "ok: custom_components unchanged"; exit 0; }
version() { python3 -c "import json,sys; print(json.load(sys.stdin)['version'])"; }
for m in $(git ls-files 'custom_components/*/manifest.json'); do
  old=$(git show "$base:$m" 2>/dev/null | version || true)
  new=$(version < "$m")
  [ "$old" != "$new" ] || { echo "::error file=$m::custom_components changed but the version is still $new; raise it in $m"; exit 1; }
  echo "ok: $m $old -> $new"
done
