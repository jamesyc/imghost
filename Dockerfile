FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV IMGHOST_UID=10001
ENV IMGHOST_GID=10001

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        libvips42 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid "${IMGHOST_GID}" imghost \
    && useradd --system --uid "${IMGHOST_UID}" --gid "${IMGHOST_GID}" --create-home --home-dir /home/imghost imghost

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

RUN pip install --no-cache-dir .

COPY db /app/db
COPY docker/entrypoints /app/scripts

RUN mkdir -p /app/data \
    && chown -R imghost:imghost /app

USER imghost

CMD ["python", "-m", "uvicorn", "imghost.main:app", "--host", "0.0.0.0", "--port", "8000"]
