#!/bin/bash
# Builds the app image, then checks what a user depends on: no vendor libs in the image, the plugin is downloaded at start
# (from a local fake CDN serving the real zip, so the pinned hash is really checked; a wrong hash must install nothing),
# the bridge answers on its own port and through ingress and runs on this arch, the browser slicer's files are served
# (engine presets), and G-code made by a browser is stored and printed through the API (bridge and HA mocked). The app has
# no server-side slicer; the slicing itself runs in a browser (hub/tests/browser_test.sh). SKIP_BUILD=1 reuses an existing image $IMG.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
IMG=${IMG:-druckwerk-test}
PLUGIN_ZIP=${PLUGIN_ZIP:-/tmp/druckwerk-plugin/plugin.zip}  # the real vendor zip; fetched once from the URL pinned in externals.py
[ -n "${SKIP_BUILD:-}" ] || "$HERE/build_image.sh"
docker image ls $IMG --format 'image size {{.Size}}'
# The native slicer is gone (it was 733 MB of 967 MB on aarch64): the image must stay small.
BYTES=$(docker image inspect $IMG --format '{{.Size}}'); MAX_MB=${MAX_IMAGE_MB:-450}
[ "$BYTES" -le $((MAX_MB * 1000 * 1000)) ] || { echo "FAIL: image is $((BYTES / 1000000)) MB, the limit is $MAX_MB MB" >&2; exit 1; }
echo "image size check: $((BYTES / 1000000)) MB <= $MAX_MB MB"
"$HERE/no_vendor_libs.sh" $IMG
if [ ! -s "$PLUGIN_ZIP" ]; then
  mkdir -p "$(dirname "$PLUGIN_ZIP")"; curl -fsSL -o "$PLUGIN_ZIP" "$(python3 -I -c "import sys;sys.path.insert(0,'$REPO/hub');import externals;print(externals.PLUGIN_URL)")"
fi
PINNED=$(python3 -I -c "import sys;sys.path.insert(0,'$REPO/hub');import externals;print(externals.PLUGIN_SHA256)")
[ "$(sha256sum "$PLUGIN_ZIP" | cut -d' ' -f1)" = "$PINNED" ] || { echo "FAIL: $PLUGIN_ZIP does not match the pinned SHA-256 $PINNED" >&2; exit 1; }
CDN=$(mktemp -d); cp "$PLUGIN_ZIP" "$CDN/plugin.zip"
python3 -m http.server 18804 --bind 127.0.0.1 -d "$CDN" >/dev/null 2>&1 & FAKE_CDN=$!
python3 "$REPO/hub/slicer/tests/mocks.py" 18801 & MOCK=$!
docker run -d --rm --name $IMG --network host -e DW_PORT=18098 -e DW_PLUGIN_URL=http://127.0.0.1:18804/plugin.zip -e SLICER_PORT=18802 \
  -e SLICER_SENDER_URL=http://127.0.0.1:18801 -e SLICER_CORE_URL=http://127.0.0.1:18801/core \
  -e SLICER_SHARE=/tmp/share -e SUPERVISOR_TOKEN=tok -e HUB_INGRESS_IPS=127.0.0.1 -e HUB_HOSTNAME=hub-test \
  -e HUB_SUPERVISOR_URL=http://127.0.0.1:18801/supervisor -e HUB_HA_WS=ws://127.0.0.1:1/none $IMG >/dev/null
