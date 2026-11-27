#!/bin/bash
# PROTOTYPE (measurement only). Runs in GitHub Actions on the branch wasm-node-prototype: slices the test plate with the
# WASM engine in Node (H2S and P1S, st and mt), measures time, peak RSS and node size, slices natively from the app image,
# and compares the G-code. No `set -e`: every section must run and print its numbers.
set -uo pipefail
REPO=$(pwd); W=$REPO/work; R=$W/results; mkdir -p "$R" "$W/in" "$W/nat" "$W/bin"
ARCH=${ARCH:-$(uname -m)}
case $ARCH in aarch64) IMG=ghcr.io/marionebl/druckwerk-aarch64:3.0.1 ;; *) IMG=ghcr.io/marionebl/druckwerk-amd64:3.0.1 ;; esac
STL=$REPO/hub/tests/fixtures/test_plate.stl; cp "$STL" "$W/in/test_plate.stl"
sudo apt-get install -y -qq time >/dev/null 2>&1; ls -l /usr/bin/time
echo "### runner"; uname -m; nproc; free -m | sed -n 2p; lscpu | grep -i 'model name\|^Architecture' ; node --version
echo "### engine and presets (hub/slicer/engine/build.sh, as the image build does)"
bash hub/slicer/engine/build.sh "$W/img" 2>&1 | tail -12
ENG=$W/img/engine/v0.6.0-m1.9; PRE=$W/img/engine/presets
echo "engine files (unpacked, as in the image): $(du -sm "$ENG" | cut -f1) MB; bytes by file:"; ls -l "$ENG" | awk '{print $5, $9}'
echo "presets: $(du -sm "$PRE" | cut -f1) MB"
echo "P1S in PRINTERS: $(python3 -c 'import sys;sys.path.insert(0,"hub");import externals;print(sorted(externals.PRINTERS))')"

echo "### node binaries (official docker images)"
for t in bookworm-slim alpine; do
  docker pull -q node:22-$t >/dev/null; id=$(docker create node:22-$t); docker cp "$id:/usr/local/bin/node" "$W/bin/node-$t"; docker rm "$id" >/dev/null
  echo "-- node:22-$t: $(docker run --rm node:22-$t node --version) image size $(docker image inspect node:22-$t --format '{{.Size}}' | awk '{printf "%.0f MB", $1/1048576}')"
  ls -l "$W/bin/node-$t"; du -m "$W/bin/node-$t"
  docker run --rm --entrypoint sh node:22-$t -c 'ldd /usr/local/bin/node; echo; ldd /usr/local/bin/node | sed -n "s/.*=> \(\/[^ ]*\) .*/\1/p" | xargs ls -lL | awk "{s+=\$5; print \$5, \$9} END {print \"libs total bytes\", s}"; du -sm /usr/local/bin/node /usr/local/lib/node_modules /opt/yarn* 2>/dev/null'
done
cp "$W/bin/node-bookworm-slim" "$W/bin/node-stripped"; strip --strip-all "$W/bin/node-stripped" 2>&1 | tail -1; echo "stripped bookworm node: $(stat -c %s "$W/bin/node-stripped") bytes"
NODE=$W/bin/node-bookworm-slim; chmod +x "$NODE"; "$NODE" --version

echo "### Node slices on the runner (official bookworm-slim node binary, /usr/bin/time -v)"
slice() {  # label model variant filament out
  local label=$1 model=$2 variant=$3 fil=$4 out=$5
  MODEL=$model VARIANT=$variant ENGINE_DIR=$ENG PRESETS=$PRE /usr/bin/time -v -o "$R/$label.time" "$NODE" hub/slicer/engine/node_slice.mjs "$W/in/test_plate.stl" "$out" "$fil" > "$R/$label.json" 2> "$R/$label.err"
  rc=$?
  echo "RESULT $label exit=$rc $(grep 'Elapsed (wall' "$R/$label.time" | sed 's/.*): //') wall, $(grep 'Maximum resident' "$R/$label.time" | sed 's/.*: //') KB peak RSS, user $(grep 'User time' "$R/$label.time" | sed 's/.*: //')s"
  tail -c 600 "$R/$label.json"; [ $rc -eq 0 ] || tail -5 "$R/$label.err"
}
for i in 1 2; do
  slice h2s-st-$i H2S st "Generic PETG @BBL H2S" "$W/node-h2s-st-$i.gcode.3mf"
  slice h2s-mt-$i H2S mt "Generic PETG @BBL H2S" "$W/node-h2s-mt-$i.gcode.3mf"
  slice p1s-st-$i P1S st "Generic PETG" "$W/node-p1s-st-$i.gcode.3mf"
  slice p1s-mt-$i P1S mt "Generic PETG" "$W/node-p1s-mt-$i.gcode.3mf"
