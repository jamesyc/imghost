# imghost — Planning Document

> Self-hosted imgur-style image & video host. FastAPI + PostgreSQL + Redis + Garage (S3) + Docker Compose.
>
> Designed for single-node self-hosting (including Raspberry Pi) with a clear upgrade path to managed cloud infrastructure at web scale.

-----

## Table of Contents

1. [Project Overview](#1-project-overview)
1. [Technology Stack](#2-technology-stack)
1. [URL Design](#3-url-design)
1. [Authentication & Authorization](#4-authentication--authorization)
1. [Media Handling](#5-media-handling)
1. [Albums](#6-albums)
1. [Storage Architecture](#7-storage-architecture)
1. [CDN & Edge Caching](#8-cdn--edge-caching)
1. [Media Serving & Streaming](#9-media-serving--streaming)
1. [Rate Limiting & Quotas](#10-rate-limiting--quotas)
1. [User Features](#11-user-features)
1. [Admin Features](#12-admin-features)
1. [ShareX Integration](#13-sharex-integration)
1. [API Design](#14-api-design)
1. [Configuration System](#15-configuration-system)
1. [Database Schema](#16-database-schema)
1. [Background Jobs & Pruning](#17-background-jobs--pruning)
1. [Abstraction Layers](#18-abstraction-layers)
1. [Observability & Metrics](#19-observability--metrics)
1. [Deployment](#20-deployment)
1. [Security Considerations](#21-security-considerations)
1. [Idempotency & Reliability](#22-idempotency--reliability)
1. [Graceful Degradation](#23-graceful-degradation)
1. [Scaling Path](#24-scaling-path)
1. [Out of Scope](#25-out-of-scope)
1. [Future Work](#26-future-work)

-----

## 1. Project Overview

**imghost** is a self-hosted image and video sharing service with an emphasis on frictionless upload. The core UX loop is:

1. Type `img<tab>` in the browser address bar (via browser keyword search shortcut)
1. Press Enter → navigate to the upload page
1. `Ctrl+V` to paste, drag-and-drop, or use the file picker
1. Get a shareable album link immediately

The design philosophy is **"public but obscure by ID"** — like imgur, content is accessible by anyone with the link but is not indexed or browsable without it. There is no discovery or explore page.

### Architecture Philosophy

The application is designed for **single-node self-hosting first** — including Raspberry Pi deployments where Redis may not be available — but every architectural decision is made with a clear upgrade path to managed cloud infrastructure. The codebase is organized around several key principles:

- **Thin abstraction interfaces** (storage, task queue, metrics, audit, repositories) isolate business logic from infrastructure. Scaling up means swapping implementations, not rewriting handlers. See [§18 Abstraction Layers](#18-abstraction-layers).
- **Internal event bus** decouples "what happened" from "what should happen because of it." Upload handlers emit events; side effects (thumbnails, audit, metrics, quota) are listeners. See [§18 Event Bus](#event-bus).
- **Correlation IDs** flow through every request, across process boundaries into background workers, making log tracing trivial. See [§19 Correlation IDs](#correlation-ids).
- **Repository pattern** keeps SQLAlchemy confined to repository files. Handlers are declarative and testable without a database. See [§18 Repositories](#repositories).
- **MediaProcessor pipeline** handles format-specific logic (validation, sanitization, metadata extraction, thumbnailing) through a registry of per-format processors rather than branching if/else chains. See [§18 MediaProcessor Pipeline](#mediaprocessor-pipeline).
- **Redis is optional.** On resource-constrained deployments (Raspberry Pi), the application runs without Redis — sessions fall back to signed cookies, rate limiting disables, and tasks run synchronously in-process. See [§23 Graceful Degradation](#23-graceful-degradation).

### Key Non-Goals

- No deduplication
- No private/password-protected albums
- No video transcoding (serve originals, with browser-compat warnings where needed)
- No password reset flow (admin resets passwords manually)

-----

## 2. Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Language | Python 3.12+ | Modern, good async support |
| Web framework | FastAPI | Async, typed, easy to reason about |
| Database | PostgreSQL 16 | MVCC, no write-lock contention with multiple workers |
| Connection pooling | PgBouncer | Pools N uvicorn workers × connections down to ~5 real PG connections. **Transaction mode required;** asyncpg's prepared statement cache must be disabled (see [§15](#15-configuration-system)) |
| ORM / migrations | SQLAlchemy 2.x (async) + Alembic | Standard, well-supported; confined to repository layer |
| Cache / sessions / rate limiting | Redis 7 (optional) | Session store, rate limit counters, task queue backend. Optional — see [§23](#23-graceful-degradation) |
| Object storage | Garage (S3-compatible) | Open source, single binary, S3 API; swappable to AWS S3/R2 via StorageBackend abstraction |
| Async S3 client | aiobotocore | Async boto3 wrapper; all storage operations go through StorageBackend |
| Async task queue | arq (Redis-backed) | Thumbnail generation and async post-upload processing; swappable via TaskQueue abstraction. Falls back to sync in-process without Redis |
| Image processing | Pillow + pyvips | Pillow for most formats; pyvips for HEIC/AVIF |
| Video processing | ffmpeg + ffprobe | Remux without transcode, thumbnail extraction, codec detection |
| Thumbnail format | WebP (animated), JPEG (static) | ffmpeg for video/GIF; Pillow for still images |
| Auth | FastAPI sessions + Authlib | Local password + pluggable SSO (Google first) |
| CDN (optional) | Cloudflare | Reverse-proxy CDN; zero code changes to enable. See [§8](#8-cdn--edge-caching) |
| Containerization | Docker Compose | App + Workers + PostgreSQL + PgBouncer + Redis + Garage |

### Managed Service Upgrade Path

Every infrastructure component is swappable to a managed equivalent without application code changes:

| Self-Hosted | Managed Equivalent | Migration Complexity |
|---|---|---|
| PostgreSQL (Docker) | RDS / Aurora / Cloud SQL | Config change (connection string) |
| Redis (Docker) | ElastiCache / Memorystore | Config change (connection string) |
| Garage | AWS S3 / Cloudflare R2 / GCS | Config change (endpoint + credentials) |
| arq (Redis-backed) | SQS + consumer / Celery | Swap TaskQueue implementation |
| No CDN | Cloudflare / CloudFront | DNS change + cache rules |

### Python Dependencies (Anticipated)

```
fastapi
uvicorn[standard]
sqlalchemy[asyncio]
alembic
asyncpg
redis[hiredis]
arq
aiobotocore
pillow
pyvips
authlib
httpx
python-multipart
python-jose[cryptography]
passlib[bcrypt]
itsdangerous           # Signed cookie sessions (Redis-free fallback)
aiofiles
typer                  # CLI commands
structlog              # Structured JSON logging
```

-----

## 3. URL Design

### ID Character Set

- **Alphabet:** `23456789abcdefghjkmnpqrstuvwxyz` (Base32-style, lowercase; strips `0oO1iIl`)
- **Case insensitive:** URLs normalized to lowercase on lookup
- **Album IDs:** 9 characters — `/a/xk7m2np4q`
- **Image/video IDs:** 12 characters — `/i/xk7m2np4q8wr.jpg`
- Length difference makes album vs. media links visually distinctive at a glance

### URL Structure

| Purpose | Pattern | Example |
|---|---|---|
| Album page | `/a/{albumId}` | `/a/xk7m2np4q` |
| Raw media (with extension) | `/i/{mediaId}.{ext}` | `/i/xk7m2np4q8wr.jpg` |
| Raw media (no extension) | `/i/{mediaId}` | `/i/xk7m2np4q8wr` |
| Thumbnail | `/t/{mediaId}.{ext}` | `/t/xk7m2np4q8wr.webp` |
| Thumbnail (no extension) | `/t/{mediaId}` | `/t/xk7m2np4q8wr` |
| User album list | `/u/{username}` | `/u/james` |
| Admin area | `/admin/*` | `/admin/users` |
| API | `/api/v1/*` | `/api/v1/upload` |
| Health checks | `/health/*` | `/health/live`, `/health/ready` |

### Media URL Routing (`/i/` and `/t/`)

The `/i/` prefix serves raw media. The `/t/` prefix serves thumbnails. In both cases:

- The route extracts the media ID by stripping everything after the last `.` (if present)
- The extension is **decorative** — the server always returns the correct `Content-Type` from the stored `mime_type`, regardless of what extension the URL contains
- `/i/xk7m2np4q8wr`, `/i/xk7m2np4q8wr.jpg`, and `/i/xk7m2np4q8wr.png` all resolve to the same file with the same content type
- The upload API response and ShareX config generate URLs with the correct extension derived from the stored format (JPEG → `.jpg`, PNG → `.png`, MP4 → `.mp4`, etc.)

**Why the extension matters for users:** Chat platforms (Discord, Slack, iMessage) and social media use the file extension as a hint for how to render link previews. `.jpg` gets an inline image preview; an extensionless URL often doesn't. The extension also makes URLs self-documenting — a user can tell at a glance whether a link is an image or a video.

**Deleted content:** Returns HTTP 404 immediately with no body and no tombstone page.

-----

## 4. Authentication & Authorization

### Auth Methods

A single account can have **both** local credentials and one or more SSO providers linked simultaneously. The account must always retain at least one login method — removing a password is blocked if no SSO is linked, and vice versa.

#### Local Auth

- Username + bcrypt-hashed password
- "Remember me" checkbox — **default: checked**
  - Checked: 30-day session
  - Unchecked: session ends on browser close
- Sessions stored in Redis as signed tokens (when Redis available)
- **Redis-free fallback:** Sessions stored as signed cookies via `itsdangerous`. Functional but no server-side revocation — logout clears the cookie but can't invalidate the token if stolen. Acceptable for single-user Pi deployments. See [§23](#23-graceful-degradation).

#### SSO (OAuth 2.0 / OIDC)

- **Phase 1:** Google OAuth 2.0
- Provider table is generic — adding GitHub, Discord, etc. requires only a new OAuth client config and one route; no schema changes
- OAuth-only accounts can set a password from the Settings page at any time
- "Connect Google" / "Disconnect Google" in Settings; Disconnect blocked unless a password is also set

### Roles

| Role | Capabilities |
|---|---|
| Anonymous | Upload with forced expiry, view public albums |
| User | Upload without expiry, manage own albums, change password, delete account |
| Admin | All of the above + admin panel |

Multiple admins are supported.

### First Admin — CLI

```bash
docker compose exec app python -m imghost create-admin --username alice --email alice@example.com
# Prompts for password interactively. Fails if username already exists.
```

### Session & Cookie Details

- Cookie flags: `httponly; secure; samesite=lax`
- Payload: `{user_id, created_at, expires_at}`, signed with `SECRET_KEY`
- **With Redis:** Redis key `session:{token_hash}` → `{user_id, ...}`. The Redis key TTL is set to match `expires_at` at the time of session creation — expired sessions are evicted automatically and never accumulate. Redis down → requests treated as anonymous (fail closed).
- **Without Redis:** Signed cookie only. No server-side session store. See [§23](#23-graceful-degradation).

### `allow_registration` Behavior

Controlled by the runtime config system (see [§15](#15-configuration-system)). Toggleable from admin UI without restart unless locked by env. When `false`, the registration route returns 403 and nav links are hidden.

-----

## 5. Media Handling

### Accepted Image Formats

| Format | Stored as | Notes |
|---|---|---|
| JPEG / JPG | JPEG | Strip private EXIF, keep orientation + safe tags |
| PNG | PNG | Same EXIF handling |
| GIF (static) | GIF | |
| GIF (animated) | GIF | Original preserved byte-for-byte |
| WebP (static/animated) | WebP | |
| HEIC / HEIF | JPEG | Converted via pyvips on upload |
| BMP | BMP | |
| AVIF | AVIF | pyvips + libavif required in Docker image |
| SVG | SVG | Sanitized before storage (strip scripts, external references) |
| TIFF | ❌ | Rejected — security surface area |
| Other | ❌ | Client-side JS modal: "This file type is not supported." |

### Accepted Video Formats

| Format | Stored as | Notes |
|---|---|---|
| MP4 | MP4 | Remux only (no transcode); strip metadata, preserve rotation |
| MOV | MOV | iPhone native; HEVC content served directly with compat warning |
| WEBM | WEBM | VP8/VP9; compat warning shown on Safari < 15 |
| Other video | ❌ | Rejected at upload |

**No transcoding.** Original file is stored and served as-is. ffmpeg is used only for metadata remux and thumbnail extraction.

### MediaProcessor Pipeline

Format-specific processing logic (validation, sanitization, metadata extraction, thumbnail generation) is handled through a **processor registry** rather than branching if/else chains. See [§18 MediaProcessor Pipeline](#mediaprocessor-pipeline) for the full interface.

Each supported format has a registered processor that implements the standard pipeline:

1. **Validate** — check magic bytes, dimensions, codec; reject if unsupported
2. **Extract metadata** — dimensions, duration, codec hint, animation detection
3. **Sanitize** — strip EXIF/metadata per policy, sanitize SVG
4. **Generate thumbnail** — format-appropriate thumbnail per the rules below

The upload handler doesn't know how to process a JPEG vs. an SVG vs. an MP4 — it identifies the format by magic bytes, looks up the registered processor, and calls the pipeline. Adding a new format means writing one processor and registering it; no existing code changes.

### EXIF / Metadata Policy

- **Strip:** GPS coordinates, device make/model, creation timestamps, software tags, owner info
- **Keep:** Orientation/rotation tag, color space, codec parameters
- **Images:** Pillow strips EXIF selectively and re-embeds safe tags
- **Video:** `ffmpeg -c copy -map_metadata -1` strips all global metadata; stream-level side-data (including iPhone display matrix for rotation) is preserved automatically by `-c copy`

### Decompression Bomb Protection

- Reject any file where pixel dimensions exceed **50 megapixels** (W × H > 50,000,000)
- Check performed **before any pixel decoding**, via header-only parse (in the processor's `validate` step)
- Applies to images (Pillow header read) and video (ffprobe before any processing)

### Thumbnail Generation — Async

Thumbnails are generated **asynchronously** after upload via the task queue (see [§17](#17-background-jobs--pruning)). The upload endpoint returns immediately once the file is stored to the storage backend and the DB row exists. The album page shows a loading placeholder for items with `thumb_status = pending` and polls until thumbnails are ready.

**Without Redis (Pi mode):** Thumbnails are generated synchronously in-process during the upload request. The user waits slightly longer, but no worker process is needed. See [§23](#23-graceful-degradation).

#### Static Images

- JPEG thumbnail, 375px wide, height proportional
- EXIF orientation applied before resize so output is correctly rotated

#### SVG

- Same rules as static images: rasterize, JPEG thumbnail, 375px wide, height proportional
- Rasterization performed after sanitization (scripts/external refs already stripped)

#### Animated GIF

1. Check original file size from storage backend metadata
1. **≤ 2MB:** Set `thumb_is_orig = true` — no separate thumb stored; thumbnail endpoint serves the original key directly
1. **> 2MB:** Generate animated WebP thumbnail at 375px wide via Pillow
1. If generated WebP ≥ original GIF size: discard WebP, fall back to `thumb_is_orig = true`

#### Video

1. `ffprobe` to get exact duration in seconds; store in `duration_secs` column
1. **< 1 second:** Generate single static JPEG frame; no animation
1. **≥ 1 second:** Evenly space 10 frames across the full duration:
   - `interval = duration / 10`
   - `ffmpeg -i input -vf "fps=1/{interval},scale=375:-1" -frames:v 10 -loop 0 thumb.webp`
1. If generated WebP exceeds a reasonable size threshold: fall back to single static JPEG at the 1-second mark
1. `VIDEO_THUMB_FRAMES` env var (default: 10)

#### Animated WebP

Same rules as animated GIF:

1. Check original file size from storage backend metadata
1. **≤ 2MB:** Set `thumb_is_orig = true` — thumbnail endpoint serves the original key directly
1. **> 2MB:** Generate animated WebP thumbnail at 375px wide via Pillow
1. If generated WebP ≥ original size: discard WebP, fall back to `thumb_is_orig = true`

### Browser Compatibility Warnings

Shown as a non-blocking informational banner on the album page (not a modal). Server renders the warning from the stored `codec_hint` column; JS also checks client-side:

- **HEVC:** `video.canPlayType('video/mp4; codecs="hev1"') === ''` → *"This video uses HEVC encoding and may not play in Firefox. Try Chrome or Safari."*
- **WebM on Safari:** `video.canPlayType('video/webm; codecs="vp9"') === ''` → *"This video may not play in older Safari. Try Chrome or Firefox."*

-----

## 6. Albums

### Structure

- Every upload creates or adds to an **album**
- Single file upload = album containing one item
- Multi-file paste/drop in one batch = **one album** containing all files (not N separate albums)
- Maximum **1000 items** per album
- Album cover = `cover_media_id` if set; otherwise falls back to first item by position

### Position Ordering

Items use **large-integer gap positions** (1000, 2000, 3000, …) so that inserting or reordering between two items requires updating only the moved item, not renumbering the entire album.

- **Append to end:** `max_position + 1000`
- **Insert between A and B:** `(A.position + B.position) / 2`, rounded to nearest integer
- **Gap collapse:** If the gap between two adjacent items falls below 2 (from extreme reorder churn), a rebalance pass renumbers all positions in that album with fresh 1000-step gaps

This means drag-to-reorder is a single `UPDATE media SET position = {new_pos} WHERE id = {id}` with no cascading updates to other rows.

### Album Cover

`albums.cover_media_id` is nullable. `null` means "use first item by position." Cover resolution is implemented in **one shared function** called everywhere album cover is needed — nothing hardcodes "grab item at index 0." The column is in the schema from day one; the UI to set it can be added later without any schema changes.

### Album Page (`/a/{albumId}`)

- Album title (editable by owner, inline)
- Created datetime + last edited datetime (shown only if the album has been edited after creation)
- All items rendered in position order: images as `<img>`, videos as `<video controls>`
- Right-clicking an image gives the browser's native "Copy image address" — resolves to `https://yourdomain.com/i/{mediaId}.jpg` (clean, permanent URL with correct extension; storage backend is completely invisible)
- Per-item **direct URL text box** at the bottom of each image for non-technical users to copy
- **Expiry banner** if `expires_at` is set: *"This album expires in X hours / X days"* — shown to all viewers including anonymous
- **Download as ZIP** button — streamed on the fly, no temp file. Public endpoint — no authentication required. Albums are already public; the ZIP is just a convenience packaging of content any visitor can already access individually.

### Album Ownership & Editing

Logged-in owners can:

- Edit album title (inline)
- Delete individual items from the album
- Add more items to an existing album (up to 1000-item limit)
- Set the album cover image
- Reorder items via drag-to-reorder
- Delete the entire album

### Expiry

- **Anonymous uploads:** Always expire. Default = 24 hours from upload (configurable via runtime config)
- **Authenticated uploads:** No expiry by default
- **Admin can:** Set or clear expiry on any album (e.g. rescue an anon upload, or force-expire a user album)
- Albums where `expires_at < NOW()` return 404 immediately on any request, before the pruning job runs

### User Album List (`/u/{username}`)

- All albums belonging to the user, sorted by **most recently modified** first
- Shows: cover thumbnail, title, item count, created date, total album size
- Publicly accessible (no private albums)

-----

## 7. Storage Architecture

### Decision: PostgreSQL (Metadata) + S3-Compatible Object Store (Blobs)

**All binary data (originals and thumbnails) is stored in the object store.** PostgreSQL stores all metadata. This is the correct split:

- Postgres BLOBs at media scale cause: `pg_dump` that includes all binary data (makes routine dumps unusable), expensive VACUUM on large dead tuples after deletes, WAL amplification (a 50MB image upload causes 100MB+ of I/O through WAL), and memory pressure on `shared_buffers` from large blob rows competing with index cache
- Image originals are 300KB–500MB — well above the ~100KB crossover where in-DB storage has any read-performance advantage
- S3-compatible stores handle byte-range requests natively at the object store level
- Independent backup: `pg_dump` (small, fast) + `rclone sync` (parallel, resumable, incremental)

### Default: Garage

MinIO's open source community edition is effectively abandoned in favor of a commercial product. Garage is:

- Developed by a non-profit (not VC-backed)
- AGPLv3 licensed, genuinely open source
- Single binary, single Docker container, minimal configuration
- S3-compatible for all operations imghost needs: PUT, GET, DELETE, multipart upload, range requests
- Designed specifically for self-hosted single-node and small multi-node deployments

### StorageBackend Abstraction

**All object store interactions go through a `StorageBackend` abstract interface.** Storage calls are never made directly from upload handlers, thumbnail workers, or the prune job — only through this interface. This constraint is enforced from day one. See [§18 Abstraction Layers](#18-abstraction-layers) for the full interface definition.

`GarageS3Backend` is the first implementation. Swapping to AWS S3, Cloudflare R2, or GCS requires only changing the endpoint URL and credentials — zero application code changes. A `LocalFilesystemBackend` can be added for dev-without-Docker.

### Object Key Layout

```
originals/{userId}/{mediaId}.{ext}    # authenticated uploads
originals/anon/{mediaId}.{ext}        # anonymous uploads
thumbnails/{mediaId}.jpg              # static image thumbnail
thumbnails/{mediaId}.webp             # animated thumbnail (GIF or video)
```

Keys are derived deterministically from the `mediaId` — no secondary DB lookup is required to construct a storage key.

-----

## 8. CDN & Edge Caching

### Overview

The application is designed to work with or without a CDN. Without a CDN, FastAPI proxies all media bytes directly (suitable for self-hosted / low-traffic deployments). With a CDN, the vast majority of media requests are served from edge cache and never reach the origin.

### Cloudflare Integration

Cloudflare operates as a reverse proxy — it sits between users and the origin server. Enabling it requires **zero code changes** because the application already emits correct cache headers on all media responses.

#### Setup

1. Add the domain to Cloudflare and change nameservers (or use CNAME setup to keep existing DNS)
2. Cloudflare automatically proxies all traffic through its edge network
3. Media endpoints (`/i/*`, `/t/*`) already return `Cache-Control: public, max-age=31536000, immutable` — Cloudflare caches these indefinitely on first request
4. HTML pages (album pages, upload page, admin) return `Cache-Control: no-cache` or `private` — Cloudflare passes these through to origin every time

#### Cache Behavior

| Endpoint Pattern | Cache-Control | CDN Behavior |
|---|---|---|
| `/i/{mediaId}.*` | `public, max-age=31536000, immutable` | Cached at edge indefinitely |
| `/t/{mediaId}.*` | `public, max-age=31536000, immutable` | Cached at edge indefinitely |
| `/a/{albumId}` | `no-cache` | Always passes to origin |
| `/api/*` | `no-cache` | Always passes to origin |
| `/admin/*` | `private, no-store` | Never cached |
| `/health/*` | `no-cache` | Always passes to origin |

#### Video Seeking (Range Requests)

Cloudflare caches 206 partial content responses correctly. Video seeking works through the CDN with no special configuration. The origin's `Accept-Ranges: bytes` header is preserved.

#### Upload Size Limit

Cloudflare's free tier enforces a 100MB request body limit on proxied traffic. This conflicts with the 500MB max video upload size. Two options:

- **Option A (recommended):** Use a separate upload subdomain (`upload.img.example.com`) that points directly to the origin, bypassing Cloudflare. Uploads are authenticated and infrequent relative to views, so this is a clean split. The main domain goes through Cloudflare for all reads.
- **Option B:** Upgrade to Cloudflare Pro (500MB limit) or Business.

The upload API response always returns URLs on the main CDN-fronted domain regardless of which upload path was used.

#### Origin Protection

When Cloudflare is enabled, restrict the origin server to only accept connections from Cloudflare's published IP ranges. This prevents users from bypassing the CDN. The nginx config can include Cloudflare's IP list (they publish it and it changes infrequently).

#### Rate Limiting with CDN

When behind Cloudflare, the real client IP is in the `CF-Connecting-IP` header instead of `X-Real-IP`. The application's rate limiting middleware should check for this header first and fall back to `X-Real-IP` / `X-Forwarded-For`. Cloudflare also offers its own WAF and rate limiting as an additional layer for DDoS protection that operates before traffic reaches the origin.

### Without CDN

The application functions identically without a CDN. FastAPI proxies all media bytes via the streaming proxy described in [§9](#9-media-serving--streaming). This is perfectly adequate for self-hosted deployments serving a small number of users.

-----

## 9. Media Serving & Streaming

### Why FastAPI Proxies (Not Presigned Redirect)

Presigned S3 URLs would expose `?X-Amz-Credential=...&X-Amz-Signature=abc123&X-Amz-Expires=3600` in the browser. Right-clicking an image on the album page would copy that presigned URL — breaking the core UX requirement of clean, permanent, right-clickable image links. FastAPI must proxy all media bytes so that `https://yourdomain.com/i/{mediaId}.jpg` is always the URL the browser sees.

This is not a meaningful performance problem at self-hosted scale. Media serving is I/O-bound, not CPU-bound. FastAPI's async streaming means one worker handles many concurrent downloads through `await`/`yield` without blocking threads. At web scale, the CDN ([§8](#8-cdn--edge-caching)) absorbs 95%+ of media requests before they reach the origin.

### Streaming Proxy Flow

```
Client → GET /i/{mediaId}.ext
       → FastAPI: extract mediaId, look up in DB (via MediaRepository), check expiry
       → Resolve to storage key
       → StorageBackend.get_stream(key, range_header) → storage backend
       → StreamingResponse to client with correct headers
```

All database lookups in the media serving path go through the `MediaRepository` — the handler never constructs SQL directly. See [§18 Repositories](#repositories).

Response headers set by FastAPI on every media response:

- `Content-Type` — from stored `mime_type`
- `Content-Length` — from stored `file_size`
- `Accept-Ranges: bytes` — always present
- `Cache-Control: public, max-age=31536000, immutable` — media IDs are permanent; safe to cache indefinitely (also enables CDN edge caching)
- `ETag: "{mediaId}"` — enables browser conditional requests

### Range Request Handling (Required for Video Seeking)

Range requests are a **first-class requirement, not an afterthought.** Without them, video players cannot seek — they must download the entire file before playback begins. This must be correct in the first working version.

Implementation:

1. Extract `Range:` header from the incoming client request (e.g. `bytes=1048576-2097151`)
1. Pass it to `StorageBackend.get_stream(key, range_header=...)`
1. The storage backend passes the range to the underlying store (Garage, S3, etc.)
1. Store returns `206 Partial Content` with a `Content-Range` header
1. FastAPI returns `StreamingResponse` with status 206 and the `Content-Range` header forwarded to the client

The nginx config forwards `Range` and `If-Range` headers (included in the shipped `nginx-site.conf`).

### Thumbnail Serving

Same streaming proxy path, via `/t/{mediaId}[.ext]`.

For items where `thumb_is_orig = true` (small animated GIFs, small animated WebPs), the thumbnail endpoint fetches the original file key directly.

**`thumb_url` extension for `thumb_is_orig` items:** When constructing the `thumb_url` in the upload API response, the server uses the original file's extension (e.g. `.gif`, `.webp`) rather than a thumbnail extension (`.webp`). This ensures that chat platforms (Discord, Slack) see the correct file extension hint and render inline previews correctly. The extension in the URL is decorative — the server always returns the correct `Content-Type` — but external platforms use it as a render hint before fetching.

For items with `thumb_status = pending`:

- Endpoint returns HTTP 202 with an empty body
- Album page JS polls `/t/{mediaId}` at a short interval until a 200 is returned
- Once `thumb_status = done`, the response includes standard cache headers and the browser stops polling

-----

## 10. Rate Limiting & Quotas

### Rate Limiting

All limit values live in Redis counters (when Redis is available). Configurable from admin UI unless locked by env. Rate limit key = `SHA256(IP + "|" + User-Agent)` for anon users; `user_id` for authenticated users. Hashed to avoid storing raw IPs.

**Without Redis:** Rate limiting is disabled entirely. Acceptable for single-user Pi deployments. See [§23](#23-graceful-degradation).

**CDN note:** When behind Cloudflare, the IP is extracted from `CF-Connecting-IP` instead of `X-Real-IP`. The rate limiting middleware checks for this header first and falls back gracefully.

#### Default Limits

| Scope | Metric | Default |
|---|---|---|
| Per anon IP+UA | Uploads per minute | 5 |
| Per anon IP+UA | Bytes per hour | 100 MB |
| Global all anon combined | Uploads per minute | 50 |
| Global all anon combined | Bytes per hour | 1 GB |
| Per logged-in user | Uploads per minute | 30 |
| Per logged-in user | Bytes per hour | 500 MB |

Per-user overrides are settable from the admin panel.

#### Redis Failure Behavior

Rate limiting **fails open** (uploads allowed without checking limits). Sessions **fail closed** (users logged out, or fall back to signed cookies if `REDIS_URL` is unset). During a Redis outage: anonymous uploads proceed without rate limiting; authenticated users are bounced to the login page (unless running in Redis-free mode, where signed cookie sessions are unaffected). Both failure modes are logged as warnings.

### Storage Quotas

| Scope | Default | Config key |
|---|---|---|
| Per user | 2 GB | `DEFAULT_USER_QUOTA_BYTES` |
| Server total | No limit | `SERVER_QUOTA_BYTES` (0 = disabled) |
| Max image file size | 50 MB | `MAX_IMAGE_SIZE_MB` |
| Max video file size | 500 MB | `MAX_VIDEO_SIZE_MB` |

Quota is calculated from `file_size + thumb_size` summed across all a user's media rows.

When the server quota is reached: **hard stop** — all uploads rejected with HTTP 507 until space is freed. The upload page shows a clear error message. Per-user quota exhaustion returns HTTP 413.

-----

## 11. User Features

### Registration & Login

- Register with username + email + password (when `allow_registration = true`)
- Register / login with Google OAuth
- "Remember me" (default: checked) → 30-day session; unchecked → session cookie (browser-close expiry)
- Post-login redirect to the page the user was on, or home

### Settings Page

- **Change password** (requires current password)
- **Connected accounts:** Shows linked SSO providers; "Connect Google"; "Disconnect" (only if a password is also set)
- **API Key:** Generate / revoke ShareX key (one active at a time); "Download ShareX Config" downloads `.sxcu`
- **Storage usage:** Used / quota displayed as a progress bar with byte values
- **Delete account:** Confirmation flow requiring password (or SSO re-auth)

### Account Deletion

- Deletes user row, all album rows, all media rows
- Deletes all storage objects (originals + thumbnails) via `StorageBackend.delete()` for each key
- Session invalidated immediately
- Emits `UserDeleted` event — listeners handle audit logging, metrics, session cleanup

### Password Change

- Form: current password + new password + confirmation
- No email required; no reset flow

-----

## 12. Admin Features

### User Management

- **List all users:** username, email, joined date, storage used, quota, status (active / suspended)
- **Create user** directly (username + email + password)
- **Reset user password** (admin sets new password; no email is sent — admin notifies out-of-band)
- **Suspend / unsuspend** (blocks login, preserves all data)
- **Delete user** (removes user + all their content + all storage objects)
- **Set per-user storage quota**
- **Set per-user rate limit overrides**
- **View user's album list** as a read-only view (admin cannot edit albums from this page)

### Album Management

- **List all albums:** owner, title, item count, size, created date, expiry status
- **Delete album** (cannot edit content)
- **Set or clear expiry** on any album (rescue anon uploads; force-expire user albums)

### Global Storage Dashboard

- Total storage used vs. server quota with visual progress bar
- Per-user breakdown table: username, file count, bytes used, % of their quota
- Total storage attributed to anonymous uploads

### Configuration Panel

- Toggle `allow_registration` (grayed out + padlock icon if env-locked)
- Toggle `anon_upload_enabled` (grayed out + padlock icon if env-locked)
- Configure `anon_expiry_hours` (grayed out + padlock icon if env-locked)
- Configure rate limits (grayed out + padlock icon if env-locked)
- All runtime config values displayed; locked values show tooltip: *"Locked by environment config."*

### Audit Log

- **Retention:** 90 days, then pruned by the daily prune job
- **Filterable by:** event type, user, date range
- Written through the `AuditWriter` abstraction (see [§18](#18-abstraction-layers)) for future migration to a dedicated log store
- Audit writes are triggered by **event bus listeners**, not by the handlers directly — see [§18 Event Bus](#event-bus)

| Event | Actor |
|---|---|
| Album created | user / anon |
| Images uploaded | user / anon |
| Image deleted from album | user / admin |
| Album deleted | user / admin |
| Album title changed | user |
| Album cover set | user |
| Items reordered | user |
| Album expiry set / cleared | admin |
| User password reset | admin |
| User created | admin |
| User suspended / unsuspended | admin |
| User deleted | admin |
| User deleted own account | user |
| Admin login | admin |
| Registration toggled | admin |
| Rate limit config changed | admin |

#### Audit Event Schema Convention

Each event type has a **documented set of expected fields** in the `metadata` JSONB column. This makes future migration to a structured log store (Elasticsearch, Loki, etc.) mechanical rather than requiring data cleanup.

| Event Type | Expected Metadata Fields |
|---|---|
| `album_created` | `album_id`, `item_count`, `source` ("web" / "api") |
| `media_uploaded` | `media_id`, `album_id`, `file_size`, `media_type`, `format`, `source` ("web" / "api") |
| `media_deleted` | `media_id`, `album_id`, `file_size` |
| `album_deleted` | `album_id`, `item_count`, `total_size` |
| `album_title_changed` | `album_id`, `old_title`, `new_title` |
| `album_cover_set` | `album_id`, `media_id` |
| `album_reordered` | `album_id` |
| `album_expiry_changed` | `album_id`, `old_expiry`, `new_expiry` |
| `user_password_reset` | `target_user_id` |
| `user_created` | `target_user_id`, `method` ("admin" / "registration" / "oauth") |
| `user_suspended` | `target_user_id`, `suspended` (bool) |
| `user_deleted` | `target_user_id`, `deleted_by` ("self" / "admin") |
| `admin_login` | (none) |
| `config_changed` | `key`, `old_value`, `new_value` |

All audit metadata includes a `correlation_id` field automatically, populated from the request context. See [§19 Correlation IDs](#correlation-ids).

-----

## 13. ShareX Integration

### How It Works

ShareX POSTs files to a custom uploader defined by a `.sxcu` config file. The user downloads this file from imghost Settings and double-clicks it to import into ShareX. From then on, screenshots are automatically uploaded to imghost and the resulting URL is placed on the clipboard.

### `.sxcu` Config (Generated Server-Side Per User)

```json
{
  "Version": "14.1.0",
  "Name": "imghost",
  "DestinationType": "ImageUploader, FileUploader",
  "RequestMethod": "POST",
  "RequestURL": "https://{DOMAIN}/api/v1/upload",
  "Headers": {
    "Authorization": "Bearer {API_KEY}"
  },
  "Body": "MultipartFormData",
  "FileFormName": "file",
  "URL": "$json:media_url$",
  "ThumbnailURL": "$json:thumb_url$",
  "DeletionURL": "$json:delete_url$"
}
```

ShareX sends a `GET` request to `DeletionURL`, so `delete_url` points to a dedicated `GET /api/v1/album/{albumId}/delete` endpoint that requires the same API key auth and deletes the album. This is separate from `DELETE /api/v1/album/{albumId}` (which uses the `DELETE` method for browser/programmatic clients).

### Upload Behavior via API Key

- `POST /api/v1/upload` with `Authorization: Bearer {apiKey}`
- One file per request (ShareX sends one at a time)
- Creates a **new single-item album** per upload
- Returns immediately (thumbnail generation is async):

```json
{
  "album_id":  "xk7m2np4q",
  "album_url": "https://example.com/a/xk7m2np4q",
  "media_id":  "xk7m2np4q8wr",
  "media_url": "https://example.com/i/xk7m2np4q8wr.jpg",
  "thumb_url": "https://example.com/t/xk7m2np4q8wr.jpg",
  "delete_url":"https://example.com/api/v1/album/xk7m2np4q",
  "expires_at": null
}
```

### API Key Details

- 32-char random token, shown to user **once only**, stored as SHA-256 hash in DB
- One active key per user; regenerating invalidates the previous one immediately
- `last_used_at` timestamp updated on each authenticated request

-----

## 14. API Design

All endpoints under `/api/v1/`. JSON in, JSON out. Auth via session cookie or `Authorization: Bearer {apiKey}` header.

All requests carry a **correlation ID** (see [§19](#19-observability--metrics)) — either from the `X-Correlation-ID` request header or auto-generated by middleware. The correlation ID is returned in the `X-Correlation-ID` response header on every response for client-side log correlation.

### Endpoints

#### Upload

```
POST /api/v1/upload
  multipart: file (required), album_id (optional — add to existing), title (optional)
  → { album_id, album_url, media_id, media_url, thumb_url, delete_url, expires_at }
```

#### Album

```
GET    /api/v1/album/{albumId}            album metadata + ordered item list
GET    /api/v1/album/{albumId}/delete     delete album via GET (ShareX DeletionURL — requires API key auth)
DELETE /api/v1/album/{albumId}            delete album (owner or admin)
PATCH  /api/v1/album/{albumId}            edit title, cover_media_id, expiry
GET    /api/v1/album/{albumId}/zip        stream ZIP download (public — no auth required)
PATCH  /api/v1/album/{albumId}/order      reorder items: [{media_id, position}, ...]
```

#### Media

```
DELETE /api/v1/media/{mediaId}            delete single item (owner or admin)
```

#### User

```
GET    /api/v1/user/me                    current user info + quota usage
POST   /api/v1/user/me/api-key            regenerate API key (returns raw key once)
DELETE /api/v1/user/me                    delete own account
PATCH  /api/v1/user/me/password           change password
```

#### Admin

```
GET    /api/v1/admin/users                list all users
POST   /api/v1/admin/users                create user
PATCH  /api/v1/admin/users/{userId}       suspend, set quota, reset password
DELETE /api/v1/admin/users/{userId}       delete user + all content
GET    /api/v1/admin/stats                global storage stats
GET    /api/v1/admin/audit                audit log (filterable)
PATCH  /api/v1/admin/config               update runtime config values
GET    /api/v1/admin/albums               list all albums
PATCH  /api/v1/admin/albums/{albumId}     set/clear expiry
DELETE /api/v1/admin/albums/{albumId}     delete album
```

#### Health

```
GET    /health/live                       liveness — always 200 if process is running
GET    /health/ready                      readiness — checks DB, Redis (if enabled), storage backend
```

-----

## 15. Configuration System

### Two-Tier Config

#### Tier 1 — Static (Environment / `.env`)

Set at deploy time. Requires restart to change. Used to lock runtime options.

```env
# Core
SECRET_KEY=
DATABASE_URL=postgresql+asyncpg://imghost:imghost@pgbouncer:5432/imghost?prepared_statement_cache_size=0
# ↑ prepared_statement_cache_size=0 is required — PgBouncer in transaction mode
#   does not support asyncpg's default prepared statement cache and will throw
#   "prepared statement already exists" errors under concurrent load without it.
REDIS_URL=redis://redis:6379/0        # empty or unset = Redis-free mode
BASE_URL=https://img.example.com
PORT=8000

# Storage (S3-compatible — works with Garage, AWS S3, R2, GCS)
S3_ENDPOINT_URL=http://garage:3900
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
S3_BUCKET=imghost
S3_REGION=garage              # Garage uses a dummy region value

# Upload domain (optional — for CDN bypass on uploads; see §8)
UPLOAD_BASE_URL=              # empty = same as BASE_URL

# Storage limits
MAX_IMAGE_SIZE_MB=50
MAX_VIDEO_SIZE_MB=500
DEFAULT_USER_QUOTA_BYTES=2147483648   # 2 GB
SERVER_QUOTA_BYTES=0                  # 0 = disabled

# Media
MAX_PIXEL_MEGAPIXELS=50
VIDEO_THUMB_FRAMES=10

# Anonymous uploads
ANON_EXPIRY_HOURS=24                  # default expiry for anonymous uploads; overridable at runtime

# Pruning
PRUNE_CRON=0 12 * * *                 # noon UTC ≈ 4am Pacific

# Logging & Metrics
LOG_LEVEL=INFO
LOG_FILE=                             # empty = stdout only
METRICS_BACKEND=log                   # 'log' | 'prometheus' (see §19)

# OAuth
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Locks — set to "true" to prevent admin UI from overriding
LOCK_ALLOW_REGISTRATION=false
LOCK_ANON_UPLOAD=false
LOCK_ANON_EXPIRY=false
LOCK_RATE_LIMITS=false
```

**Redis-free mode:** When `REDIS_URL` is empty or unset, the application starts without Redis. Sessions use signed cookies, rate limiting is disabled, and background tasks run synchronously in-process. See [§23 Graceful Degradation](#23-graceful-degradation).

#### Tier 2 — Runtime (PostgreSQL `config` Table)

Editable from admin UI, effective immediately, no restart needed.

Each key has three possible DB states:

| DB value | Meaning |
|---|---|
| `null` / missing | Use compiled-in default |
| `"true"` | Explicitly enabled |
| `"false"` | Explicitly disabled |

If the corresponding `LOCK_*` env var is `"true"`, the admin UI renders the control grayed out with a padlock icon and tooltip. The DB value is ignored and the env default applies.

**Runtime config keys:**

```
allow_registration              default: true      lock: LOCK_ALLOW_REGISTRATION
anon_upload_enabled             default: true      lock: LOCK_ANON_UPLOAD
anon_expiry_hours               default: from ANON_EXPIRY_HOURS env (24)  lock: LOCK_ANON_EXPIRY
rate_limit_anon_rpm             default: 5         lock: LOCK_RATE_LIMITS
rate_limit_anon_bph             default: 104857600      # 100 MB
rate_limit_global_anon_rpm      default: 50
rate_limit_global_anon_bph      default: 1073741824     # 1 GB
rate_limit_user_rpm             default: 30
rate_limit_user_bph             default: 524288000      # 500 MB
```

`anon_expiry_hours` is the only runtime config key that sources its compiled-in default from an env var (`ANON_EXPIRY_HOURS`). This lets operators set the value at deploy time without touching the DB, while still allowing runtime override via admin UI (unless `LOCK_ANON_EXPIRY=true`).

-----

## 16. Database Schema

### Core Tables

```sql
-- Users
users (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  username        TEXT UNIQUE NOT NULL,
  email           TEXT UNIQUE,
  password_hash   TEXT,                    -- null if SSO-only
  is_admin        BOOLEAN NOT NULL DEFAULT false,
  is_suspended    BOOLEAN NOT NULL DEFAULT false,
  quota_bytes     BIGINT,                  -- null = use DEFAULT_USER_QUOTA_BYTES
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)

-- SSO provider links (one row per linked provider per user)
user_sso_links (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users ON DELETE CASCADE,
  provider        TEXT NOT NULL,           -- 'google', 'github', etc.
  provider_uid    TEXT NOT NULL,           -- provider's stable user ID
  linked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (provider, provider_uid)
)

-- API keys (stored as SHA-256 hash; raw value shown to user once only)
api_keys (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES users ON DELETE CASCADE,
  key_hash        TEXT UNIQUE NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at    TIMESTAMPTZ
)

-- Albums
albums (
  id              TEXT PRIMARY KEY,        -- 9-char generated ID
  user_id         UUID REFERENCES users ON DELETE CASCADE,  -- null = anonymous
  title           TEXT,
  cover_media_id  TEXT,                    -- null = resolve to first item by position
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ              -- null = no expiry
)

-- Media items
media (
  id              TEXT PRIMARY KEY,        -- 12-char generated ID
  album_id        TEXT NOT NULL REFERENCES albums ON DELETE CASCADE,
  user_id         UUID REFERENCES users ON DELETE SET NULL,
  filename_orig   TEXT,                    -- original upload filename
  media_type      TEXT NOT NULL,           -- 'image' | 'video'
  format          TEXT NOT NULL,           -- 'jpeg' | 'png' | 'gif' | 'mp4' | etc.
  mime_type       TEXT NOT NULL,
  storage_key     TEXT NOT NULL,           -- object store key for the original
  thumb_key       TEXT,                    -- object store key for thumbnail; null until generated
  thumb_is_orig   BOOLEAN NOT NULL DEFAULT false,  -- true = thumbnail endpoint serves storage_key
  thumb_status    TEXT NOT NULL DEFAULT 'pending', -- 'pending'|'processing'|'done'|'failed'
  file_size       BIGINT NOT NULL,
  thumb_size      BIGINT,
  width           INT,
  height          INT,
  duration_secs   FLOAT,                   -- video only; populated by ffprobe at upload
  is_animated     BOOLEAN NOT NULL DEFAULT false,
  codec_hint      TEXT,                    -- 'hevc' | 'vp9' | null; drives browser compat warnings
  position        BIGINT NOT NULL,         -- gap-based ordering: 1000, 2000, 3000, ...
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)

-- Runtime config
config (
  key             TEXT PRIMARY KEY,
  value           TEXT,
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_by      UUID REFERENCES users ON DELETE SET NULL
)

-- Audit log
audit_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type      TEXT NOT NULL,
  actor_id        UUID,                    -- null = anonymous
  actor_ip_hash   TEXT,                    -- SHA256 of IP; never raw IP
  target_type     TEXT,                    -- 'album' | 'media' | 'user' | 'config'
  target_id       TEXT,
  correlation_id  TEXT,                    -- request correlation ID for log tracing
  metadata        JSONB,                   -- event-specific context; see §12 for schema per event type
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
)

-- Per-user rate limit overrides
user_rate_limits (
  user_id         UUID PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  rpm             INT,
  bph             BIGINT
)
```

### Notes on `albums.cover_media_id`

This is a soft reference — not enforced as a DB foreign key, to avoid circular dependency issues at album/media creation time. Enforced at the application layer. The cover resolution function handles null (fall back to first by position) and stale references (media was deleted; fall back to first by position).

### Key Indexes

```sql
CREATE INDEX idx_albums_user_updated    ON albums (user_id, updated_at DESC);
CREATE INDEX idx_albums_expires         ON albums (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_media_album_position   ON media (album_id, position);
CREATE INDEX idx_media_user_id          ON media (user_id);
CREATE INDEX idx_media_thumb_pending    ON media (thumb_status) WHERE thumb_status IN ('pending', 'processing');
CREATE INDEX idx_audit_created          ON audit_log (created_at);
CREATE INDEX idx_audit_actor            ON audit_log (actor_id);
CREATE INDEX idx_audit_correlation      ON audit_log (correlation_id);
CREATE INDEX idx_audit_event_type       ON audit_log (event_type);
CREATE INDEX idx_api_keys_user_id       ON api_keys (user_id);
CREATE INDEX idx_sso_links_user_id      ON user_sso_links (user_id);
```

### `updated_at` Triggers

`albums.updated_at` and `config.updated_at` are maintained by Postgres triggers so that no application code can forget to update them. Without this, the user album list sort order (`updated_at DESC`) would silently go wrong any time a handler omits the field.

```sql
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER albums_set_updated_at
  BEFORE UPDATE ON albums
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TRIGGER config_set_updated_at
  BEFORE UPDATE ON config
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
```

`users.updated_at` follows the same pattern but is less load-bearing (not used for ordering in hot paths). Add the trigger for consistency.

### Task Queue Architecture

Background jobs are dispatched through the **TaskQueue abstraction** (see [§18](#18-abstraction-layers)). Application code never calls `enqueue` directly — instead, it emits domain events (see [§18 Event Bus](#event-bus)), and event listeners dispatch the appropriate tasks.

**Without Redis:** Tasks run synchronously in the upload request handler. No separate worker processes are needed. See [§23](#23-graceful-degradation).

#### Queue Topology

Jobs are split into separate named queues with different priority and concurrency characteristics:

| Queue | Purpose | Concurrency | Notes |
|---|---|---|---|
| `thumbnails` | Image/video thumbnail generation | CPU-bound; match to core count (e.g. `max_jobs=4`) | High priority — directly affects user-visible latency |
| `cleanup` | Deletion, pruning, audit log trimming | `max_jobs=1` | Low priority, I/O-bound, order doesn't matter |
| `default` | Future lightweight tasks | `max_jobs=4` | Catch-all for anything that doesn't need its own queue |

Each queue runs as a **separate Docker service** (or separate process) using the same application image with a different command. The `thumbnails` worker can be scaled independently — if thumbnail latency is creeping up, add more thumbnail workers without touching cleanup.

#### Current Implementation: arq

arq supports queue separation natively via the `queue_name` parameter. Each worker service subscribes to one queue name. The arq-specific wiring (worker class, task registration, retry config) lives in a single file behind the TaskQueue abstraction.

#### Failure Handling

**Retry:** arq retries failed jobs up to 3 times (configurable per queue). Retry behavior is implementation-specific and expected to change during migration — this is acceptable.

**Dead letters:** No custom dead letter mechanism. When a thumbnail job exhausts all retries, the worker sets `thumb_status = 'failed'` in the database. The `failed` status in the database *is* the dead letter record — queryable from admin, displayable in the UI as a broken-image placeholder. An admin CLI command can re-enqueue all failed thumbnails.

When migrating to SQS, SQS provides native dead letter queues automatically — failed messages are moved to a DLQ after exceeding the retry count, with built-in redrive support. No need to build DLQ behavior in the arq era.

#### Correlation ID Propagation

Every task message includes the `correlation_id` from the originating HTTP request. When the worker picks up a job, it binds the correlation ID to structlog's context so all log lines from the worker carry the same ID as the original upload request. This enables tracing a single upload from HTTP request through thumbnail completion in logs. See [§19 Correlation IDs](#correlation-ids).

### Docker Compose Worker Services

```yaml
worker-thumbnails:
  build: .
  command: python -m imghost worker --queue thumbnails
  environment: *app-env
  depends_on:
    pgbouncer:
      condition: service_healthy
    redis:
      condition: service_healthy
    garage:
      condition: service_healthy
  restart: unless-stopped

worker-cleanup:
  build: .
  command: python -m imghost worker --queue cleanup
  environment: *app-env
  depends_on:
    pgbouncer:
      condition: service_healthy
    redis:
      condition: service_healthy
    garage:
      condition: service_healthy
  restart: unless-stopped
```

### Upload Flow (Event-Driven)

The upload handler's core responsibility is narrow: validate, store, insert. Side effects are handled by event listeners.

1. **Upload handler:**
   - Identifies format by magic bytes → looks up MediaProcessor
   - Calls `processor.validate(file)` — reject if invalid
   - Calls `processor.extract_metadata(file)` — dimensions, duration, codec
   - Calls `processor.sanitize(file)` — strip EXIF, sanitize SVG
   - Stores processed file to storage backend via `StorageBackend.put()`
   - Creates album (if new) and media row via `AlbumRepository` / `MediaRepository`
   - **Emits `MediaUploaded` event** — includes media_id, album_id, user_id, file_size, format, correlation_id
   - Returns response immediately

2. **Event listeners** (registered separately, not in the handler):
   - `on_media_uploaded → enqueue("generate_thumbnail", queue="thumbnails", media_id=..., correlation_id=...)`
   - `on_media_uploaded → metrics.increment("uploads_total", tags={media_type, source})`
   - `on_media_uploaded → audit.write("media_uploaded", ...)`
   - `on_media_uploaded → update_quota_cache(user_id, file_size)`

This means adding a webhook system, a notification, or any other side effect requires registering one more listener — the upload handler is never touched.

### Thumbnail Worker Flow

1. Worker picks up `generate_thumbnail` job with `media_id` and `correlation_id`
1. Binds `correlation_id` to structlog context
1. Loads media row via `MediaRepository`
1. Checks `thumb_status` — if already `'done'`, skip (idempotent). See [§22](#22-idempotency--reliability).
1. Sets `thumb_status = 'processing'`
1. Streams original from storage backend
1. Looks up MediaProcessor for the format, calls `processor.generate_thumbnail()`
1. Uploads thumbnail to storage backend
1. Updates `thumb_key`, `thumb_size`, `thumb_status = 'done'`

On permanent failure (retries exhausted): sets `thumb_status = 'failed'`. Album page shows a static broken-image placeholder.

### Pruning Job

**Schedule:** `0 12 * * *` (noon UTC / ~4am Pacific). Configurable via `PRUNE_CRON` env.
**Runs in:** the `worker-cleanup` service.
**Enqueued to:** the `cleanup` queue.

**What it deletes, in order:**

1. Find all albums where `expires_at < NOW()`
1. For each album, collect all `storage_key` and `thumb_key` values from child media rows
1. Delete storage objects for each key via `StorageBackend.delete()`
1. Delete media rows (FK cascade handles this when album is deleted)
1. Delete album rows
1. Delete `audit_log` rows where `created_at < NOW() - INTERVAL '90 days'`

**Order of operations:** Storage objects are deleted **before** DB rows. If a storage delete fails, log the error and skip the DB delete for that item — it will be retried on the next prune run. This prevents orphaned DB rows pointing to missing objects. The prune job is inherently idempotent — running it twice produces the same result. See [§22](#22-idempotency--reliability).

**CLI:**

```bash
docker compose exec app python -m imghost prune
docker compose exec app python -m imghost prune --dry-run   # shows what would be deleted
```

### ZIP Streaming

Album ZIP downloads are streamed on the fly. A generator pulls each file from storage via `StorageBackend.get_stream()` and feeds it into Python's `zipfile.ZipFile` in streaming mode, yielded through FastAPI's `StreamingResponse`. No temp files are written; memory usage is bounded to the streaming buffer.

-----

## 18. Abstraction Layers

This section defines the interfaces that isolate business logic from infrastructure. The discipline is: **if you grep the codebase for `arq`, `prometheus`, `garage`, `sqlalchemy`, or `audit_log INSERT`, each should appear in exactly one file** — the adapter implementation. Everything else calls the interface.

### Event Bus

The event bus decouples "what happened" from "what should happen because of it." Handlers emit domain events; listeners handle side effects independently. This is an in-process dispatch mechanism — not an external message broker.

```python
# Event definitions — simple dataclasses
@dataclass
class MediaUploaded:
    media_id: str
    album_id: str
    user_id: str | None
    file_size: int
    media_type: str      # 'image' | 'video'
    format: str          # 'jpeg' | 'mp4' | etc.
    source: str          # 'web' | 'api'
    correlation_id: str

@dataclass
class MediaDeleted:
    media_id: str
    album_id: str
    file_size: int
    actor_id: str | None
    correlation_id: str

@dataclass
class AlbumCreated:
    album_id: str
    user_id: str | None
    item_count: int
    source: str          # 'web' | 'api'. API key uploads (ShareX) are treated as 'api'.
    correlation_id: str

@dataclass
class AlbumDeleted:
    album_id: str
    item_count: int
    total_size: int
    actor_id: str | None
    correlation_id: str

@dataclass
class AlbumTitleChanged:
    album_id: str
    old_title: str | None
    new_title: str | None
    actor_id: str
    correlation_id: str

@dataclass
class AlbumCoverSet:
    album_id: str
    media_id: str
    actor_id: str
    correlation_id: str

@dataclass
class AlbumReordered:
    album_id: str
    actor_id: str
    correlation_id: str

@dataclass
class AlbumExpiryChanged:
    album_id: str
    old_expiry: str | None   # ISO 8601 or null
    new_expiry: str | None
    actor_id: str
    correlation_id: str

@dataclass
class UserPasswordReset:
    target_user_id: str
    actor_id: str            # admin who performed the reset
    correlation_id: str

@dataclass
class AdminLoggedIn:
    actor_id: str
    correlation_id: str

@dataclass
class UserRegistered:
    user_id: str
    method: str          # 'registration' | 'oauth' | 'admin'
    correlation_id: str

@dataclass
class UserDeleted:
    user_id: str
    deleted_by: str      # 'self' | 'admin'
    correlation_id: str

@dataclass
class UserSuspended:
    user_id: str
    suspended: bool
    actor_id: str
    correlation_id: str

@dataclass
class ConfigChanged:
    key: str
    old_value: str | None
    new_value: str | None
    actor_id: str
    correlation_id: str
```

```python
# The bus itself — a list of callables keyed by event type
class EventBus:
    def subscribe(self, event_type: type, listener: Callable) -> None: ...
    async def emit(self, event: Any) -> None: ...
```

`emit()` calls all registered listeners for that event type. Listeners run in-process. If a listener fails, the failure is logged but does not propagate to the handler that emitted the event — side effects are best-effort, not transactional with the core operation.

#### Standard Listeners

These listeners are registered at application startup:

| Event | Listener | Side Effect |
|---|---|---|
| `MediaUploaded` | `enqueue_thumbnail` | Enqueue thumbnail generation task (or run sync in Pi mode) |
| `MediaUploaded` | `write_upload_audit` | Write audit log entry |
| `MediaUploaded` | `record_upload_metrics` | Increment upload counters and histograms |
| `MediaUploaded` | `update_quota` | Update user's cached quota usage |
| `MediaDeleted` | `write_delete_audit` | Write audit log entry |
| `MediaDeleted` | `record_delete_metrics` | Decrement storage gauge |
| `AlbumCreated` | `write_album_audit` | Write audit log entry |
| `AlbumDeleted` | `write_album_delete_audit` | Write audit log entry |
| `AlbumDeleted` | `record_album_delete_metrics` | Update storage metrics |
| `AlbumTitleChanged` | `write_album_title_audit` | Write audit log entry |
| `AlbumCoverSet` | `write_album_cover_audit` | Write audit log entry |
| `AlbumReordered` | `write_album_reorder_audit` | Write audit log entry |
| `AlbumExpiryChanged` | `write_album_expiry_audit` | Write audit log entry |
| `UserPasswordReset` | `write_password_reset_audit` | Write audit log entry |
| `AdminLoggedIn` | `write_admin_login_audit` | Write audit log entry |
| `UserRegistered` | `write_user_audit` | Write audit log entry |
| `UserDeleted` | `cleanup_user_sessions` | Invalidate all sessions for the user |
| `UserDeleted` | `write_user_delete_audit` | Write audit log entry |
| `UserSuspended` | `write_suspend_audit` | Write audit log entry |
| `UserSuspended` | `invalidate_sessions_if_suspended` | Kill active sessions if suspended=true |
| `ConfigChanged` | `write_config_audit` | Write audit log entry |

Adding a webhook system, a notification service, or any other side effect means registering one more listener. No existing code changes.

### StorageBackend

All object store interactions go through this interface. Garage, AWS S3, R2, GCS, and local filesystem are all valid implementations behind it.

```python
class StorageBackend(ABC):
    async def put(self, key: str, data: AsyncIterator[bytes],
                  content_type: str, size: int) -> None: ...
    async def get_stream(self, key: str,
                         range_header: str | None = None) -> StorageStream: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def get_size(self, key: str) -> int: ...
    async def health_check(self) -> bool: ...    # used by readiness probe

class StorageStream:
    status_code: int           # 200 or 206
    content_type: str
    content_length: int | None
    content_range: str | None  # populated on 206 responses
    body: AsyncIterator[bytes]
```

**Implementations:** `GarageS3Backend` (default). Swapping to AWS S3 or R2 = change `S3_ENDPOINT_URL` and credentials. `LocalFilesystemBackend` for dev-without-Docker (~50 lines).

### TaskQueue

All job dispatch goes through a single `enqueue` function. Task functions are plain functions with simple arguments (strings, ints) — no framework decorators, no framework context objects. Every enqueued job includes a `correlation_id` for log tracing.

```python
# The entire interface
async def enqueue(task_name: str, queue: str = "default", **kwargs) -> None: ...
```

**Implementations:**
- **arq (default):** `enqueue` serializes kwargs and pushes to the arq Redis queue with `_queue_name=queue`. One file contains the arq worker config and task registry.
- **Sync in-process (Redis-free mode):** `enqueue` calls the task function directly in the current process. No background workers needed. Thumbnails are generated during the upload request. See [§23](#23-graceful-degradation).
- **SQS (future):** `enqueue` serializes kwargs to JSON and sends an SQS message to the queue URL mapped from the queue name. Consumer service polls SQS and dispatches to the same task functions. SQS provides native dead letter queues and redrive.
- **Celery (future):** `enqueue` dispatches to the named Celery task with the given queue routing.

Task functions (e.g. `generate_thumbnail(media_id: str, correlation_id: str)`) never import or reference the queue framework. They are called by the framework-specific worker, but they don't know that.

### Repositories

The repository layer puts a thin interface over all database access. **SQLAlchemy models, sessions, and query construction are confined to repository files.** Handlers and task functions call repository methods; they never import SQLAlchemy.

```python
class AlbumRepository(ABC):
    async def get_by_id(self, album_id: str) -> Album | None: ...
    async def create(self, album_id: str, user_id: str | None,
                     title: str | None, expires_at: datetime | None) -> Album: ...
    async def update(self, album_id: str, **fields) -> Album: ...
    async def delete(self, album_id: str) -> None: ...
    async def list_by_user(self, user_id: str, limit: int = 50,
                           offset: int = 0) -> list[Album]: ...
    async def find_expired(self) -> list[Album]: ...

class MediaRepository(ABC):
    async def get_by_id(self, media_id: str) -> Media | None: ...
    async def create(self, **fields) -> Media: ...
    async def update(self, media_id: str, **fields) -> Media: ...
    async def delete(self, media_id: str) -> None: ...
    async def list_by_album(self, album_id: str) -> list[Media]: ...
    async def find_pending_thumbnails(self) -> list[Media]: ...
    async def find_failed_thumbnails(self) -> list[Media]: ...
    async def sum_storage_by_user(self, user_id: str) -> int: ...

class UserRepository(ABC):
    async def get_by_id(self, user_id: str) -> User | None: ...
    async def get_by_username(self, username: str) -> User | None: ...
    async def create(self, **fields) -> User: ...
    async def update(self, user_id: str, **fields) -> User: ...
    async def delete(self, user_id: str) -> None: ...
    async def list_all(self, limit: int = 50, offset: int = 0) -> list[User]: ...
    async def authenticate(self, username: str, password: str) -> User | None: ...

class ConfigRepository(ABC):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, updated_by: str) -> None: ...
    async def get_all(self) -> dict[str, str]: ...
```

**Why this matters:**
- **Testability:** Handlers can be tested with a mock repository — no database needed. Unit tests are fast and deterministic.
- **Readability:** Handlers become short and declarative: "get album, check ownership, delete media, emit event." The SQL complexity lives in one place.
- **Query optimization:** When a query needs tuning, you know exactly where to look — the repository method, not scattered across dozens of handlers.
- **SQLAlchemy isolation:** A grep for `sqlalchemy` should only hit repository implementation files and the model definitions. If it appears in a handler, that's a code review flag.

**Implementation:** `SqlAlchemyAlbumRepository`, `SqlAlchemyMediaRepository`, etc. These are the only files that import SQLAlchemy session and model objects.

### AuditWriter / AuditReader

All audit log writes and reads go through these interfaces. Audit writes are **triggered by event bus listeners**, never called directly from handlers. The first implementation writes to/reads from the PostgreSQL `audit_log` table. Future implementations can write to Elasticsearch, Loki, CloudWatch, etc. with zero changes to calling code.

```python
# Write interface
async def write_audit_event(
    event_type: str,
    actor_id: str | None,
    actor_ip_hash: str | None,
    target_type: str,
    target_id: str,
    correlation_id: str,
    metadata: dict
) -> None: ...

# Read interface
async def query_audit_log(
    event_type: str | None = None,
    actor_id: str | None = None,
    correlation_id: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 100,
    offset: int = 0
) -> list[AuditEvent]: ...
```

The `metadata` dict follows a **documented schema per event type** (see [§12 Audit Log](#audit-log)). Consistent metadata structure makes migration to structured log stores mechanical.

### Metrics

Three operations. That's the entire interface.

```python
def increment(name: str, value: int = 1, tags: dict | None = None) -> None: ...
def histogram(name: str, value: float, tags: dict | None = None) -> None: ...
def gauge(name: str, value: float, tags: dict | None = None) -> None: ...
```

**Implementations:**
- **Log (default):** Each call emits a structured JSON log line. Crude but queryable from log aggregation. Zero dependencies.
- **Prometheus (future):** Counters, histograms, and gauges map directly to Prometheus client types. Add a `/metrics` endpoint to FastAPI. Point Prometheus at it. Everything the app was already tracking appears in Grafana.
- **Datadog / CloudWatch (future):** Same interface, different wire protocol.

Configured via `METRICS_BACKEND` env var. See [§19](#19-observability--metrics) for which metrics to instrument.

### MediaProcessor Pipeline

Format-specific processing logic is handled through a **processor registry**. Each supported format registers a processor that implements a standard interface. The upload handler identifies the format by magic bytes and dispatches to the correct processor — no if/else chains.

```python
class MediaProcessor(ABC):
    """Implement one per supported format (or format family)."""

    @staticmethod
    def supported_formats() -> list[str]: ...
    # Returns format strings this processor handles, e.g. ['jpeg', 'jpg']

    async def validate(self, file: UploadFile) -> ValidationResult: ...
    # Check magic bytes, dimensions, codec. Return rejection reason or OK.

    async def extract_metadata(self, file: UploadFile) -> MediaMetadata: ...
    # Return width, height, duration, codec_hint, is_animated, mime_type.

    async def sanitize(self, file: UploadFile) -> SanitizedFile: ...
    # Strip EXIF, sanitize SVG, remux video. Return processed bytes.

    async def generate_thumbnail(self, file: UploadFile,
                                  metadata: MediaMetadata) -> ThumbnailResult: ...
    # Generate format-appropriate thumbnail per §5 rules.
    # Return thumbnail bytes + format, or ThumbIsOriginal sentinel.
```

```python
@dataclass
class ValidationResult:
    ok: bool
    rejection_reason: str | None = None

@dataclass
class MediaMetadata:
    width: int | None
    height: int | None
    duration_secs: float | None
    codec_hint: str | None       # 'hevc' | 'vp9' | None
    is_animated: bool
    mime_type: str
    format: str                  # 'jpeg' | 'png' | 'gif' | 'mp4' | etc.

@dataclass
class ThumbnailResult:
    data: bytes | None           # None if thumb_is_orig
    thumb_is_orig: bool
    format: str                  # 'jpeg' | 'webp'
    size: int
```

#### Processor Registry

Processors register themselves by declaring which formats they handle. At startup, the registry builds a format-to-processor lookup table. The upload handler calls `registry.get_processor(format)` and gets the right one.

```python
class ProcessorRegistry:
    def register(self, processor: MediaProcessor) -> None: ...
    def get_processor(self, format: str) -> MediaProcessor | None: ...
```

#### Standard Processors

| Processor | Formats | Key Behavior |
|---|---|---|
| `JpegProcessor` | jpeg, jpg | Pillow EXIF strip, JPEG thumbnail |
| `PngProcessor` | png | Pillow EXIF strip, JPEG thumbnail |
| `GifProcessor` | gif | Animated detection, size-based thumb strategy |
| `WebpProcessor` | webp | Animated detection, Pillow processing |
| `HeicProcessor` | heic, heif | pyvips conversion to JPEG, then standard thumbnail |
| `AvifProcessor` | avif | pyvips processing |
| `BmpProcessor` | bmp | Pillow processing, JPEG thumbnail |
| `SvgProcessor` | svg | Script/event handler sanitization, rasterized JPEG thumbnail (375px wide) |
| `Mp4Processor` | mp4 | ffprobe metadata, ffmpeg remux, video thumbnail |
| `MovProcessor` | mov | Same as Mp4 with HEVC codec detection |
| `WebmProcessor` | webm | ffprobe metadata, VP8/VP9 detection, video thumbnail |

**Adding a new format** means writing one processor class, implementing the four methods, and calling `registry.register()`. No changes to the upload handler, no changes to existing processors.

**Testing:** Each processor has its own test suite. The SVG sanitizer can be tested in isolation with crafted malicious inputs. The HEIC-to-JPEG converter can be tested with sample files. Video metadata extraction can be tested with short clips. No integration test needs to exercise the full upload pipeline to verify format-specific behavior.

-----

## 19. Observability & Metrics

### Correlation IDs

Every request that enters the system is assigned a **correlation ID** — a unique string that flows through every log line, every event bus emission, every task queue message, every audit log entry, and every metrics tag for that request. This enables tracing a single user action (e.g. "upload a file") from HTTP request through background worker completion in a single log search.

#### How It Works

1. **FastAPI middleware** checks for an incoming `X-Correlation-ID` header. If present, it uses that value (enables client-side correlation). If absent, it generates a new UUID.
2. The correlation ID is bound to **structlog's context variables** for the duration of the request. Every log line emitted during the request automatically includes `"correlation_id": "abc-123"` with no explicit passing required.
3. The correlation ID is included in the **`X-Correlation-ID` response header** so clients can correlate their logs with server logs.
4. When the handler emits a **domain event**, the event dataclass includes the `correlation_id` field (populated from the request context).
5. When an event listener enqueues a **background task**, the `correlation_id` is passed as a task argument.
6. When a **worker picks up a task**, it binds the `correlation_id` to structlog's context before executing. All worker log lines carry the same correlation ID as the original request.
7. When an event listener writes an **audit log entry**, the `correlation_id` is stored in the `audit_log.correlation_id` column.

#### Example: Tracing an Upload

A single search for `correlation_id = "abc-123"` returns:

```
[app]    INFO  upload_started       media_type=image format=jpeg size=4200000  correlation_id=abc-123
[app]    INFO  storage_put          key=originals/user1/xk7m.jpg              correlation_id=abc-123
[app]    INFO  media_created        media_id=xk7m album_id=abc9              correlation_id=abc-123
[app]    INFO  event_emitted        event=MediaUploaded                       correlation_id=abc-123
[app]    INFO  task_enqueued        task=generate_thumbnail queue=thumbnails  correlation_id=abc-123
[worker] INFO  task_started         task=generate_thumbnail media_id=xk7m    correlation_id=abc-123
[worker] INFO  thumbnail_generated  media_id=xk7m format=jpeg size=12000     correlation_id=abc-123
[worker] INFO  task_completed       task=generate_thumbnail duration_ms=850  correlation_id=abc-123
```

Without correlation IDs, these 8 log lines from 2 different processes would be unrelated noise.

### Logging

#### Structured JSON Logging

All logs are structured JSON via `structlog`. This is non-negotiable — structured logs are searchable and parseable by log aggregation systems; human-readable strings are not.

Every log line automatically includes: `timestamp`, `level`, `event`, `correlation_id` (from context), `service` ("app" or "worker-thumbnails" or "worker-cleanup").

Example log line:

```json
{"event": "upload_complete", "media_id": "xk7m2np4q8wr", "album_id": "xk7m2np4q", "size_bytes": 4200000, "duration_ms": 340, "user_id": "anon", "correlation_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "service": "app", "level": "info", "timestamp": "2025-01-15T12:34:56Z"}
```

#### Log Targets

- **Always:** stdout/stderr (captured by `docker compose logs`; shipped to Loki / CloudWatch / Elasticsearch in production)
- **Optional:** File via `LOG_FILE=/var/log/imghost/app.log`; daily rotation, 14-day retention

#### Log Level

`LOG_LEVEL` env var, default `INFO`. Values: `DEBUG`, `INFO`, `WARNING`, `ERROR`.

#### What Gets Logged

All log lines include `correlation_id` automatically via structlog context.

- Every upload: user/anon, file size, media ID, album ID, thumb task enqueued
- Thumbnail job: start, success (with duration), failure with error detail
- Every deletion: media ID, actor
- Auth events: login, logout, failed login, OAuth flow steps
- Rate limit hits: scope, limit type (IP hash only — never raw IP)
- Redis unavailable: WARNING with timestamp
- Server quota reached: WARNING with current usage figures
- Prune job: start, count of items deleted, bytes freed, wall-clock duration
- Event bus listener failures: WARNING with event type and listener name
- All metrics emissions (when `METRICS_BACKEND=log`)

### Metrics

All metrics are emitted through the Metrics abstraction (see [§18](#18-abstraction-layers)). The default backend logs them as structured JSON. When upgraded to Prometheus or Datadog, the same metrics appear in dashboards with zero calling-code changes.

#### Instrumented Metrics

| Metric Name | Type | Tags | Description |
|---|---|---|---|
| `http_requests_total` | counter | `method`, `endpoint`, `status` | Request count by endpoint and status code (FastAPI middleware) |
| `http_request_duration_seconds` | histogram | `method`, `endpoint` | Request latency (FastAPI middleware) |
| `uploads_total` | counter | `media_type`, `source` | Upload count (web / api) |
| `upload_duration_seconds` | histogram | `media_type` | End-to-end upload handler time |
| `upload_bytes_total` | counter | `media_type` | Total bytes uploaded |
| `thumbnail_jobs_total` | counter | `status` | Thumbnail jobs completed (done / failed) |
| `thumbnail_duration_seconds` | histogram | `media_type` | Time to generate one thumbnail |
| `thumbnail_queue_depth` | gauge | `queue` | Number of pending jobs per queue |
| `storage_bytes_used` | gauge | `scope` | Total storage (all / per-user breakdown sampled periodically) |
| `active_sessions` | gauge | | Current session count in Redis (0 in Redis-free mode) |
| `prune_items_deleted` | counter | | Items removed per prune run |
| `prune_bytes_freed` | counter | | Bytes freed per prune run |
| `prune_duration_seconds` | histogram | | Wall-clock time of prune job |
| `event_bus_listeners_failed` | counter | `event_type`, `listener` | Listener failures (should be ~0) |

#### Prometheus Upgrade Path

When `METRICS_BACKEND=prometheus`:

1. The Prometheus client library replaces the log-based implementation behind the same interface
2. A `/metrics` endpoint is added to FastAPI (one line of middleware)
3. Prometheus scrapes the endpoint; Grafana connects to Prometheus for dashboards

#### Recommended Dashboards (Post-Prometheus)

- **API health:** Request rate, latency percentiles (p50/p95/p99), error rate by endpoint
- **Thumbnail pipeline:** Queue depth over time, processing time distribution, failure rate
- **Storage:** Total bytes used, per-user breakdown, quota headroom

#### Recommended Alerts (Post-Prometheus)

- Error rate > 5% of requests returning 5xx over 5 minutes
- Thumbnail queue depth growing monotonically for > 10 minutes (workers dead or stuck)
- Storage usage > 90% of server quota
- Certificate expiry < 14 days (if managing own certs)

-----

## 20. Deployment

### Docker Compose (Default — No Nginx in Container)

```yaml
services:
  app:
    build: .
    ports:
      - "${PORT:-8000}:8000"
    environment: &app-env
      DATABASE_URL: postgresql+asyncpg://imghost:${POSTGRES_PASSWORD:-imghost}@pgbouncer:5432/imghost?prepared_statement_cache_size=0
      REDIS_URL: redis://redis:6379/0
      S3_ENDPOINT_URL: http://garage:3900
      S3_ACCESS_KEY_ID: ${S3_ACCESS_KEY_ID}
      S3_SECRET_ACCESS_KEY: ${S3_SECRET_ACCESS_KEY}
      S3_BUCKET: imghost
      S3_REGION: garage
      BASE_URL: ${BASE_URL}
      SECRET_KEY: ${SECRET_KEY}
      PORT: "8000"
      METRICS_BACKEND: ${METRICS_BACKEND:-log}
    depends_on:
      db:
        condition: service_healthy
      pgbouncer:
        condition: service_healthy
      redis:
        condition: service_healthy
      garage:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health/live"]
      interval: 15s
      timeout: 5s
      retries: 3
      start_period: 10s
    restart: unless-stopped

  worker-thumbnails:
    build: .
    command: python -m imghost worker --queue thumbnails
    environment: *app-env
    depends_on:
      pgbouncer:
        condition: service_healthy
      redis:
        condition: service_healthy
      garage:
        condition: service_healthy
    restart: unless-stopped

  worker-cleanup:
    build: .
    command: python -m imghost worker --queue cleanup
    environment: *app-env
    depends_on:
      pgbouncer:
        condition: service_healthy
      redis:
        condition: service_healthy
      garage:
        condition: service_healthy
    restart: unless-stopped

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: imghost
      POSTGRES_USER: imghost
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-imghost}
    volumes:
      - imghost_db:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U imghost"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  pgbouncer:
    image: bitnami/pgbouncer:latest
    environment:
      POSTGRESQL_HOST: db
      POSTGRESQL_USERNAME: imghost
      POSTGRESQL_PASSWORD: ${POSTGRES_PASSWORD:-imghost}
      POSTGRESQL_DATABASE: imghost
      PGBOUNCER_POOL_MODE: transaction
      PGBOUNCER_DEFAULT_POOL_SIZE: 10
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -p 5432 -U imghost"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  garage:
    image: dxflrs/garage:v1.0.0
    volumes:
      - garage_meta:/var/lib/garage/meta
      - garage_data:/var/lib/garage/data
      - ./garage.toml:/etc/garage.toml:ro
    # Port 3900 intentionally NOT exposed to host — internal Docker network only
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:3900/health || exit 1"]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 10s
    restart: unless-stopped

volumes:
  imghost_db:
  garage_meta:
  garage_data:
```

### Lightweight Compose (Raspberry Pi / No Redis)

```yaml
# docker-compose.pi.yml — override for resource-constrained deployments
services:
  app:
    environment:
      REDIS_URL: ""    # empty = Redis-free mode
    depends_on:
      db:
        condition: service_healthy
      garage:
        condition: service_healthy
    # No worker services needed — tasks run synchronously in-process

  # Remove worker and redis services
  worker-thumbnails:
    profiles: ["disabled"]
  worker-cleanup:
    profiles: ["disabled"]
  redis:
    profiles: ["disabled"]
```

Usage: `docker compose -f docker-compose.yml -f docker-compose.pi.yml up`

### Health Check Endpoints

Two endpoints, distinct purposes. Never conflate them — they serve different operational needs.

#### `GET /health/live` — Liveness

**Question it answers:** "Is this process alive and not deadlocked?"

- Returns `200 OK` with `{"status": "alive"}` if the Python process is running
- **Never checks dependencies.** If Postgres is down, killing and restarting the app doesn't fix Postgres. Liveness failures should only trigger a container restart.
- Used by: Docker `HEALTHCHECK`, Kubernetes `livenessProbe`, ECS health check

#### `GET /health/ready` — Readiness

**Question it answers:** "Can this instance serve traffic right now?"

- Checks each dependency and reports individual status:
  - **PostgreSQL:** Execute `SELECT 1` via the connection pool
  - **Redis (if configured):** Execute `PING`
  - **Storage backend:** Call `StorageBackend.health_check()` (e.g. `HEAD` on the bucket)
- Returns `200 OK` with component statuses if all pass
- Returns `503 Service Unavailable` with component statuses if any required check fails
- Redis failure does **not** fail readiness if `REDIS_URL` is unset (Redis-free mode)
- Used by: Load balancer health checks, Kubernetes `readinessProbe`. When readiness fails, the load balancer stops routing traffic to this instance but doesn't kill it. When the dependency recovers, readiness passes again and traffic resumes.

```json
// 200 OK — all healthy
{
  "status": "ready",
  "checks": {
    "database": {"status": "ok", "latency_ms": 2},
    "redis": {"status": "ok", "latency_ms": 1},
    "storage": {"status": "ok", "latency_ms": 15}
  }
}

// 503 — storage backend unreachable
{
  "status": "not_ready",
  "checks": {
    "database": {"status": "ok", "latency_ms": 2},
    "redis": {"status": "ok", "latency_ms": 1},
    "storage": {"status": "error", "error": "connection refused"}
  }
}

// 200 OK — Redis-free mode, Redis not checked
{
  "status": "ready",
  "checks": {
    "database": {"status": "ok", "latency_ms": 2},
    "redis": {"status": "skipped", "reason": "not configured"},
    "storage": {"status": "ok", "latency_ms": 15}
  }
}
```

### Garage Initial Setup

Garage requires a one-time bucket and key creation after first start. Documented in `docs/garage-setup.md` and scripted as:

```bash
python -m imghost init-storage
# Internally runs: garage layout assign, garage key create, garage bucket create/allow
```

### Host Nginx Config

Shipped as `docs/nginx-site.conf`. Drop into `/etc/nginx/sites-enabled/`:

```nginx
server {
    listen 80;
    server_name img.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name img.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/img.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/img.yourdomain.com/privkey.pem;

    client_max_body_size 512M;   # must be >= MAX_VIDEO_SIZE_MB

    # When behind Cloudflare, use CF-Connecting-IP for real client IP
    # set_real_ip_from  173.245.48.0/20;   # uncomment + add all CF ranges
    # real_ip_header    CF-Connecting-IP;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # Required for video range requests / seeking
        proxy_set_header   Range    $http_range;
        proxy_set_header   If-Range $http_if_range;
        proxy_no_cache     $http_range $http_if_range;
    }
}
```

Garage is never referenced in nginx config. It is Docker-internal only.

### Optional: `docker-compose.with-nginx.yml`

An override compose file adding a bundled nginx + Certbot container for users who want a fully self-contained stack. Documented and shipped separately; not the default.

### Dockerfile Notes

- Base image: `python:3.12-slim`
- System packages: `ffmpeg`, `libvips42`, `libavif-dev`, `curl` (for healthcheck)
- Runs as non-root user (`imghost:imghost`)
- Entrypoint: `alembic upgrade head && uvicorn ...` (migrations run at startup automatically)
- Same image used for `app`, `worker-thumbnails`, and `worker-cleanup` services; service role set by `command`

### CLI Commands

```bash
python -m imghost create-admin     # Interactive first admin setup
python -m imghost init-storage     # Create Garage bucket and access key (run once)
python -m imghost migrate          # Run DB migrations (also runs automatically at startup)
python -m imghost prune            # Manual prune run
python -m imghost prune --dry-run  # Preview what would be pruned without deleting
python -m imghost worker --queue thumbnails   # Start thumbnail worker
python -m imghost worker --queue cleanup      # Start cleanup worker
python -m imghost retry-thumbnails            # Re-enqueue all failed thumbnails
```

-----

## 21. Security Considerations

### File Handling

- File type validated by **magic bytes** only (not extension or `Content-Type` header) — this is the first step in the MediaProcessor pipeline's `validate` method
- SVG sanitized before storage: strip `<script>` tags, event handler attributes, and external `href`/`src` references
- Decompression bomb check via header-only parse before any pixel decoding (50MP limit)
- Files served with correct `Content-Type`; `Content-Disposition: inline` for media, `attachment` for ZIP downloads
- ZIP filenames sanitized to prevent path traversal

### Auth

- Passwords: bcrypt, cost factor 12
- API keys: raw value shown once only; stored as SHA-256 hash
- Session tokens: 32-byte random, stored hashed in Redis (or signed cookies in Redis-free mode)
- Cookie flags: `httponly; secure; samesite=lax`
- CSRF protection on all state-mutating form endpoints

### Rate Limit Key

`SHA256(IP + "|" + User-Agent)` — avoids storing raw IPs in Redis; provides basic bot resistance without perfect uniqueness; more GDPR-friendly. When behind Cloudflare, IP is sourced from `CF-Connecting-IP`.

### Admin Routes

All `/admin/*` and `/api/v1/admin/*` routes protected by a shared FastAPI dependency that checks `current_user.is_admin`. Protection is not opt-in per-route.

### Storage Isolation

The object store (Garage or S3) is never directly accessible from the internet. In the default Docker deployment, Garage is on the internal Docker network only — port 3900 is not mapped to the host. No storage credentials or internal URLs ever reach the browser. The only way to access stored media is through FastAPI's streaming proxy (or the CDN, which caches the proxy's responses).

-----

## 22. Idempotency & Reliability

Every write operation is designed so that executing it twice produces the same result as executing it once. This matters because retries are inevitable — network timeouts cause clients to retry uploads, crashed workers cause the task queue to redeliver jobs, and the prune job runs daily against a set of items that may have been partially cleaned on the previous run.

### Upload Idempotency

**Risk:** Client times out during upload, retries. Could create duplicate albums.

**Mitigation:** Each upload request generates its media ID and album ID before any writes. The media ID is derived from a cryptographic random value, not from file content (no dedup by design). If the client retries with a new request, it gets a new album — this is acceptable behavior. The original upload either completed (and both albums exist, which is harmless) or failed partway through.

**Partial failure cleanup:** If storage `put` succeeds but the DB insert fails, the upload handler catches the exception and attempts `StorageBackend.delete()` for the orphaned object. If the delete also fails, the orphaned object will be caught by a periodic orphan scan (storage keys with no matching DB row). This is a background maintenance task, not a user-facing concern.

**Test:** Upload the same file twice in rapid succession. Verify two separate albums are created, each with correct metadata and storage objects. Verify no duplicate media IDs.

### Thumbnail Idempotency

**Risk:** Worker crashes mid-thumbnail, arq redelivers the job. Could corrupt the thumbnail or double-process.

**Mitigation:** The thumbnail worker checks `thumb_status` at the start of every job:
- If `done` → skip entirely, return success. The job was already completed on a previous attempt.
- If `processing` → this means a previous worker died mid-flight. Proceed with regeneration (overwrite is safe — storage `put` is an unconditional overwrite, not create-if-not-exists).
- If `pending` → normal case, proceed.

Storage writes are unconditional overwrites. Writing the same thumbnail twice produces the same object. The final DB update (`thumb_status = 'done'`, `thumb_key`, `thumb_size`) is a single atomic UPDATE.

**Test:** Enqueue the same `generate_thumbnail` job twice. Verify the thumbnail is generated once and the second invocation is a no-op. Force-kill a worker mid-processing, let the retry run, verify the thumbnail is correct.

### Deletion Idempotency

**Risk:** Delete request times out, client retries. Could error on "not found."

**Mitigation:** Deleting an already-deleted resource returns 404, which is the correct and expected response. The client treats both "deleted successfully" and "already gone" as success. Storage `delete` is also idempotent — deleting a non-existent key is a no-op in S3-compatible stores.

**Test:** Delete a media item, then delete it again. Verify 200 on first call, 404 on second. Verify no errors in logs.

### Album Deletion Idempotency

**Risk:** Album deletion involves multiple storage deletes (one per media item) and cascade DB deletes. Could fail partway through.

**Mitigation:** Album deletion iterates through media items and deletes storage objects one by one. If a storage delete fails, it logs the error and continues — the next item still gets deleted. The DB cascade delete (`ON DELETE CASCADE`) removes all media rows atomically when the album row is deleted. If storage deletes partially failed, the orphaned objects are cleaned by the orphan scan.

The DB delete is the last step. If it fails, all storage objects were already deleted (or attempted), and the next deletion attempt will re-attempt the storage deletes (which are no-ops for already-deleted keys) and then succeed on the DB delete.

**Test:** Delete an album with 5 items while one storage delete is artificially failing. Verify the other 4 storage objects are deleted. Re-run deletion. Verify the remaining object is deleted and the album is removed from the DB.

### Prune Job Idempotency

**Risk:** Prune job crashes halfway through processing expired albums.

**Mitigation:** Each album is processed independently. The prune job queries for all albums where `expires_at < NOW()`. If it crashes after processing 3 of 10 expired albums, the next run finds the remaining 7 and processes them. Albums that were already fully deleted are no longer in the query results.

Within a single album: storage deletes happen before the DB delete. If a storage delete fails, the album row is preserved and will be retried on the next run.

**Test:** Create 10 expired albums. Kill the prune job after 5 are processed. Run prune again. Verify all 10 are cleaned up with no errors.

### API Key Regeneration Idempotency

**Risk:** User clicks "regenerate" twice quickly.

**Mitigation:** API key regeneration is a single transaction: delete old key row, insert new key row. The second request either finds no old key to delete (harmless) or deletes the key created by the first request (the user gets a new key, which is what they asked for). The user only sees the raw key from whichever response their UI renders last.

**Test:** Send two concurrent regeneration requests. Verify exactly one API key exists in the DB afterward and it matches one of the two responses.

### Event Bus Idempotency

**Risk:** An event listener could be called twice if the event bus implementation changes or if a listener re-emits.

**Mitigation:** Event listeners are designed to be safe to call multiple times. Audit writes are insert-only (duplicate audit entries are noise but not corruption). Metrics increments are additive (a double-count is visible but not destructive). Thumbnail enqueue is idempotent because the thumbnail worker itself is idempotent. Quota updates are recalculated from the source of truth (sum of media rows), not incremented.

**Test:** Emit the same `MediaUploaded` event twice. Verify two audit entries exist (acceptable), metrics are incremented twice (acceptable), and only one thumbnail is generated (worker idempotency).

-----

## 23. Graceful Degradation

This section documents the expected behavior when each dependency is unavailable. The design goal is that the application **always does the most it can** rather than failing entirely when one component is down.

### Redis-Free Mode (Raspberry Pi / Lightweight Deployments)

When `REDIS_URL` is empty or unset, the application starts in Redis-free mode. This is a **first-class supported configuration**, not a degraded fallback. It's designed for single-user Raspberry Pi deployments or development environments where running Redis is unnecessary overhead.

| Subsystem | Normal (Redis available) | Redis-Free Mode |
|---|---|---|
| **Sessions** | Signed token stored in Redis; server-side revocation on logout | Signed cookie via `itsdangerous`; no server-side revocation. Logout clears the cookie but a stolen token remains valid until expiry. |
| **Rate limiting** | Redis counters per IP+UA / per user | Disabled entirely. No rate limiting on any endpoint. |
| **Task queue** | arq dispatches to separate worker processes via Redis | Synchronous in-process execution. `enqueue()` calls the task function directly. Upload requests block until thumbnail generation completes. |
| **Worker services** | `worker-thumbnails` and `worker-cleanup` run as separate containers | Not needed. All tasks run in the app process. Use `docker-compose.pi.yml` to disable workers. |
| **Readiness probe** | Redis checked as dependency | Redis check skipped; reports `"skipped"` in health response. |

### Degradation Matrix

| Dependency Down | Uploads | Media Serving | Album Pages | Thumbnails | Admin Panel | Auth |
|---|---|---|---|---|---|---|
| **PostgreSQL** | ❌ Rejected (503) | ❌ No metadata lookup | ❌ No album data | ❌ No media rows | ❌ No data | ❌ No user lookup |
| **Storage backend** | ❌ Can't store files | ❌ Can't stream bytes | ⚠️ Page loads, images broken | ❌ Can't read originals | ✅ Works (no storage needed) | ✅ Works |
| **Redis (configured)** | ⚠️ Allowed (rate limits fail open) | ✅ Works | ✅ Works | ⚠️ New tasks can't enqueue; existing workers stall | ✅ Works | ❌ Sessions fail closed; users logged out |
| **Redis (not configured)** | ✅ Works (no rate limiting) | ✅ Works | ✅ Works | ✅ Sync in-process | ✅ Works | ✅ Signed cookies |
| **CDN (Cloudflare)** | ✅ Works (uploads bypass CDN) | ⚠️ All traffic hits origin; slower but functional | ⚠️ Higher origin load | ✅ Works | ✅ Works | ✅ Works |
| **arq workers crashed** | ✅ Uploads succeed | ✅ Works | ⚠️ Thumbs show loading placeholder indefinitely | ❌ Queue grows until workers restart | ✅ Works | ✅ Works |
| **PgBouncer** | ❌ Same as PostgreSQL (app can't reach DB) | ❌ Same as PostgreSQL | ❌ Same as PostgreSQL | ❌ Same as PostgreSQL | ❌ Same as PostgreSQL | ❌ Same as PostgreSQL |

**Legend:** ✅ Works normally | ⚠️ Degraded but functional | ❌ Unavailable

### Key Design Decisions

**PostgreSQL is the only hard dependency.** If Postgres is down, nothing works. This is acceptable — Postgres is the source of truth for all application state.

**Storage backend failure is partial.** Album pages can still render (from DB metadata) even if images can't load. This is better than returning a 500 for the entire page.

**Redis failure is asymmetric by design.** Rate limiting fails open (allow traffic, risk abuse) because rejecting legitimate uploads during a Redis blip is worse than temporarily allowing extra traffic. Sessions fail closed (reject auth, require re-login) because serving authenticated requests when you can't validate the session token is a security risk.

**CDN failure is transparent.** If Cloudflare goes down (rare but possible), traffic routes directly to the origin. Performance degrades but nothing breaks, because the origin serves the same responses the CDN was caching.

**Worker failure is visible but non-blocking.** Uploads succeed even when workers are dead — the album appears immediately with loading placeholders. Thumbnails generate when workers come back. This is vastly better than blocking uploads on worker availability.

-----

## 24. Scaling Path

This section documents the upgrade path from single-node self-hosting to managed cloud infrastructure at web scale. Each step is independent — pick the ones that address your current bottleneck.

### Phase 1: Managed Infrastructure (Minimal Code Changes)

These changes are pure configuration — no application code modifications required.

| Change | What to do | Impact |
|---|---|---|
| **Garage → S3/R2** | Change `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`. Migrate objects with `rclone sync`. | Eleven nines durability, cross-region replication, no disk management |
| **PostgreSQL → RDS/Aurora** | Change `DATABASE_URL`. Use `pg_dump`/`pg_restore` to migrate. | Automated failover, PITR backups, read replicas |
| **Redis → ElastiCache** | Change `REDIS_URL`. | Replication, automated failover |
| **Add Cloudflare CDN** | Point DNS to Cloudflare. Zero code changes. See [§8](#8-cdn--edge-caching). | 95%+ of media requests served from edge; origin load drops dramatically |

### Phase 2: Horizontal Scaling

| Change | What to do | Impact |
|---|---|---|
| **Multiple app instances** | Deploy behind ALB or nginx load balancer. App is already stateless (sessions in Redis, blobs in S3). Correlation IDs work across instances — load balancer doesn't need sticky sessions. | Handle more concurrent requests |
| **Scale thumbnail workers** | Run more `worker-thumbnails` instances. Queue is shared via Redis. | Reduce thumbnail latency under load |
| **Container orchestration** | Move from Docker Compose to ECS / Kubernetes. Use liveness/readiness probes from [§20](#20-deployment). | Autoscaling, health checks, rolling deploys |

### Phase 3: Architecture Upgrades (Requires Implementation Work)

| Change | What to do | Impact |
|---|---|---|
| **Presigned uploads** | Client uploads directly to S3 via presigned URL; notifies API on completion. API validates and creates DB records. | Removes app servers as upload bottleneck for large video files |
| **arq → SQS** | Swap TaskQueue implementation. Task functions unchanged. SQS provides native dead letter queues and redrive. Correlation IDs pass through as SQS message attributes. | More robust job processing, better visibility, no Redis dependency for jobs |
| **Prometheus metrics** | Set `METRICS_BACKEND=prometheus`. All existing metrics appear in Grafana. | Real dashboards and alerting |
| **Audit log migration** | Swap AuditWriter/AuditReader implementation to Elasticsearch or Loki. Correlation ID column enables full request tracing in the new system. | Better log querying, retention, and aggregation at scale |

### Phase 4: Global Scale

| Change | What to do | Impact |
|---|---|---|
| **Multi-region reads** | S3 cross-region replication + CDN handles this automatically for media. PostgreSQL read replicas in secondary regions for metadata. | Low-latency reads globally |
| **Multi-region writes** | Aurora Global Database or equivalent. Single-region primary for writes with fast failover. | Write availability across regions |
| **Dedicated rate limiting** | WAF / API gateway layer for DDoS protection before traffic reaches application. | Protection against volumetric attacks |
| **Event bus → external broker** | Replace in-process event bus with SNS/SQS or Kafka for cross-service event delivery. Same event types, same listeners, different transport. | Decouple services; enable independent deployment |

-----

## 25. Out of Scope

- Password reset via email
- Private / password-protected albums
- Video transcoding
- Deduplication
- Discovery / explore / public gallery
- Comments or social features
- Multiple simultaneous storage backends
- Mobile native app
- oEmbed / embed codes
- Notifications of any kind

-----

## 26. Future Work

### Designed In From Day One (Schema/Abstraction Ready; UI Optional)

| Feature | What's already in place | What's needed to activate |
|---|---|---|
| Album cover selection | `cover_media_id` column in schema; cover resolution function handles null | UI button: "Set as cover" → `PATCH /album/{id}` |
| Drag-to-reorder | Gap-based `position` ordering; `PATCH /album/{id}/order` endpoint | Sortable.js on the album page |
| Additional SSO providers | `user_sso_links` table is provider-agnostic | New OAuth client config + one route per provider |
| Local filesystem storage | `StorageBackend` abstraction | `LocalFilesystemBackend` implementation (~50 lines) |
| Real AWS S3 / R2 | `StorageBackend` abstraction | Change `S3_ENDPOINT_URL` and credentials |
| SQS task queue | `TaskQueue` abstraction, plain task functions | New `SQSTaskQueue` implementation + consumer service |
| Prometheus metrics | `Metrics` abstraction, all metrics already instrumented | Set `METRICS_BACKEND=prometheus`, add `/metrics` endpoint |
| External audit log | `AuditWriter`/`AuditReader` abstraction, consistent metadata schema | New implementation targeting Elasticsearch / Loki / CloudWatch |
| CDN | Correct `Cache-Control` headers on all responses | Point DNS to Cloudflare; zero code changes |
| New media formats | `MediaProcessor` registry, standard pipeline interface | Write one processor class per format, register it |
| Webhooks | Event bus with typed domain events | Add webhook listener that POSTs events to configured URLs |
| External event broker | Typed domain events with serializable payloads | Replace in-process EventBus with SNS/SQS or Kafka adapter |

### Requires More Design When Needed

- **Presigned uploads:** Client-side upload flow to S3, finalize endpoint to validate and create DB records. Removes app server as upload bottleneck for large files.
- **Email integration:** Requires SMTP config, password reset token table, and expiry warning jobs.
- **Garage → S3 migration:** `rclone sync garage://imghost/ s3://your-bucket/` migrates all objects; update config; zero application code changes.
- **Orphan storage scan:** Periodic job that lists all keys in the storage bucket, compares against `storage_key` and `thumb_key` columns in the DB, and deletes keys with no matching row. Safety net for partial upload failures. Low priority but good hygiene.
