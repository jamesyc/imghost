# DESIGN2

This document is the updated design snapshot for the current state of the project. It is intentionally much shorter than [`DESIGN.md`](/home/james/imghost/DESIGN.md) and focuses on the architecture that actually exists now, plus the direction it should evolve.

## Goals

- Keep the current prototype useful and operable.
- Preserve clean public media URLs.
- Support both anonymous and authenticated usage.
- Keep deployment simple for one-machine self-hosting.
- Leave room to grow into a more production-ready multi-service setup later.

## Current Architecture

Today the application is a single FastAPI service that owns:

- HTTP API
- public HTML pages
- browser session auth
- API-key auth
- upload orchestration
- media processing dispatch
- thumbnail background work
- admin APIs
- runtime config APIs
- audit persistence

Backing services:

- PostgreSQL
- filesystem storage or Garage/S3-compatible object storage

Current queue/session/rate-limit model:

- in-process task queue
- signed cookie browser sessions
- in-process rate limiting

That is acceptable for a prototype and a single deployment unit, but it is not the final scaling model.

## URL Model

Public absolute URLs should be generated from the active request origin when possible.

Priority:

1. `X-Forwarded-Proto` + `X-Forwarded-Host`
2. direct request scheme/host
3. `BASE_URL` fallback

This allows a single deployment to respond correctly for multiple public domains while still retaining a fallback/default origin for non-request contexts.

## Auth Model

Two auth modes are first-class:

- browser session auth
- bearer API key auth

Local auth supports:

- registration
- login by username or email
- logout
- password change

Passwords are bcrypt-hashed.

Sessions are currently signed cookies. That is fine for now, but if the app needs stronger invalidation semantics or horizontal scale, sessions should move to server-side storage.

## Album Ownership Model

There are two album classes:

- anonymous/public albums:
  - `delete_token` based mutation
  - expiry by default
- authenticated owned albums:
  - owner-managed
  - no default expiry
  - no `delete_token`

Mutation policy:

- owner or admin can manage owned albums
- valid `delete_token` can manage anonymous albums

Authenticated uploads may:

- create a new owned album
- upload multiple files into one owned album
- append to an existing owned album they own

## Storage Model

Application metadata lives in PostgreSQL.

Binary media lives in one storage backend:

- local filesystem
- Garage / S3-compatible object storage

The app proxies media bytes so public URLs stay stable and storage implementation details are never exposed as the canonical user-facing URL.

## Browser UI Philosophy

The current UI is intentionally a utility UI, not a finished product UI.

Its purpose is to:

- prove backend behavior
- test flows in a real browser
- cover auth, upload, album, and admin behavior end-to-end

It should eventually be replaced, not endlessly polished in place.

## Deployment Model

### Current Recommended Prototype Deployment

- one FastAPI app process
- one PostgreSQL instance
- one Garage/S3 backend or filesystem backend
- optional reverse proxy for HTTPS

### Current Docker Shape

The Docker setup under [`docker/`](/home/james/imghost/docker) is now the active local deployment path.

The app container can talk to:

- local Compose Postgres/Garage by default
- remote Postgres/Garage if env vars are overridden

That is enough for lightweight split-host deployments.

## What Should Change Next

If the project continues toward a more production-oriented design, the next architectural step should be:

1. externalize sessions, rate limits, and task dispatch
2. add trusted origin/host validation
3. improve observability
4. replace the utility UI

## What Should Not Change

- clean stable public media URLs
- PostgreSQL as source of truth for app state
- pluggable storage backend abstraction
- explicit distinction between anonymous and authenticated album ownership
