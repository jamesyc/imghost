FROM dxflrs/garage:v2.2.0 AS garage

FROM alpine:3.20

COPY --from=garage /garage /garage
COPY docker/garage/init.sh /garage-init.sh

RUN chmod +x /garage-init.sh

ENTRYPOINT ["/bin/sh", "/garage-init.sh"]