done
slice h2s-mt64 H2S mt64 "Generic PETG @BBL H2S" "$W/node-h2s-mt64.gcode"
for q in good best; do QUALITY=$q slice h2s-st-$q H2S st "Generic PETG @BBL H2S" "$W/node-h2s-st-$q.gcode"; done
echo "-- alpine (musl) node, single thread, H2S, in a container"
docker run --rm -v "$REPO:/r:ro" -v "$W/in:/in:ro" -v "$W/img:/img:ro" -v "$W/bin:/bin2:ro" -e MODEL=H2S -e VARIANT=st -e ENGINE_DIR=/img/engine/v0.6.0-m1.9 -e PRESETS=/img/engine/presets \
  node:22-alpine sh -c 'apk add -q time >/dev/null 2>&1; /usr/bin/time -v node /r/hub/slicer/engine/node_slice.mjs /in/test_plate.stl /tmp/o.gcode "Generic PETG @BBL H2S" 2>&1 | tail -3; ' 2>&1 | grep -v '^\s*$' | cut -c1-300 | tee "$R/alpine.txt" | grep -i 'elapsed\|maximum res\|slice_s\|fail\|error' 

echo "### Native Bambu Studio from the app image $IMG (same runner)"
docker pull -q "$IMG" >/dev/null && {
  docker image inspect "$IMG" --format 'image unpacked size bytes: {{.Size}}' | awk '{print $0, "=", $NF/1048576, "MiB", $NF/1000000, "MB"}'
  docker run --rm --entrypoint sh "$IMG" -c 'du -xm -d1 / 2>/dev/null | sort -n | tail -12; echo; du -xm -d1 /opt/bs 2>/dev/null | sort -n; echo; du -xsm /opt/bs/resources/profiles /srv/engine /srv 2>/dev/null; ls /srv; ls /srv/engine 2>/dev/null; command -v node python3 bash; ls -l /usr/lib/*/libstdc++.so.6* /usr/lib/*/libgcc_s.so.1 2>/dev/null; cat /etc/os-release | head -2; ldd --version | head -1'
  docker run --rm --entrypoint sh "$IMG" -c 'du -xm -d2 /usr /lib /var /opt 2>/dev/null | sort -n | tail -25'
  docker run --rm --entrypoint sh "$IMG" -c 'python3 -I /opt/bs/bin/bs.py filament-for /opt/bs/resources/profiles "Bambu Lab H2S 0.4 nozzle" GFG02 PETG' 2>&1 | tail -2
  for m in H2S P1S; do
    if [ $m = H2S ]; then P="Bambu Lab H2S 0.4 nozzle"; PR="0.20mm Standard @BBL H2S"; F="Generic PETG @BBL H2S"; else P="Bambu Lab P1S 0.4 nozzle"; PR="0.20mm Standard @BBL X1C"; F="Generic PETG"; fi
    docker run --rm -v "$REPO/hub/tests/rss_run.py:/rss_run.py:ro" -v "$W/in:/in:ro" -v "$W/nat:/out" -e PRINTER="$P" -e PROCESS="$PR" -e BED="Textured PEI Plate" --entrypoint python3 "$IMG" -I /rss_run.py bash /opt/bs/bin/slice.sh /in/test_plate.stl /out/$m "$F" 2>&1 | tail -6 | cut -c1-300 | tee "$R/native-$m.txt"
    ls -l "$W/nat/$m" 2>&1 | tail -2
  done
  echo "-- Node (bookworm-slim binary) inside the app image, same python RSS wrapper, to prove it runs on the image's libc"
  for m in H2S P1S; do
    if [ $m = H2S ]; then F="Generic PETG @BBL H2S"; else F="Generic PETG"; fi
    for v in st mt; do
      docker run --rm -v "$REPO:/r:ro" -v "$W/in:/in:ro" -v "$W/img:/img:ro" -v "$NODE:/node:ro" -v "$W/nat:/out" -e MODEL=$m -e VARIANT=$v -e ENGINE_DIR=/img/engine/v0.6.0-m1.9 -e PRESETS=/img/engine/presets \
        --entrypoint python3 "$IMG" -I /r/hub/tests/rss_run.py /node /r/hub/slicer/engine/node_slice.mjs /in/test_plate.stl /out/inimage-$m-$v.gcode "$F" 2>&1 | tail -4 | cut -c1-400 | tee "$R/inimage-$m-$v.txt"
    done
  done
}

echo "### G-code comparison, native vs node st (hub/tests/node_vs_native.py)"
for m in H2S P1S; do
  l=$(echo $m | tr A-Z a-z); N=$(ls "$W/nat/$m"/*.gcode.3mf 2>/dev/null | head -1)
  if [ -z "$N" ]; then echo "no native result for $m"; continue; fi
  python3 hub/tests/node_vs_native.py $m "$N" "$W/node-$l-st-1.gcode.3mf" "$R/compare-$m.json" 2>&1 | cut -c1-400 | tee "$R/compare-$m.txt"
  echo "-- st vs mt identical except the timestamp line?"; diff <(unzip -p "$W/node-$l-st-1.gcode.3mf" Metadata/plate_1.gcode) <(unzip -p "$W/node-$l-mt-1.gcode.3mf" Metadata/plate_1.gcode) | head -4 | cut -c1-120
done
cp "$W"/node-*.gcode.3mf "$R/" 2>/dev/null; for m in H2S P1S; do cp "$W/nat/$m"/*.gcode.3mf "$R/native-$m.gcode.3mf" 2>/dev/null; done
exit 0
