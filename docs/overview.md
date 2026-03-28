# Overview

`imghost` is a self-hosted image and video hosting application built with FastAPI, PostgreSQL, optional Redis, and either local filesystem storage or Garage/S3-compatible object storage.

## Core capabilities

- Anonymous uploads into public albums
- Authenticated uploads into owned albums
- Public album JSON and HTML views
- Public per-user album listings
- Album mutation by owner, admin, or delete token as appropriate
- Browser sessions and API keys
- Optional Google OAuth sign-in/linking
- ShareX config export
- Admin APIs and utility UI
- Background thumbnail processing
- ZIP streaming for album downloads
- Prometheus-format metrics
- PWA manifest and service worker endpoints
- Health and runtime-status endpoints with degraded-mode readiness behavior

## Main moving parts

- App bootstrap: FastAPI app wiring in [`src/imghost/main.py`](/home/james/imghost/src/imghost/main.py)
- Route modules: focused web routes in [`src/imghost/web`](/home/james/imghost/src/imghost/web)
- Database: PostgreSQL state via [`src/imghost/repositories.py`](/home/james/imghost/src/imghost/repositories.py)
- Storage: filesystem or S3-compatible backend via [`src/imghost/storage.py`](/home/james/imghost/src/imghost/storage.py)
- Background jobs: in-process async, sync, or Redis-backed task queue via [`src/imghost/tasks.py`](/home/james/imghost/src/imghost/tasks.py)
- Sessions: signed-cookie fallback with Redis-backed session records when available via [`src/imghost/sessions.py`](/home/james/imghost/src/imghost/sessions.py)
- Rate limits: in-memory fallback with optional Redis-backed counters via [`src/imghost/rate_limits.py`](/home/james/imghost/src/imghost/rate_limits.py)
- Shared JSON payload helpers via [`src/imghost/payloads.py`](/home/james/imghost/src/imghost/payloads.py)
- Page/bootstrap view shaping via [`src/imghost/web/page_views.py`](/home/james/imghost/src/imghost/web/page_views.py)
- Shared page/API pagination validation via [`src/imghost/web/pagination.py`](/home/james/imghost/src/imghost/web/pagination.py)

## Web structure

The web layer is intentionally split by responsibility:

- [`src/imghost/web/auth_context.py`](/home/james/imghost/src/imghost/web/auth_context.py): auth/session resolution and page/admin guards
- [`src/imghost/web/csrf.py`](/home/james/imghost/src/imghost/web/csrf.py): browser-session CSRF enforcement
- [`src/imghost/web/page_context.py`](/home/james/imghost/src/imghost/web/page_context.py): template rendering and shared page context
- [`src/imghost/web/request_context.py`](/home/james/imghost/src/imghost/web/request_context.py): request/app-state helpers
- [`src/imghost/web/pages.py`](/home/james/imghost/src/imghost/web/pages.py): browser page routes
- [`src/imghost/web/auth.py`](/home/james/imghost/src/imghost/web/auth.py): auth API routes
- [`src/imghost/web/public_api.py`](/home/james/imghost/src/imghost/web/public_api.py): upload/album/media routes
- [`src/imghost/web/user_api.py`](/home/james/imghost/src/imghost/web/user_api.py): current-user API routes
- [`src/imghost/web/admin_api.py`](/home/james/imghost/src/imghost/web/admin_api.py): admin API routes
- [`src/imghost/web/media.py`](/home/james/imghost/src/imghost/web/media.py): media-serving routes
- [`src/imghost/web/health.py`](/home/james/imghost/src/imghost/web/health.py): liveness/readiness routes

## Deployment model

The project supports:

- App-only local development with a direct `DATABASE_URL`
- Beginner Docker Compose with app, Postgres, Garage, and Garage bootstrap
- Advanced Docker Compose with app, split workers, scheduler, Postgres, Redis, Garage, and Garage bootstrap
- Mixed deployments where Postgres, Redis, or S3-compatible storage live on other hosts

See:

- [configuration.md](/home/james/imghost/docs/configuration.md)
- [docker-deployment.md](/home/james/imghost/docs/docker-deployment.md)
- [reverse-proxy.md](/home/james/imghost/docs/reverse-proxy.md)
