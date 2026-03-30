# Setup

This is the practical deployment guide for running `imghost` on one machine with Docker Compose and a reverse proxy such as nginx.

It is intentionally opinionated and focuses on the current implementation, not every theoretical deployment shape.

For a simpler local or LAN setup, use the beginner stack described in [docker-deployment.md](/home/james/imghost/docs/docker-deployment.md) with [`compose.beginner.yaml`](/home/james/imghost/compose.beginner.yaml) and [`.env.beginner`](/home/james/imghost/.env.beginner).

## What this guide assumes

- one Linux host
- Docker and Docker Compose available
- nginx running on the host or elsewhere in front of the Docker stack
- one or more public hostnames pointing at that machine
- Garage used as the storage backend

If you want a different storage backend or a non-Docker deployment shape, use this guide as a reference and then adapt with:

- [configuration.md](/home/james/imghost/docs/configuration.md)
- [docker-deployment.md](/home/james/imghost/docs/docker-deployment.md)
- [reverse-proxy.md](/home/james/imghost/docs/reverse-proxy.md)

## 1. Copy the Docker env file

Start from:

- [`.env.example`](/home/james/imghost/.env.example)

Create:

- [`.env`](/home/james/imghost/.env)

Example:

```bash
cp .env.example .env
```

For the beginner stack instead:

```bash
cp .env.example.beginner .env.beginner
```

## 2. Set the minimum required values

At minimum, set these values in [`.env`](/home/james/imghost/.env):

### Public URL and proxy settings

- `BASE_URL`
- `TRUSTED_PUBLIC_ORIGINS`

For one hostname:

```env
BASE_URL=https://imghost.example.com
TRUSTED_PUBLIC_ORIGINS=https://imghost.example.com
```

For multiple hostnames:

```env
BASE_URL=https://imghost.example.com
TRUSTED_PUBLIC_ORIGINS=https://imghost.example.com,https://imghost.example2.com
```

Trusted proxy gating:

- leave this permissive at first:

```env
TRUSTED_PROXY_CIDRS_ENABLED=false
TRUSTED_PROXY_CIDRS=127.0.0.1/32,172.16.0.0/12
```

- once the proxy path is understood and stable, you can enable it:

```env
TRUSTED_PROXY_CIDRS_ENABLED=true
```

### Secrets

Set strong values for:

- `SECRET_KEY`
- `REDIS_PASSWORD`
- `POSTGRES_PASSWORD`
- `GARAGE_RPC_SECRET`
- `GARAGE_ADMIN_TOKEN`
- `GARAGE_METRICS_TOKEN`
- `S3_SECRET_ACCESS_KEY`

You should also replace:

- `S3_ACCESS_KEY_ID`

with a non-example value.

### Recommended cookie settings

For an HTTPS deployment:

```env
SESSION_COOKIE_SECURE=true
SESSION_REMEMBER_DAYS=30
```

### Recommended Redis/task settings

The current stack is designed around Redis-backed tasks:

```env
REDIS_URL=redis://redis:6379/0
REDIS_PASSWORD=<same password you configured above>
REDIS_MODE=auto
TASK_QUEUE_MODE=redis
THUMBNAIL_WORKER_COUNT=1
```

### Garage / S3 settings

If you are using the bundled Garage service, keep:

```env
STORAGE_BACKEND=garage
S3_ENDPOINT_URL=http://garage:3900
S3_BUCKET=imghost
S3_REGION=garage
```

and set your actual:

- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`

## 3. Understand what is first-boot sensitive

These values are easiest to set correctly before the first `docker compose up`:

- `POSTGRES_PASSWORD`
- `GARAGE_RPC_SECRET`
- `GARAGE_ADMIN_TOKEN`
- `GARAGE_METRICS_TOKEN`
- `GARAGE_ZONE`
- `GARAGE_CAPACITY`
- `GARAGE_KEY_NAME`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET`

If you change those later, especially after volumes already contain initialized data, read:

- [secrets-and-rotation.md](/home/james/imghost/docs/secrets-and-rotation.md)

## 4. Start the stack

Bring the stack up with:

```bash
docker compose -f compose.build.yaml --env-file .env up -d
```

This guide uses `compose.build.yaml` because it is the easiest stack for local setup, debugging, and test tooling. If you are deploying the standard pull-based stack instead, substitute `compose.yaml` in the commands below. The safer production-like default is `compose.yaml`, which keeps PostgreSQL, PgBouncer, Redis, and Garage off the host network.

This starts:

- app
- worker-thumbnails
- worker-cleanup
- worker-default
- scheduler
- pgbouncer
- postgres
- redis
- garage
- garage-init

