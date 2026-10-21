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

1. Copy the `compose.beginner.yaml`, `.env.example.beginner` file and the `docker` folder into a folder on your computer
   
2. Rename the beginner env file:

```bash
mv .env.example.beginner .env.beginner
```

3. Edit `.env.beginner` and set it up for your system and network. Set your hostname/IP address, etc. 
  
4. Start the stack:

```bash
docker compose -f compose.beginner.yaml --env-file .env.beginner up -d
```

5. Open `http://your-server:8000`

### Standard Install

1. Copy the `compose.yaml`, `.env.example` file and the `docker` folder into a folder on your computer

2.  Rename the standard env file:

```bash
mv .env.example .env
```

3. Edit `.env` and set it for your system and network. Set your hostname/IP address, etc. 
  
4. Start the published images:

```bash
docker compose -f compose.yaml --env-file .env up -d
```

This is the safer default stack. It only publishes the app port to the host. PostgreSQL, PgBouncer, Redis, and Garage stay on the internal Compose network.

If you want to build locally instead:

```bash
docker compose -f compose.build.yaml --env-file .env up -d
```

Use the build stack for local development, debugging, and the test workflow. Unlike `compose.yaml`, it also publishes PostgreSQL, PgBouncer, Redis, and Garage ports on the host for local inspection and test tooling.

## Optional public-page analytics

Set `UMAMI_WEBSITE_ID` to your website UUID and `UMAMI_HOST_URL` to the HTTPS
collector origin to enable Umami on the home page, public albums and public user
galleries. Set `UMAMI_DOMAINS` to comma-separated production hostnames (defaults
to the hostname in `BASE_URL`). Tracking is absent on auth, management, settings
and admin pages. Signed-in visits to public pages count normally.

The collector must serve `/init.js` defining `window.umamiBeforeSend` before
loading Umami's `/script.js`, and expose `/api/send`. The initializer must normalize
public album IDs and usernames, use a fixed site title, strip queries/fragments,
sanitize referrers and suppress private routes. The tracker is loaded only by the
initializer so collection fails closed when it cannot load. CSP permits only the
configured origin, and only on tracked pages. Query strings and fragments are
excluded in the tracker as well. No user identification or event properties are
sent by this integration.

Restart/recreate the app after changing these settings. The optional settings are
already passed through Compose's env file; no Compose edits are required.

## Docker Compose Files

- [`compose.beginner.yaml`](compose.beginner.yaml): simple single-app stack
- [`compose.yaml`](compose.yaml): standard pull-based stack with only the app port published
- [`compose.build.yaml`](compose.build.yaml): standard local-build stack with extra internal-service ports published for debugging and local tests
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

The app also has runtime-tunable upload and auth rate limits. See [docs/configuration.md](docs/configuration.md) and [docs/security.md](docs/security.md).

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
