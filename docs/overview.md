# Overview

`imghost` is a self-hosted image and video hosting application built with FastAPI, PostgreSQL, optional Redis, and either local filesystem storage or Garage/S3-compatible object storage.

## Core capabilities

- Anonymous uploads into public albums
- Authenticated uploads into owned albums
- Public album JSON and HTML views
- Public per-user album listings
- Album mutation by owner, admin, or delete token as appropriate
- Browser sessions and API keys
- ShareX config export
- Admin APIs and utility UI
- Background thumbnail processing
- ZIP streaming for album downloads
- Health and runtime-status endpoints with degraded-mode readiness behavior

## Main moving parts

- Web app: FastAPI app in [`src/imghost/main.py`](/home/james/imghost/src/imghost/main.py)
- Database: PostgreSQL state via [`src/imghost/repositories.py`](/home/james/imghost/src/imghost/repositories.py)
- Storage: filesystem or S3-compatible backend via [`src/imghost/storage.py`](/home/james/imghost/src/imghost/storage.py)
- Background jobs: in-process async, sync, or Redis-backed task queue via [`src/imghost/tasks.py`](/home/james/imghost/src/imghost/tasks.py)
- Sessions: signed-cookie fallback with Redis-backed session records when available via [`src/imghost/sessions.py`](/home/james/imghost/src/imghost/sessions.py)
- Rate limits: in-memory fallback with optional Redis-backed counters via [`src/imghost/rate_limits.py`](/home/james/imghost/src/imghost/rate_limits.py)
- Shared JSON payload helpers via [`src/imghost/payloads.py`](/home/james/imghost/src/imghost/payloads.py)

## Deployment model

The project supports:

- App-only local development with a direct `DATABASE_URL`
- Docker Compose with app, worker, Postgres, Redis, Garage, and Garage bootstrap
- Mixed deployments where Postgres, Redis, or S3-compatible storage live on other hosts

See:

- [configuration.md](/home/james/imghost/docs/configuration.md)
- [docker-deployment.md](/home/james/imghost/docs/docker-deployment.md)
- [reverse-proxy.md](/home/james/imghost/docs/reverse-proxy.md)
