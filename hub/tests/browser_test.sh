#!/bin/bash
# Slice the test plate in headless Chromium through the Druckwerk UI (New print) against the real image, in three setups
# (isolated top-level, cross-origin iframe without COOP/COEP in the parent, no isolation).
# Fake HA, bridge and Supervisor (e2e_mock.py): nothing reaches a printer. Env: IMG, BUILD_ARGS, SKIP_BUILD=1 as in ui_e2e.sh.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
IMG=${IMG:-druckwerk-browser}
[ -n "${SKIP_BUILD:-}" ] || "$HERE/build_image.sh"
docker image ls $IMG --format 'image size {{.Size}}'
WORK=$(mktemp -d); mkdir -p "$WORK/jobs" "$WORK/share"
python3 "$HERE/e2e_mock.py" 18811 18812 & MOCK=$!
trap 'docker logs $IMG 2>&1 | tail -15; docker rm -f $IMG >/dev/null 2>&1 || true; kill $MOCK 2>/dev/null || true' EXIT
python3 "$HERE/e2e_seed.py" "$WORK/seeded"
docker run -d --rm --name $IMG --network host -v "$WORK/seeded:/data" -v "$WORK/jobs:/tmp/jobs" -v "$WORK/share:/tmp/share" --entrypoint python3 \
  -e SLICER_PORT=18813 -e DW_PORT=18811 -e SLICER_SENDER_URL=http://127.0.0.1:18811 -e SLICER_CORE_URL=http://127.0.0.1:18811/core \
  -e HUB_SUPERVISOR_URL=http://127.0.0.1:18811/supervisor -e HUB_HA_WS=ws://127.0.0.1:18812/websocket -e HUB_HOSTNAME=hub-e2e \
  -e HUB_INGRESS_IPS=127.0.0.1 -e SLICER_JOBS=/tmp/jobs -e SLICER_SHARE=/tmp/share -e SUPERVISOR_TOKEN=tok $IMG -I /srv/hub.py >/dev/null
for _ in $(seq 100); do curl -fs http://127.0.0.1:18813/index.html >/dev/null 2>&1 && break; sleep 0.2; done
echo "isolation headers: $(curl -s -D - -o /dev/null http://127.0.0.1:18813/ | grep -i '^cross-origin\|^document-isolation' | tr -d '\r' | tr '\n' ' ')"
HUB=http://127.0.0.1:18813 STL="$REPO/hub/tests/fixtures/test_plate.stl" JOBS="$WORK/jobs" python3 "$HERE/browser_slice.py"