trap 'docker logs $IMG 2>&1 | tail -20; docker rm -f $IMG $IMG-bad >/dev/null 2>&1; kill $MOCK $FAKE_CDN' EXIT
BRIDGE=http://127.0.0.1:18098; B=http://127.0.0.1:18802
ID=(-H "X-Remote-User-Id: smoke-user" -H "X-Remote-User-Name: smoke")
for _ in $(seq 100); do curl -fs "${ID[@]}" $B/api/me >/dev/null 2>&1 && curl -fs $BRIDGE/api/health >/dev/null 2>&1 && break; sleep 0.2; done
# The plugin is downloaded at start; wait for the bridge to report it ready, then the bridge binary must run on this arch.
for _ in $(seq 300); do curl -fs $BRIDGE/api/health | grep -q '"state": "ready"' && break; sleep 0.5; done
echo "bridge health: $(curl -fsS $BRIDGE/api/health)"
curl -fsS $BRIDGE/api/health | python3 -c 'import json,sys;d=json.load(sys.stdin);assert d["plugin"]["state"]=="ready" and d["plugin"]["error"] is None,d'
docker exec $IMG /opt/bridge/bridge.sh --check
# The slim ffmpeg must do what server.py asks of it: H.264 to a JPEG still, H.264 to MPEG-TS (copy), MPEG-TS to a JPEG still.
SAMPLE=$HERE/fixtures/sample.h264
docker exec -i $IMG ffmpeg -loglevel error -f h264 -i - -frames:v 1 -f image2 -c:v mjpeg - < "$SAMPLE" | head -c2 | xxd -p | grep -qx ffd8 \
  || { echo "FAIL: ffmpeg gave no JPEG from H.264" >&2; exit 1; }
docker exec -i $IMG ffmpeg -loglevel error -framerate 15 -f h264 -i - -c copy -flush_packets 1 -f mpegts - < "$SAMPLE" > /tmp/dw-sample.ts
[ -s /tmp/dw-sample.ts ] || { echo "FAIL: ffmpeg gave no MPEG-TS" >&2; exit 1; }
docker exec -i $IMG ffmpeg -loglevel error -f mpegts -i - -frames:v 1 -f image2 -c:v mjpeg - < /tmp/dw-sample.ts | head -c2 | xxd -p | grep -qx ffd8 \
  || { echo "FAIL: ffmpeg gave no JPEG from MPEG-TS" >&2; exit 1; }
