# Route Structure

This file describes the current route structure as implemented today.

## Page Routes

### Public Pages

- `/`: upload-first landing page; signed-in users can still upload here
- `/login`: login page; authenticated users are redirected to `/dashboard`
- `/register`: registration page; authenticated users are redirected to `/dashboard`
- `/a/{album_id}`: public album page
- `/u/{username}`: public user album list
- `/manage/{album_id}?token=...`: token-backed anonymous album management page using the shared workspace template
- `/sharex/delete/{album_id}?token=...`: ShareX delete capability entry point; validates and redirects to confirmation
- `/sharex/delete/{album_id}/confirm`: ShareX delete confirmation page
- `/manifest.webmanifest`: web app manifest
- `/service-worker.js`: minimal service worker bootstrap

### Signed-In Pages

- `/dashboard`: signed-in home page
- `/albums`: signed-in owned album list
- `/albums/{album_id}`: owner-only album workspace
- `/settings`: signed-in account/settings page

### Admin Pages

- `/admin`: admin overview
- `/admin/users`: user management list
- `/admin/users/new`: create-user page
- `/admin/users/{user_id}`: admin user detail page
- `/admin/albums`: album moderation page
- `/admin/config`: runtime config page
- `/admin/ops`: operations page

## Route Behavior Notes

- Private signed-in pages redirect logged-out viewers to `/login?next=...`.
- Admin pages redirect logged-out viewers to `/login?next=...` and return `403` to authenticated non-admin users.
- `/login` and `/register` normalize `next` values to internal paths before the client uses them for post-auth redirects.
- `/register` can render a disabled state when runtime config disables registration.
- `/albums/{album_id}` returns `404` when the album is missing, expired, or not owned by the current user. It does not fall back to the public album page.
- `/manage/{album_id}` requires a valid delete token and reuses the owner workspace template in token mode.
- `/sharex/delete/{album_id}` never deletes on `GET`; it validates the capability URL, sets a short-lived confirmation cookie, and redirects to `/sharex/delete/{album_id}/confirm`.
- `/a/{album_id}` and `/u/{username}` are public page surfaces backed by page-context builders in [`src/imghost/web/page_views.py`](/home/james/imghost/src/imghost/web/page_views.py).

## API Route Groups

### Auth API

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/logout`
- `GET /auth/google/start`
- `GET /auth/{provider}/start`
- `GET /auth/google/callback`
- `GET /auth/{provider}/callback`

### Public and Shared Album API

- `POST /api/v1/upload`
- `GET /api/v1/album/{album_id}`
- `GET /api/v1/album/{album_id}/zip`
- `DELETE /api/v1/album/{album_id}`
- `PATCH /api/v1/album/{album_id}`
- `PATCH /api/v1/album/{album_id}/order`
- `DELETE /api/v1/media/{media_id}`

### ShareX Delete Flow

- `GET /sharex/delete/{album_id}`
- `GET /sharex/delete/{album_id}/confirm`
- `POST /sharex/delete/{album_id}/confirm`

These routes handle ShareX deletion for authenticated API-key uploads. The initial `delete_url` is a capability URL, but deletion happens only after the confirmation `POST`.

These endpoints serve both authenticated owner actions and token-backed anonymous management, depending on session state and `delete_token`.

### Authenticated User API

- `GET /api/v1/user/me`
- `GET /api/v1/user/me/albums`
- `POST /api/v1/user/me/api-key`
- `PATCH /api/v1/user/me/password`
- `GET /api/v1/user/me/sharex-config`
- `DELETE /api/v1/user/me`
- `POST /api/v1/user/me/oauth/google/disconnect`

### Admin API

- `GET /api/v1/admin/users`
- `POST /api/v1/admin/users`
- `GET /api/v1/admin/users/{user_id}`
- `PATCH /api/v1/admin/users/{user_id}`
- `DELETE /api/v1/admin/users/{user_id}`
- `POST /api/v1/admin/users/{user_id}/reset-password`
- `GET /api/v1/admin/users/{user_id}/stats`
- `GET /api/v1/admin/users/{user_id}/albums`
- `GET /api/v1/admin/albums`
- `PATCH /api/v1/admin/albums/{album_id}`
- `DELETE /api/v1/admin/albums/{album_id}`
- `GET /api/v1/admin/audit`
- `GET /api/v1/admin/config`
- `PATCH /api/v1/admin/config`
- `GET /api/v1/admin/stats`
- `GET /api/v1/admin/runtime-status`

### Media and Health

- `/i/{id}.{ext}`: original media streaming
- `/t/{id}.{ext}`: thumbnail streaming
- `/health/live`
- `/health/ready`
- `/metrics`

## Source of Truth

The current source of truth for route behavior is:

- [`src/imghost/web/pages.py`](/home/james/imghost/src/imghost/web/pages.py)
- [`src/imghost/web/auth.py`](/home/james/imghost/src/imghost/web/auth.py)
- [`src/imghost/web/public_api.py`](/home/james/imghost/src/imghost/web/public_api.py)
- [`src/imghost/web/user_api.py`](/home/james/imghost/src/imghost/web/user_api.py)
- [`src/imghost/web/admin_api.py`](/home/james/imghost/src/imghost/web/admin_api.py)
- [`tests/test_pages.py`](/home/james/imghost/tests/test_pages.py)
- [`tests/test_routes.py`](/home/james/imghost/tests/test_routes.py)
