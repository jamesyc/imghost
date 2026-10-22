#!/bin/sh
set -eu

python /tmp/native_sources.py fetch /tmp/native-versions.json

cd /tmp/native/curl
./configure --prefix=/usr/local --with-openssl --with-zlib --without-libpsl --disable-static
make -j "$(nproc)"
make install

cd /tmp/native/cairo
meson setup build --prefix=/usr/local --libdir=lib -Dtests=disabled -Dgtk_doc=false -Dglib=disabled -Dxlib=disabled -Dxcb=disabled
meson compile -C build
meson install -C build

cd /tmp/native/ffmpeg
./configure --prefix=/usr/local --disable-doc --disable-debug --disable-ffplay --disable-network \
    --enable-gpl --enable-libx264 --enable-libwebp --enable-libvpx --enable-libopus --enable-libdav1d
make -j "$(nproc)"
make install
