# Page Routes

## Principles

- Separate public viewing routes, authenticated product routes, admin routes, and API routes.
- Keep public album viewing and owner album editing as different pages.
- Prefer redirects for logged-out page access rather than showing broken private pages.
- Keep public URLs stable even if the internal UI structure evolves.

## Route Map

### Public

- `/`
  Landing page with branding, quick upload, and `Sign in` / `Sign up` buttons.
- `/login`
  Sign-in page.
- `/register`
  Sign-up page.
- `/a/{album_id}`
  Public album page.
- `/u/{username}`
  Public user gallery page.
- `/manage/{album_id}`
  Token-backed anonymous album management page.

### Authenticated Product

- `/dashboard`
  Signed-in home with upload and quick navigation.
- `/albums`
  Owned album list.
- `/albums/{album_id}`
  Owned album editor and management page.
- `/settings`
  Account and settings page.

### Admin

- `/admin`
  Admin overview or index page.
- `/admin/users`
  User management page.
- `/admin/users/new`
  Dedicated create-user page.
- `/admin/albums`
  Album moderation page.
- `/admin/config`
  Runtime config page.
- `/admin/ops`
  Runtime and audit operations page.

### Optional Later

- `/upload`
  Dedicated upload page if `/` becomes too overloaded.
- `/admin/audit`
  Audit page when that surface becomes a priority.

## Route Behavior

### `/`

- Always accessible.
- Logged-out users see the landing page, upload entry, and auth entry points.
- Logged-in users can still access it, but it should not replace `/dashboard` as the main signed-in home.

### `/login`

- Logged-out users see the login form.
- Logged-in users should redirect to `/dashboard`.

### `/register`

- Logged-out users see the registration form when registration is enabled.
- Logged-in users should redirect to `/dashboard`.
- If registration is disabled, the page should show a disabled state or redirect to `/login`.

### `/dashboard`

- Requires authentication.
- Logged-out users should redirect to `/login`.
- This should be the main post-login landing page.

### `/albums`

- Requires authentication.
- Logged-out users should redirect to `/login`.
- This should be a distinct album-list page, not the general signed-in home.

### `/albums/{album_id}`

- Requires authentication.
- Logged-out users should redirect to `/login`.
- If the album exists but the user does not own it and is not an admin, redirect to `/a/{album_id}` only if the album is publicly viewable.
- Otherwise return or render a forbidden state.

### `/a/{album_id}`

- Public and stable.
- The same page should work for owners, anonymous users, and other logged-in users.
- If the owner is viewing it, the page can include a subtle `Edit this album` link to `/albums/{album_id}`.
- If the browser has saved anonymous token access for that album, the page can include a subtle `Manage Album` link to `/manage/{album_id}?token=...`.

### `/manage/{album_id}`

- Token-backed and stable enough to save/share deliberately.
- Missing or invalid token should return a denied state.
- This should reuse the owner album workspace shape, but all mutations must flow through the anonymous delete token instead of user auth.

### `/settings`

- Requires authentication.
- Logged-out users should redirect to `/login`.

### `/admin*`

- Require admin authentication.
- Logged-out users should redirect to `/login`.
- Authenticated non-admin users should see a denied state or redirect away from the page surface.
- API routes should continue to return `403`.

## Page Responsibilities

### `/`

Should include:

- product framing
- quick anonymous upload
- sign-in button
- sign-up button
- a concise explanation of what the app does

Should not try to be:

- the main signed-in dashboard
- the full auth form page

### `/login`

Should include:

- username or email login form
- password field
- remember-me control
- link to sign up

### `/register`

Should include:

- username
- email
- password
- submit
- link to sign in

### `/dashboard`

Should include:

- links to `/albums`
- link to `/settings`
- primary authenticated upload box
- concise account or usage summary
- recent albums or other quick-resume content
- any other lightweight dashboard panels that make the signed-in home useful

Should not try to be:

- the full album-management workspace
- the full settings page

### `/albums`

Should include:

- owned album cards or list
- storage usage summary
- empty state for users with no albums
- clear route into `/albums/{album_id}`
- compact per-album owner actions such as public-link access, ZIP download, and delete

Should not include:

- the main upload box

### `/albums/{album_id}`

Should include:

- album title and metadata
- current cover
- media grid or list
- reorder UI
- cover selection UI
- append-files UI
- ZIP download
- media delete actions
- album delete action
- inline title editing and a strong owner-focused share control
- media preview/lightbox behavior for full-size viewing

### `/a/{album_id}`

Should include:

- public album presentation
- full-media previews for images
- thumbnail-backed preview flow for videos
- ZIP download
- split-link media actions
- owner edit link when the viewer owns the album
- public delete or manage affordance only when token context is available

