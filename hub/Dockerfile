# App image: only our code on top of the base image (hub/base.Dockerfile). Build context is the repo root.
# docker build -f hub/Dockerfile --build-arg BASE=<base image> . ; hub/tests/build_image.sh builds the base first.
ARG BASE=druckwerk-base:local

# One stage collects the app files, so the app is a single layer on top of the base.
FROM scratch AS app
COPY hub/bridge/server.py hub/bridge/plugin.py hub/bridge/bridge.sh hub/externals.py hub/externals.env /opt/bridge/
COPY hub/slicer/slice.sh hub/slicer/bs.py hub/externals.py hub/externals.env /opt/bs/bin/
COPY hub/slicer/server.py /srv/slicer_server.py
COPY hub/slicer/pack.py /srv/
COPY hub/externals.py /srv/
COPY hub/hub.py /srv/
COPY hub/ui/index.html hub/ui/style.css hub/ui/app.js hub/ui/api.js hub/ui/util.js hub/ui/feedback.js /srv/ui/
COPY hub/ui/views /srv/ui/views
COPY hub/ui/vendor/three.module.min.js /srv/ui/vendor/
COPY hub/ui/vendor/LICENSE-three.txt /usr/share/licenses/three/LICENSE
COPY hub/app.py hub/actions.py hub/ha.py hub/filament.py hub/history.py hub/printer_state.py hub/queue/queue_*.py /srv/
COPY hub/entities.py hub/files.py hub/gcode_guard.py hub/integration.py hub/notify.py hub/preview.py hub/sender.py hub/watchdog.py hub/offline.jpg /srv/
COPY hub/run.sh /run.sh

FROM ${BASE}
ARG VERSION=dev
ARG HASS_ARCH=unknown
COPY --from=app / /
ENV DW_DAEMON_CMD=/opt/bridge/bridge.sh DW_CERT=/opt/bridge/slicer_base64.cer \
    SLICER_SENDER_URL=http://127.0.0.1:8098 SLICER_PORT=8099 SLICER_ENGINE=/srv/engine \
    BS_APP=/opt/bs SLICER_SLICE_CMD=/opt/bs/bin/slice.sh SLICER_BS=/opt/bs/bin/bs.py SLICER_PROFILES=/opt/bs/resources/profiles
LABEL io.hass.type="app" io.hass.name="Druckwerk" io.hass.version="$VERSION" io.hass.arch="$HASS_ARCH" \
      org.opencontainers.image.source="https://github.com/marionebl/druckwerk"
CMD ["/run.sh"]
