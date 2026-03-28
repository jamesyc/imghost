# Docs Index

This directory holds implementation-oriented documentation for the current `imghost` codebase.

## Documents

- [overview.md](/home/james/imghost/docs/overview.md)
  High-level summary of what the app does, the current `src/imghost/web` module layout, the main moving parts, and the supported deployment shapes.

- [setup.md](/home/james/imghost/docs/setup.md)
  Opinionated step-by-step setup guide for a practical Docker plus reverse-proxy deployment.

- [configuration.md](/home/james/imghost/docs/configuration.md)
  Reference for the current env/config surface, including strict vs local public-origin behavior, OAuth toggles, Docker settings, and runtime config stored in PostgreSQL.

- [cli.md](/home/james/imghost/docs/cli.md)
  Operational command reference for the current `python -m imghost` subcommands and how they are used.

- [docker-deployment.md](/home/james/imghost/docs/docker-deployment.md)
  Description of the beginner and advanced Docker Compose stacks, service roles, startup flow, volumes, and Redis/Garage bootstrap behavior.

- [secrets-and-rotation.md](/home/james/imghost/docs/secrets-and-rotation.md)
  Practical notes on which secrets are easy to rotate in place and which behave more like bootstrap-time values.

- [reverse-proxy.md](/home/james/imghost/docs/reverse-proxy.md)
  How public-origin handling works, how local direct-access differs from strict deployment mode, how trusted proxy CIDRs affect forwarded-header trust, and what a reverse proxy should send.

- [authentication.md](/home/james/imghost/docs/authentication.md)
  Browser sessions, browser-session CSRF policy, local auth, Google OAuth, API keys, ShareX export behavior, and the current auth model and caveats.

- [storage.md](/home/james/imghost/docs/storage.md)
  Filesystem and S3-compatible storage backends, media serving behavior, and ZIP streaming.

- [background-jobs.md](/home/james/imghost/docs/background-jobs.md)
  Task queue modes, split worker and scheduler behavior, Redis fallback semantics, and thumbnail recovery.

- [operations.md](/home/james/imghost/docs/operations.md)
  Health endpoint contract, runtime status, metrics, probe semantics, low-noise logging model, degraded-mode behavior, and browser-session mutation troubleshooting.

- [testing.md](/home/james/imghost/docs/testing.md)
  Test command, test database safety model, current suite size, OAuth/metrics coverage, browser-session CSRF coverage, and what the suite expects from the local environment.

- [api-overview.md](/home/james/imghost/docs/api-overview.md)
  Route map for the main HTML, API, admin, and health endpoints.

- [ui-surfaces.md](/home/james/imghost/docs/ui-surfaces.md)
  Short description of what each browser UI page is for and how responsibilities are split.

- [sharex.md](/home/james/imghost/docs/sharex.md)
  ShareX config export behavior, auth modes, and how the generated `.sxcu` is built.

- [security.md](/home/james/imghost/docs/security.md)
  Security-relevant implementation notes around uploads, auth, sessions, and proxy trust.

- [roadmap.md](/home/james/imghost/docs/roadmap.md)
  Forward-looking work that is intentionally separate from current-state documentation.
