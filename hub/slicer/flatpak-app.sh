#!/bin/bash
# Installs the slicer from Flathub (native build for this machine's arch) and makes an APP dir that slice.sh accepts:
# APP/bin/studio runs the binary with the GNOME runtime's own ld.so, so no flatpak/bwrap at run time.
# usage: flatpak-app.sh <appdir>   (needs flatpak, run as root for --system; leaves the flatpak install broken)
set -euo pipefail
E=$(dirname "$0"); [ -f "$E/externals.env" ] || E=$E/..; . "$E/externals.env"
OUT=${1:?appdir}
flatpak remote-add --system --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --system -y --noninteractive flathub "$FLATHUB_APP_ID"
flatpak info --system "$FLATHUB_APP_ID"
A=$(flatpak info --system --show-location "$FLATHUB_APP_ID")/files
RT_REF=$(flatpak info --system --show-runtime "$FLATHUB_APP_ID")
R=$(flatpak info --system --show-location "$RT_REF")/files
echo "app=$A runtime=$R ($RT_REF)"
LDSO=$(cd "$R" && find lib lib64 -name "ld-linux-$(uname -m | tr _ -).so.[0-9]" 2>/dev/null | head -1 || true)
[ -n "$LDSO" ] || { echo "FAIL: no ld-linux in runtime" >&2; exit 2; }
LIBDIR=lib/$(uname -m)-linux-gnu
RES=$(cd "$A" && dirname "$(find . -path "$PROFILE_SEARCH" | head -1)" | xargs dirname)
echo "resources=$A/$RES"
mkdir -p "$OUT/bin"; rm -rf "$OUT/app" "$OUT/runtime" "$OUT/resources"
mv "$A" "$OUT/app"; mv "$R" "$OUT/runtime";  # mv: the files are hardlinks into the ostree repo, a copy doubles the size
ln -s "app/$RES" "$OUT/resources"
REAL=$(cd "$OUT/app" && find bin -maxdepth 1 -type f \( -name "${STUDIO_BINARY_NAMES%% *}" -o -name "${STUDIO_BINARY_NAMES##* }" \) | head -1 || true)
[ -n "$REAL" ] || { echo "FAIL: no slicer binary in app/bin" >&2; exit 2; }
cat > "$OUT/bin/studio" <<W
#!/bin/sh
D=\$(cd "\$(dirname "\$0")/.." && pwd)
exec "\$D/runtime/$LDSO" --library-path "\$D/app/lib:\$D/runtime/$LIBDIR:\$D/runtime/lib" "\$D/app/$REAL" "\$@"
W
chmod 755 "$OUT/bin/studio"
du -sh "$OUT/app" "$OUT/runtime"
# Slim down: the CLI slices headless, so the GUI data, static libs and the runtime's unused parts are dead weight.
BEFORE=$(du -sk "$OUT" | cut -f1)
find "$OUT/app" -name '*.a' -delete   # static wx/boost archives, ~700 MB, used only to build the slicer
rm -rf "$OUT/app/lib/pkgconfig" "$OUT/app/lib/wx/config" "$OUT/app/lib/wx/include" "$OUT/app/bin/wxrc-3.2" "$OUT/app/bin/wxrc" "$OUT/app/bin/wx-config" \
  "$OUT/app/share/doc" "$OUT/app/share/man" "$OUT/app/share/locale"
( cd "$OUT/app/$RES" && rm -rf hms web calib tooltip i18n )   # printer-error images, GUI pages, calibration models, GUI strings
( cd "$OUT/runtime" && rm -rf bin lib/python3* libexec/glycin-loaders lib/aarch64-linux-gnu/gstreamer-1.0 lib/aarch64-linux-gnu/frei0r-1 \
  share/i18n share/hwdata share/mythes share/gtk-4.0 share/icons share/doc share/man share/locale share/gtk-doc share/help \
  share/fonts/noto-emoji share/fonts/Adwaita share/fonts/tex-gyre include )
# Shared libraries over 500 kB that the slicer binary does not link (GTK 4, WebKitGTK 6, SpiderMonkey, AV1, ...). Smaller
# ones stay in case something loads them by name. Everything in a sub directory stays.
LP="$OUT/app/lib:$OUT/runtime/$LIBDIR:$OUT/runtime/lib"
LD_LIBRARY_PATH=$LP "$OUT/runtime/$LDSO" --list "$OUT/app/$REAL" | tee /tmp/ld.list | awk '/=>/ {print $3}' | xargs -r readlink -f | sort -u > /tmp/ld.keep
if grep -q 'not found' /tmp/ld.list; then echo "FAIL: slicer lacks a library after the slim down" >&2; grep 'not found' /tmp/ld.list >&2; exit 2; fi
find "$OUT/runtime/$LIBDIR" -maxdepth 1 \( -type f -o -type l \) -name '*.so*' -size +500k -print0 | while IFS= read -r -d '' f; do
  grep -qxF "$(readlink -f "$f")" /tmp/ld.keep || rm -f "$f"
done
find "$OUT/runtime/$LIBDIR" -maxdepth 1 -xtype l -delete   # links left without a target
echo "slim down: $BEFORE kB -> $(du -sk "$OUT" | cut -f1) kB"
du -sh "$OUT/app" "$OUT/runtime"
