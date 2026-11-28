# Base image: the large parts that rarely change (WASM engine, qemu, ffmpeg, bridge). CI builds it once per
# content hash (hub/tests/base_tag.sh) and the app image starts FROM it, so an app update reuses these layers on the device.
# Build context is the repo root: docker build -f hub/base.Dockerfile .
# No RUN runs under emulation: the x86_64 pieces are cross-compiled or only COPYed.
ARG TARGETARCH

# Bridge: boo-cloud v0.2.2 + our patch, always built for x86_64. On an arm64 builder that is a cross build.
# Fetched by commit id (tag v0.2.2), so a moved tag cannot change the build.
FROM --platform=$BUILDPLATFORM rust:1.88-bookworm AS bridge
RUN apt-get update && apt-get install -y --no-install-recommends patch && rm -rf /var/lib/apt/lists/* \
 && rustup target add x86_64-unknown-linux-gnu
RUN git init -q /src && cd /src && git fetch -q --depth 1 https://github.com/estampo/boo-cloud 7a7860af9ad579152754e5e675493d1b8da76750 \
 && git checkout -q FETCH_HEAD
COPY hub/bridge/bridge.patch /tmp/
RUN cd /src/bridge && patch -p1 < /tmp/bridge.patch
# The patch's Rust tests (redact() keeps camera credentials out of logs, body limit, ...) run on the builder's own arch.
# camera tests t1..t11 (except t1b, t6) compile tests/fake_bambusource.c, which the patch does not contain: skipped until it does.
RUN cd /src/bridge && cargo test --locked -- \
    --skip camera::tests::t1_ --skip camera::tests::t2_ --skip camera::tests::t3_ --skip camera::tests::t4_ --skip camera::tests::t5_ \
    --skip camera::tests::t7_ --skip camera::tests::t8_ --skip camera::tests::t9_ --skip camera::tests::t10_ --skip camera::tests::t11_
RUN set -e; cd /src/bridge; \
    if [ "$(uname -m)" != x86_64 ]; then \
      apt-get update && apt-get install -y --no-install-recommends g++-x86-64-linux-gnu libc6-dev-amd64-cross; \
      export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc CC_x86_64_unknown_linux_gnu=x86_64-linux-gnu-gcc \
             CXX_x86_64_unknown_linux_gnu=x86_64-linux-gnu-g++ AR_x86_64_unknown_linux_gnu=x86_64-linux-gnu-ar; \
    fi; \
    cargo build --release --locked --target x86_64-unknown-linux-gnu; \
    mkdir /out && cp target/x86_64-unknown-linux-gnu/release/boocloud-bridge /out/ && cp /src/LICENSE /out/LICENSE \
 && readelf -h /out/boocloud-bridge | grep -q X86-64

# The cert the bridge needs comes from the vendor's AGPL slicer repo (URL in externals.env). The vendor's proprietary
# network plugin is NOT in the image: hub/bridge/plugin.py downloads it on the user's device at app start (pinned SHA-256).
FROM debian:trixie-slim AS cert
COPY hub/externals.env /externals.env
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN . /externals.env && mkdir /out && curl -fsSL -o /out/slicer_base64.cer "$CERT_URL" \
 && echo "$CERT_SHA256  /out/slicer_base64.cer" | sha256sum -c -

# OrcaSlicer compiled to WebAssembly (Muon3D Slicer Engine, AGPL-3.0) for slicing in the browser, with the presets flattened
# at build time. The output is the same on every arch, so it is built natively on the builder. Only /out/engine is used.
FROM --platform=$BUILDPLATFORM node:26-bookworm AS engine
COPY hub/slicer/engine/ /w/hub/slicer/engine/
COPY hub/externals.py hub/externals.env /w/hub/
RUN /w/hub/slicer/engine/build.sh /out

# ffmpeg with only what hub/bridge/server.py runs: raw H.264 and MPEG-TS in, MJPEG stills and MPEG-TS out. The Debian
# package pulls ~400 MB of codecs and GPU libraries. Built natively on the builder of each arch.
# hevc is only there because libavcodec 7.1 links the H.264 SEI code against the HEVC one (aom_film_grain.o).
FROM debian:trixie-slim AS ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git ca-certificates && rm -rf /var/lib/apt/lists/*
# Commit id of tag n7.1.1.
RUN git init -q /src && cd /src && git fetch -q --depth 1 https://github.com/FFmpeg/FFmpeg db69d06eeeab4f46da15030a80d539efb4503ca8 \
 && git checkout -q FETCH_HEAD \
 && ./configure --prefix=/out --disable-everything --disable-autodetect --disable-doc --disable-debug --disable-network \
      --disable-ffplay --disable-ffprobe --disable-x86asm --enable-small --enable-static --disable-shared \
      --enable-protocol=pipe,fd,file --enable-demuxer=h264,mpegts --enable-muxer=mpegts,image2 --enable-parser=h264 \
      --enable-decoder=h264,hevc --enable-encoder=mjpeg --enable-bsf=null,h264_mp4toannexb,extract_extradata \
      --enable-filter=buffer,buffersink,scale,format,null,fps \
 && make -j"$(nproc)" install && strip /out/bin/ffmpeg && /out/bin/ffmpeg -hide_banner -version | head -1

# Only the x86_64 user emulator; the Debian qemu-user package has ~30 CPUs (460 MB).
FROM debian:trixie-slim AS qemu
RUN apt-get update && apt-get install -y --no-install-recommends qemu-user-static && rm -rf /var/lib/apt/lists/*

FROM debian:trixie-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pil ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /out/bin/ffmpeg /usr/local/bin/ffmpeg

# aarch64: run the x86_64 bridge and plugin under qemu in an x86_64 rootfs (COPY only, the Pi has no amd64 binfmt).
# The plugin libs land in /amd64/usr/lib at app start (hub/plugin.py).
# glibc 2.34 is the highest symbol needed by bridge and plugin, so bookworm (2.36) suffices.
FROM --platform=linux/amd64 gcr.io/distroless/cc-debian12 AS amd64root
FROM --platform=linux/amd64 debian:bookworm-slim AS amd64libs
FROM base AS arch-arm64
COPY --from=qemu /usr/bin/qemu-x86_64-static /usr/bin/qemu-x86_64
COPY --from=amd64root / /amd64/
# liblzma/libz are dependencies of the plugin that distroless lacks.
COPY --from=amd64libs /usr/lib/x86_64-linux-gnu/liblzma.so.5 /usr/lib/x86_64-linux-gnu/libz.so.1 /amd64/usr/lib/x86_64-linux-gnu/
COPY --from=bridge /out/boocloud-bridge /amd64/usr/local/bin/boocloud-bridge
# qemu -L only redirects paths that exist in the prefix, so pre-create the plugin's working dirs.
COPY hub/externals.env /opt/bridge/externals.env
COPY --from=cert /out/slicer_base64.cer /tmp/cert/
# The ELF interpreter symlink is dropped by COPY from distroless, and it must be relative to resolve inside /amd64.
# qemu does not apply -L to statx (used by Rust is_file), so the bridge's existence check needs host-side links (made by plugin.py).
RUN . /opt/bridge/externals.env && mkdir -p /amd64$PLUGIN_AGENT_DIR/log /amd64$PLUGIN_AGENT_DIR/config /amd64$PLUGIN_AGENT_DIR/cert /amd64/lib64 \
 && cp /tmp/cert/slicer_base64.cer /amd64$PLUGIN_AGENT_DIR/cert/ && rm -r /tmp/cert \
 && ln -sf ../lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 /amd64/lib64/ld-linux-x86-64.so.2
# plugin.py creates the host-side links to the plugin libs (a link named like a vendor lib must not be baked in).
ENV DW_PLUGIN_LINK_DIR=/amd64/usr/lib DW_PLUGIN_HOST_LINK_DIR=/usr/lib

# amd64: native.
FROM base AS arch-amd64
RUN apt-get update && apt-get install -y --no-install-recommends libssl3t64 libstdc++6 liblzma5 zlib1g && rm -rf /var/lib/apt/lists/*
COPY --from=bridge /out/boocloud-bridge /opt/bridge/boocloud-bridge
RUN mkdir -p /opt/bridge/lib
ENV DW_PLUGIN_LINK_DIR=/opt/bridge/lib

FROM arch-${TARGETARCH}
COPY --from=cert /out/slicer_base64.cer /opt/bridge/
COPY --from=bridge /out/LICENSE /usr/share/licenses/boo-cloud/LICENSE
COPY --from=engine /out/engine /srv/engine
