# imghost

`imghost` is a self-hosted image and video hosting app with anonymous uploads, user accounts, public albums, admin tooling, and practical deployment options for both Docker Compose and direct host installs.

## Features

- Anonymous uploads into shareable public albums
- User accounts with browser sessions and API keys
- Public album pages and per-user galleries
- Album management:
  - rename albums
  - reorder media
  - set cover images
  - delete items or entire albums
- Admin UI and admin APIs
- Image, animated image, SVG, and video support
- Async thumbnail generation
- ShareX export
- Docker Compose and non-Docker deployment options

## Quick Start

If you do not want to use Docker, see [docs/non-docker-deployment.md](docs/non-docker-deployment.md).

Choose one:

- Beginner Docker stack:
  - simplest install
  - no Redis
  - no PgBouncer
  - no separate worker or scheduler containers
  - good for local, LAN, or first-time self-hosting
- Standard Docker stack:
  - full split-service deployment
  - Redis, PgBouncer, dedicated workers, and scheduler
  - better for heavier or more production-like setups

### Beginner Install

1. Copy the beginner env file:

```bash
cp .env.example.beginner .env.beginner
```

2. Edit `.env.beginner` and set it for your system

3. Start the stack:

```bash
docker compose -f compose.beginner.yaml --env-file .env.beginner up -d
```

4. Open `http://your-server:8000`

### Standard Install

1. Copy the standard env file:

```bash
cp .env.example .env
```

2. Edit `.env` and set it for your system

3. Pull and start the published images:

```bash
docker compose -f compose.yaml --env-file .env pull
docker compose -f compose.yaml --env-file .env up -d
```

If you want to build locally instead:

```bash
docker compose -f compose.build.yaml --env-file .env up -d
```

## Docker Compose Files

- [`compose.beginner.yaml`](compose.beginner.yaml): simple single-app stack
- [`compose.yaml`](compose.yaml): standard pull-based stack
- [`compose.build.yaml`](compose.build.yaml): standard local-build stack
- [`compose.with-nginx.yaml`](compose.with-nginx.yaml): optional nginx companion

Service-specific runtime files live under [`docker/`](docker/).

## Images

Published images are pushed to GitHub Container Registry:

- `ghcr.io/jamesyc/imghost:latest`
- `ghcr.io/jamesyc/imghost-garage-init:latest`

The publish workflow is in [`.github/workflows/publish-image.yml`](.github/workflows/publish-image.yml).

## Configuration

Main env files:

- [`.env.example`](.env.example)
- [`.env.example.beginner`](.env.example.beginner)

Common things to configure:

- `BASE_URL`
- `SECRET_KEY`
- database password
- Redis password for the standard stack
- Garage/S3 credentials
- public origin settings if running behind a reverse proxy

For full configuration details, see [docs/configuration.md](docs/configuration.md).

## Reverse Proxy

If you want nginx inside the Compose stack:

```bash
docker compose \
  -f compose.yaml \
  -f compose.with-nginx.yaml \
  --env-file .env \
  up -d
```

That config uses [`docker/nginx/nginx-site.conf`](docker/nginx/nginx-site.conf).

For deployment guidance, see:

- [docs/docker-deployment.md](docs/docker-deployment.md)
- [docs/non-docker-deployment.md](docs/non-docker-deployment.md)
- [docs/setup.md](docs/setup.md)
- [docs/reverse-proxy.md](docs/reverse-proxy.md)

## Development

Run tests with:

```bash
uv run pytest -q
```

Useful commands:

```bash
uv run python -m imghost init-storage
uv run python -m imghost run-worker
```

## License

No license file is currently included in this repository.