echo "ffmpeg: H.264 to JPEG, H.264 to MPEG-TS, MPEG-TS to JPEG ok"
# Identity: the app trusts Supervisor ingress headers, so no headers means 401 and headers mean 200.
code=$(curl -s -o /dev/null -w '%{http_code}' $B/api/me); [ "$code" = 401 ] || { echo "FAIL: /api/me without identity gave $code" >&2; exit 1; }
echo "GET /api/me without identity: $code"
ME=$(curl -fsS "${ID[@]}" $B/api/me); echo "GET /api/me with identity: 200 $ME"
echo "$ME" | python3 -c 'import json,sys;d=json.load(sys.stdin);assert d["user_id"]=="smoke-user" and d["is_admin"] is False and d["setup_done"] is False,d'
# The job flow below runs as the integration (internal caller), which the mock HA cannot grant to a user.
SECRET=$(docker exec $IMG python3 -c 'import json;print(json.load(open("/data/hub.json"))["secret"])')
ID=(-H "X-Hub-Secret: $SECRET")
health() {  # $1 label, $2 url
  local out; out=$(curl -fsS "${ID[@]}" "$2") || { echo "FAIL: $1: no answer from $2" >&2; exit 1; }
  echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);assert d["server"]=="ok" and d["accounts"]==[] and d["plugin"]["state"]=="ready",d' || { echo "FAIL: $1: unexpected health $out" >&2; exit 1; }
  echo "$1: health ok, accounts []"
}
health "bridge port" $BRIDGE/api/health
health "via ingress" $B/api/bridge/api/health
# The web UI loads without identity (the API behind it does not). Captured first: grep -q would break curl's pipe.
page=$(curl -fsS $B/); grep -q '<title>Druckwerk</title>' <<<"$page"
js=$(curl -fsS $B/api.js); grep -q "api/me" <<<"$js"; curl -fsS -o /dev/null $B/views/queue.js; echo "web UI served at /"
curl -fsS "${ID[@]}" $B/api/bridge/ | grep -q 'Druckwerk bridge'
# The browser slices; the app only serves the engine and stores the G-code the browser uploads. Upload a model, ask for the
# filament preset (read from the engine presets in the image), upload a real vendor-slicer H2S G-code like the browser does.
J=$(curl -fsS "${ID[@]}" -X POST --data-binary @"$REPO/hub/tests/fixtures/test_plate.stl" "$B/api/jobs?name=test_plate.stl" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
FIL=$(curl -fsS "${ID[@]}" "$B/api/engine/filament?serial=H2S0001&tray=0:0" | python3 -c 'import json,sys;print(json.load(sys.stdin)["filament"])')
echo "filament preset from the image's engine presets: $FIL"
curl -fsS "${ID[@]}" -o /dev/null -w 'engine version.json %{http_code}\n' $B/engine/version.json
code=$(curl -s -o /dev/null -w '%{http_code}' "${ID[@]}" -X POST $B/api/jobs/$J/slice)
[ "$code" = 404 ] || { echo "FAIL: POST /api/jobs/<id>/slice gave $code, expected 404 (no server-side slicing)" >&2; exit 1; }
echo "POST /api/jobs/<id>/slice: $code (no server-side slicing)"
Q=$(python3 -c 'import sys,urllib.parse as u;print(u.urlencode({"serial":"H2S0001","quality":"better","tray":"0:0","filament":sys.argv[1],"seconds":7440,"grams":75.22,"metres":24.5,"engine":"mt","slice_s":9}))' "$FIL")
curl -fsS "${ID[@]}" -X POST --data-binary @"$REPO/hub/tests/fixtures/gcode/h2s_multi_object.gcode" "$B/api/jobs/$J/gcode?$Q" \
  | python3 -c 'import json,sys;j=json.load(sys.stdin);print(json.dumps({k:j.get(k) for k in ("state","error","info")}));assert j["state"]=="done" and j["info"]["seconds"]==7440,j'
curl -fsS "${ID[@]}" -o /dev/null -w 'plate.gcode %{http_code} %{size_download} B\n' $B/api/jobs/$J/plate.gcode
# The app sends the print to the real bridge, which has no account here: it must refuse with its own error text.
code=$(curl -s -o /tmp/print.json -w '%{http_code}' "${ID[@]}" -X POST -H 'Content-Type: application/json' -d '{"confirm":true}' $B/api/jobs/$J/print); echo "print: $code $(cat /tmp/print.json)"
case "$code" in 409|502) python3 -c 'import json;assert json.load(open("/tmp/print.json"))["error"]' ;; *) echo "FAIL: print gave $code" >&2; exit 1 ;; esac
curl -fsS http://127.0.0.1:18801/calls; echo
curl -fsS http://127.0.0.1:18801/calls | python3 -c 'import json,sys;c=[x for x in json.load(sys.stdin) if x["path"]=="/supervisor/discovery"];assert c and c[0]["body"]["service"]=="druckwerk" and c[0]["body"]["config"]["host"]=="hub-test",c;print("discovery sent to Supervisor")'
# A wrong hash must be reported by health, retried, and must install nothing.
docker run -d --rm --name $IMG-bad --network host -e DW_PORT=18099 -e SLICER_PORT=18805 -e DW_PLUGIN_BACKOFF_MIN=1 \
  -e DW_PLUGIN_URL=http://127.0.0.1:18804/plugin.zip -e DW_PLUGIN_SHA256=$(printf '0%.0s' $(seq 64)) $IMG >/dev/null
for _ in $(seq 100); do curl -fs http://127.0.0.1:18099/api/health | grep -q '"state": "failed"' && break; sleep 0.2; done
sleep 4
BAD=$(curl -fsS http://127.0.0.1:18099/api/health); echo "wrong hash, bridge health: $BAD"
echo "$BAD" | python3 -c 'import json,sys;d=json.load(sys.stdin);p=d["plugin"];assert d["server"]=="ok" and p["state"]=="failed" and "SHA-256 mismatch" in p["error"] and p["attempt"]>=2,d'
. "$HERE/../externals.env"; expr=""; for p in $PLUGIN_FIND_PATTERNS; do expr="$expr${expr:+ -o }-name \"$p\""; done
LEFT=$(docker exec $IMG-bad sh -c "find / /data -xdev $expr")
[ -z "$LEFT" ] || { echo "FAIL: wrong hash but files installed: $LEFT" >&2; exit 1; }
echo "wrong hash: nothing installed"
