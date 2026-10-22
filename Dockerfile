FROM --platform=$BUILDPLATFORM python:3.15-slim-trixie AS native-versions

COPY docker/native_sources.py /tmp/native_sources.py
RUN python /tmp/native_sources.py resolve > /native-versions.json

FROM python:3.15-slim-trixie AS native

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config nasm xz-utils gnupg meson ninja-build \
        libssl-dev zlib1g-dev libx264-dev libwebp-dev libvpx-dev libopus-dev libdav1d-dev \
        libpixman-1-dev libfreetype-dev libfontconfig-dev libpng-dev \
    && rm -rf /var/lib/apt/lists/*
COPY docker/native_sources.py /tmp/native_sources.py
COPY docker/build-native.sh /tmp/build-native.sh
COPY --from=native-versions /native-versions.json /tmp/native-versions.json
RUN sh /tmp/build-native.sh

FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.15-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV IMGHOST_UID=10001
ENV IMGHOST_GID=10001
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libssl3t64 zlib1g libx264-164 libwebp7 libwebpmux3 libwebpdemux2 \
        libvpx9 libopus0 libdav1d7 libpixman-1-0 libfreetype6 libfontconfig1 libpng16-16t64 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=native /usr/local/bin/curl /usr/local/bin/curl
COPY --from=native /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/
COPY --from=native /usr/local/lib/libcurl.so* /usr/local/lib/
COPY --from=native /usr/local/lib/libcairo*.so* /usr/local/lib/
COPY --from=native /usr/local/share/imghost-native-versions.json /usr/local/share/imghost-native-versions.json
COPY --from=uv /uv /usr/local/bin/uv
RUN ldconfig

RUN groupadd --system --gid "${IMGHOST_GID}" imghost \
    && useradd --system --uid "${IMGHOST_UID}" --gid "${IMGHOST_GID}" --create-home --home-dir /home/imghost imghost

COPY pyproject.toml uv.lock /app/
COPY src /app/src

RUN uv sync --locked --no-dev --no-editable --no-cache

COPY db /app/db
COPY docker/entrypoints /app/scripts

RUN mkdir -p /app/data \
    && chown -R imghost:imghost /app

USER imghost

CMD ["python", "-m", "uvicorn", "imghost.main:app", "--host", "0.0.0.0", "--port", "8000"]