## 5. What happens on first boot

On first successful startup:

- Postgres initializes its data directory
- PgBouncer starts and waits for Postgres
- Redis starts with AOF enabled and password auth if `REDIS_PASSWORD` is set
- Garage starts
- `garage-init` assigns layout, imports the S3 key, creates the bucket, and grants access
- the app container runs `python -m imghost init-storage` before starting uvicorn when `STORAGE_BACKEND=garage`
- the app starts as the `app` role
- the thumbnail worker re-enqueues recoverable thumbnails
- the worker services consume Redis-backed jobs
- the scheduler enqueues recurring cleanup onto the `cleanup` queue

## 6. Verify the stack

Check containers:

```bash
docker compose -f compose.build.yaml --env-file .env ps
```

Check health:

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
docker compose -f compose.build.yaml --env-file .env exec pgbouncer sh -lc 'pg_isready -h 127.0.0.1 -p 5432 -d "postgres://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:5432/pgbouncer"'
```

Expected:

- `/health/live` returns `ok`
- `/health/ready` returns JSON with `"ok": true`

If you have an admin API key, you can also inspect:

- `GET /api/v1/admin/runtime-status`

to verify:

- database connectivity
- storage health
- Redis reachability
- queue mode
- worker state
- scheduler state
- trusted public origins
- forwarded-header policy

## 7. Put nginx in front of it

The app should usually sit behind a reverse proxy that terminates HTTPS.

Important headers:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

Proxy upstream:

```nginx
proxy_pass http://127.0.0.1:8000;
```

If you want nginx inside the Compose stack instead of on the host, use the optional companion file:

```bash
docker compose \
  -f compose.build.yaml \
  -f compose.with-nginx.yaml \
  --env-file .env \
  up -d
```

The bundled Compose nginx config lives at [`docker/nginx/nginx-site.conf`](/home/james/imghost/docker/nginx/nginx-site.conf) and proxies to `app:8000` on the Docker network. Mount your certificate pair at `./certs/fullchain.pem` and `./certs/privkey.pem` before enabling it.

If nginx runs on the host instead, use [`docs/nginx-site.conf`](/home/james/imghost/docs/nginx-site.conf), which proxies to `127.0.0.1:8000`.

If nginx is on the same machine, start with:

- `TRUSTED_PROXY_CIDRS_ENABLED=false`

Then once you understand the proxy path, you can enable the trusted-proxy gate and restrict `TRUSTED_PROXY_CIDRS`.

## 8. Multi-domain setup

If both of these point to the same deployment:

- `imghost.example.com`
- `imghost.example2.com`

then use:

```env
BASE_URL=https://imghost.example.com
TRUSTED_PUBLIC_ORIGINS=https://imghost.example.com,https://imghost.example2.com
```

That allows generated links, ShareX configs, and other public URLs to reflect the actual trusted request hostname instead of collapsing everything to one domain.

## 9. Create your first user

You can create a user from the CLI:

```bash
python -m imghost create-user --username admin --email admin@example.com --admin
```

Then issue an API key:

```bash
python -m imghost issue-api-key --user-id <user-id>
```

Or use the browser registration flow if registration is enabled.

## 10. Common mistakes

### Wrong public URLs in responses

Usually means one of:

- `TRUSTED_PUBLIC_ORIGINS` does not include the actual hostname
- proxy headers are missing
- trusted-proxy gating is enabled but `TRUSTED_PROXY_CIDRS` does not match the proxy peer
- fallback to `BASE_URL` is happening

### Redis auth problems

Usually means one of:

- `REDIS_PASSWORD` in the app does not match the Redis container
- `REDIS_URL` points to the wrong host/db
- Redis was restarted with a new password but app/workers/scheduler were not restarted

### Postgres password change did not work

Usually means:

- you changed `POSTGRES_PASSWORD` in env only
- the existing Postgres data dir was already initialized with the old password

See:

- [secrets-and-rotation.md](/home/james/imghost/docs/secrets-and-rotation.md)

### Garage credentials changed but storage still fails

Usually means:

- Garage bootstrap/import state and app-side S3 credentials are now out of sync
- changing env alone was not enough for an already-initialized Garage deployment

## 11. Suggested next checks after setup

After the stack is up:

1. load `/`
2. register or create a user
3. upload a test image
4. verify album page, raw media URL, and thumbnail URL
5. verify ZIP download
6. verify `/health/ready`
7. verify Redis-backed worker processing by watching a thumbnail go from pending to ready
8. verify scheduler state in `/api/v1/admin/runtime-status`

For broader manual verification, use:

- [FEATURES.md](/home/james/imghost/FEATURES.md)
