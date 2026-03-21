# Could Improve

This file is intentionally not a blocker list. Everything here is working or at least acceptable for the current prototype, but could be improved.

## High Value

- Add richer settings-page account management beyond the current password, API key, ShareX, and delete-account utilities.
- Redesign admin to match the newer dashboard, albums, public pages, and settings surface.

## Security / Hardening

- Add stronger trusted-proxy / forwarded-header hardening beyond the current exact trusted-origin allowlist.
- Add stronger deployment docs for reverse proxy and forwarded header handling.
- Revisit API key lifecycle:
  - multiple active keys
  - labels
  - revocation history
- Add CSRF protection if browser-session usage grows beyond the current prototype shape.

## Storage / Media

- Forward explicit `Content-Length` where possible.
- Support streamed ZIP creation.
- Add more media formats only if there is a real need.
- Improve video compatibility handling and reporting.
- Add orphaned-object reconciliation tooling for storage cleanup.

## Product / UX

- Better session/browser UX than the remaining utility-style admin page and other older message surfaces.
- Better anonymous album management UX.
- Better admin UX for audit and runtime config.
- Better error surfacing on the remaining testing-oriented pages.

## Operations

- Structured logging
- metrics
- health/readiness documentation
- backup/restore docs
- multi-machine deployment docs beyond the current env-override pattern

## CLI

- Add a dedicated `create-admin` command.
- Allow CLI password creation or reset.
- Add safer environment validation commands.

## Docs

- Trim or archive stale parts of the original `DESIGN.md` over time.
- Add a short deployment guide for:
  - single-machine Docker
  - app on one host with remote Postgres/Garage
  - HTTPS reverse proxy
