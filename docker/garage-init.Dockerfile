FROM --platform=$BUILDPLATFORM python:3.15-alpine AS garage
ARG TARGETARCH
COPY docker/native_sources.py /tmp/native_sources.py
RUN python /tmp/native_sources.py "$TARGETARCH"

FROM alpine:3

COPY --from=garage /garage /garage
COPY docker/garage/init.sh /garage-init.sh

RUN chmod +x /garage-init.sh

ENTRYPOINT ["/bin/sh", "/garage-init.sh"]