### `/settings`

Should include:

- current-user summary
- quota and usage
- album and media counts
- API key reveal or rotation
- ShareX config download
- password change
- account deletion
- logout

### `/admin/users`

Should include:

- paginated user table or list
- per-user summary
- link to `/admin/users/new`
- suspend or unsuspend
- quota editing
- per-user rate-limit editing
- reset password
- delete user

### `/admin/users/new`

Should include:

- create-user form
- optional admin toggle
- initial quota and per-user rate limit overrides
- clear route back to `/admin/users`

### `/admin/albums`

Should include:

- album moderation table
- owner
- title
- item count
- size
- created date
- expiry state
- set or clear expiry
- delete album

### `/admin/config`

Should include:

- runtime config values
- editable values
- locked values with explicit locked state
- rate-limit settings
- registration and anonymous-upload settings

### `/admin/ops`

Should include:

- global storage stats
- per-user usage breakdown
- anonymous storage usage
- runtime-status data
- Redis, task, and worker visibility

## Backend Mapping

### Public and Auth

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/logout`
- `GET /api/v1/user/me`
- `GET /api/v1/user/me/albums`
- `POST /api/v1/user/me/api-key`
- `PATCH /api/v1/user/me/password`
- `GET /api/v1/user/me/sharex-config`
- `DELETE /api/v1/user/me`
- `POST /api/v1/upload`
- `GET /api/v1/album/{album_id}`
- `PATCH /api/v1/album/{album_id}`
- `PATCH /api/v1/album/{album_id}/order`
- `DELETE /api/v1/media/{media_id}`
- `DELETE /api/v1/album/{album_id}`
- `GET /api/v1/album/{album_id}/zip`
- `GET /api/v1/album/{album_id}/delete`
- `GET /a/{album_id}`
- `GET /u/{username}`
- `GET /i/{id}.{ext}`
- `GET /t/{id}.{ext}`

### Admin

- `GET /api/v1/admin/users`
- `GET /api/v1/admin/users/{user_id}`
- `GET /api/v1/admin/users/{user_id}/stats`
- `GET /api/v1/admin/users/{user_id}/albums`
- `POST /api/v1/admin/users`
- `PATCH /api/v1/admin/users/{user_id}`
- `POST /api/v1/admin/users/{user_id}/reset-password`
- `DELETE /api/v1/admin/users/{user_id}`
- `GET /api/v1/admin/albums`
- `PATCH /api/v1/admin/albums/{album_id}`
- `DELETE /api/v1/admin/albums/{album_id}`
- `GET /api/v1/admin/config`
- `PATCH /api/v1/admin/config`
- `GET /api/v1/admin/stats`
- `GET /api/v1/admin/runtime-status`

## Implementation Notes

1. Keep API routes unchanged and layer new page routes on top.
2. Add dedicated HTML routes for `/login`, `/register`, `/dashboard`, `/albums`, `/albums/{album_id}`, `/admin/users`, `/admin/albums`, `/admin/config`, and `/admin/ops`.
3. Keep current utility pages during transition.
4. Keep `/dashboard` as a real page and stop treating it as a temporary redirect target.
5. Let `/admin` become an overview/index page instead of a giant all-in-one screen.
6. Retire dev-only utility pages once the real user-facing flow replaces them.
7. Normalize auth redirect behavior with shared helpers for user and admin page routes.
8. Preserve `/a/{album_id}` and `/u/{username}` as stable public URLs.

## Remaining Backend Gaps

At this point, there are no major backend blockers for the main UI plan.

The remaining gaps are mostly frontend design and implementation decisions:

- exact navigation model
- exact visual system
- whether `/admin` is a real dashboard or a directory page
- how rich the first-pass album editor should be

## Open Decisions

1. `/` should remain accessible to logged-in users instead of auto-redirecting to `/dashboard`.
2. `/albums/{album_id}` should redirect non-owners to `/a/{album_id}` only when the album is publicly viewable.
3. `/admin` should start as a lightweight real dashboard with summary panels and links into the admin subpages.

## Suggested Implementation Order

1. `/login`
2. `/register`
3. `/dashboard`
4. `/albums`
5. `/albums/{album_id}`
6. `/settings`
7. `/admin/users`
8. `/admin/albums`
9. `/admin/config`
10. `/admin/ops`

## UI Priorities

Priority 1:

- `/`
- `/login`
- `/register`
- `/dashboard`
- `/albums`
- `/albums/{album_id}`
- `/settings`
- `/a/{album_id}`

Priority 2:

- `/admin/users`
- `/admin/albums`
- `/admin/config`
- `/admin/ops`

Priority 3:

- deeper anonymous token-management UX
- audit UI
- more advanced operational visualization
