FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ffmpeg \
        libvips42 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY src /app/src

RUN pip install --no-cache-dir .

COPY db /app/db
COPY docker/entrypoints /app/scripts

CMD ["python", "-m", "uvicorn", "imghost.main:app", "--host", "0.0.0.0", "--port", "8000"]
