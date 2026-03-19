# Implemented But Different From `DESIGN.md`

This file captures features that are present in the codebase but materially differ from the behavior described in [`DESIGN.md`](/home/james/imghost/DESIGN.md). It excludes items that are simply not implemented yet, such as Redis-backed sessions/rate limits and OAuth/SSO.

## 1. Authenticated Album Management Is Narrower Than Designed

The design says logged-in owners can add items to existing albums and manage their own albums by ownership.

Current code differs:

- Authenticated uploads are forced to exactly one file and always create a new album.
  - [`src/imghost/main.py:639`](/home/james/imghost/src/imghost/main.py#L639)
- Album edits, cover changes, reordering, and per-media deletion currently authorize via `delete_token`, not owner session/admin identity.
  - [`src/imghost/main.py:1331`](/home/james/imghost/src/imghost/main.py#L1331)
  - [`src/imghost/main.py:1352`](/home/james/imghost/src/imghost/main.py#L1352)
  - [`src/imghost/main.py:1372`](/home/james/imghost/src/imghost/main.py#L1372)
  - [`src/imghost/service.py:355`](/home/james/imghost/src/imghost/service.py#L355)
  - [`src/imghost/service.py:412`](/home/james/imghost/src/imghost/service.py#L412)
  - [`src/imghost/service.py:454`](/home/james/imghost/src/imghost/service.py#L454)

## 2. Password Hashing Does Not Match The Design

The design specifies bcrypt-hashed passwords.

Current code uses plain SHA-256 for password hashing:

- [`src/imghost/service.py:998`](/home/james/imghost/src/imghost/service.py#L998)

## 3. Session Cookies Are Not `Secure`

The design specifies cookie flags `httponly; secure; samesite=lax`.

Current code sets cookies with `secure=False`:

- [`src/imghost/main.py:380`](/home/james/imghost/src/imghost/main.py#L380)
- [`src/imghost/main.py:394`](/home/james/imghost/src/imghost/main.py#L394)

## 4. Rate Limiting Behavior Differs From The Design

The design says rate limiting should use Redis when available, and be disabled entirely without Redis.

Current code always wires in an in-process limiter and enforces limits in memory:

- [`src/imghost/main.py:52`](/home/james/imghost/src/imghost/main.py#L52)
- [`src/imghost/rate_limits.py:38`](/home/james/imghost/src/imghost/rate_limits.py#L38)

## 5. Upload Size Limits Are Simplified

The design calls for separate maximum sizes for images and videos, with videos allowed up to 500 MB.

Current code exposes one global `MAX_UPLOAD_BYTES` limit and applies it to every upload:

- [`src/imghost/config.py:47`](/home/james/imghost/src/imghost/config.py#L47)
- [`src/imghost/service.py:146`](/home/james/imghost/src/imghost/service.py#L146)

## 6. Album ZIP Download Is Buffered, Not Streamed

The design says ZIP download should be streamed on the fly.

Current code builds the full archive in memory before returning it:

- [`src/imghost/main.py:973`](/home/james/imghost/src/imghost/main.py#L973)
- [`src/imghost/service.py:522`](/home/james/imghost/src/imghost/service.py#L522)

## 7. ShareX Config Download Requires API-Key Authentication

The design describes ShareX config download as a Settings action for a signed-in user.

Current code rejects normal session-authenticated requests unless the request itself is authenticated with the API key:

- [`src/imghost/main.py:1050`](/home/james/imghost/src/imghost/main.py#L1050)

## 8. Admin Bootstrap CLI Differs From The Design

The design calls for a `create-admin` command that prompts interactively for a password.

Current code instead provides `create-user --admin`, and that path creates a user with `password_hash=None`:

- [`src/imghost/__main__.py:22`](/home/james/imghost/src/imghost/__main__.py#L22)
- [`src/imghost/__main__.py:67`](/home/james/imghost/src/imghost/__main__.py#L67)

## 9. Media Responses Do Not Explicitly Forward `Content-Length`

The design says media responses should include `Content-Length`.

The storage layer computes it, but the response path does not forward it explicitly:

- [`src/imghost/storage.py:15`](/home/james/imghost/src/imghost/storage.py#L15)
- [`src/imghost/storage.py:105`](/home/james/imghost/src/imghost/storage.py#L105)
- [`src/imghost/main.py:947`](/home/james/imghost/src/imghost/main.py#L947)
