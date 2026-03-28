# App Structure Overview

This directory documents the current shipped structure of the app.

## Stack

- FastAPI application with server-rendered Jinja templates
- Shared first-party CSS in [`src/imghost/static/css/base.css`](/home/james/imghost/src/imghost/static/css/base.css)
- Page-specific vanilla JavaScript modules in [`src/imghost/static/js`](/home/james/imghost/src/imghost/static/js)
- PostgreSQL-backed persistence behind repository and service layers
- Object storage and background media processing coordinated through app state

## High-Level Layout

- [`src/imghost/main.py`](/home/james/imghost/src/imghost/main.py): app startup, middleware, static mount, router registration
- [`src/imghost/app_state.py`](/home/james/imghost/src/imghost/app_state.py): process-level wiring for settings, repository, storage, service layer, runtime config, audit, and app/worker/scheduler process roles
- [`src/imghost/config.py`](/home/james/imghost/src/imghost/config.py): environment-driven settings and deployment configuration
- [`src/imghost/service.py`](/home/james/imghost/src/imghost/service.py): core application workflows for uploads, albums, users, auth, admin actions, and destructive operations
- [`src/imghost/repositories.py`](/home/james/imghost/src/imghost/repositories.py): persistence access for albums, media, users, API keys, sessions, and runtime state
- [`src/imghost/storage.py`](/home/james/imghost/src/imghost/storage.py): object storage reads and writes
- [`src/imghost/processors.py`](/home/james/imghost/src/imghost/processors.py): media processing helpers
- [`src/imghost/tasks.py`](/home/james/imghost/src/imghost/tasks.py): background work entrypoints
- [`src/imghost/telemetry`](/home/james/imghost/src/imghost/telemetry): audit, metrics, telemetry state, and sinks
- [`src/imghost/web`](/home/james/imghost/src/imghost/web): HTTP routing, auth/session helpers, template rendering helpers, and page payload shaping

## Web Layer Split

- [`src/imghost/web/pages.py`](/home/james/imghost/src/imghost/web/pages.py): HTML page routes
- [`src/imghost/web/auth.py`](/home/james/imghost/src/imghost/web/auth.py): session login, registration, and logout APIs
- [`src/imghost/web/public_api.py`](/home/james/imghost/src/imghost/web/public_api.py): upload, public album reads, token-backed album management, and media deletion
- [`src/imghost/web/user_api.py`](/home/james/imghost/src/imghost/web/user_api.py): authenticated user APIs such as profile, albums, password, API key, ShareX config, and account deletion
- [`src/imghost/web/admin_api.py`](/home/james/imghost/src/imghost/web/admin_api.py): admin user, album, config, and audit APIs
- [`src/imghost/web/media.py`](/home/james/imghost/src/imghost/web/media.py): raw media and thumbnail streaming
- [`src/imghost/web/health.py`](/home/james/imghost/src/imghost/web/health.py): liveness and readiness endpoints

## Frontend Shape

- Shared shell in [`src/imghost/templates/base.html`](/home/james/imghost/src/imghost/templates/base.html)
- Shared nav and partials in [`src/imghost/templates/partials`](/home/james/imghost/src/imghost/templates/partials)
- Page templates in [`src/imghost/templates/pages`](/home/james/imghost/src/imghost/templates/pages)
- JS entrypoints generally align one-to-one with page surfaces, with shared helpers for album cards, auth, upload behavior, and admin common behavior

## Current Product Surfaces

- Public landing, auth, public album, and public user pages
- Signed-in dashboard, album list, owner album workspace, and settings
- Token-backed anonymous album management via the same workspace shell
- Admin overview, user management, user detail, album moderation, config, and ops pages
- Prometheus metrics and optional Google OAuth flows

## Notes Versus Older Planning Docs

- The app is already firmly on the template plus vanilla-JS architecture; HTMX-centric planning is obsolete.
- Some implementation details now exceed older route/UX docs, including the dedicated admin user detail page and more explicit page bootstrap helpers.
- Current-state behavior should be documented from code and tests, not inferred from old migration plans.
