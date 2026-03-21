# UX Implementation Plan

This document turns the UX and frontend decisions into a concrete implementation plan.

The chosen frontend direction is:

- FastAPI templates
- HTMX
- small focused vanilla JavaScript
- plain CSS

The implementation should replace the current utility UI without destabilizing the backend or changing public URLs.

## Goals

- replace inline HTML string pages with real templates
- preserve existing API routes and backend behavior
- preserve stable public routes like `/a/{id}` and `/u/{username}`
- keep the app Python-first with no Node frontend toolchain
- improve UX incrementally instead of attempting a full rewrite in one pass

## Non-Goals

- rewriting the backend API surface
- changing media URL structure
- introducing a separate frontend app
- building a client-side SPA router
- rebuilding every admin utility before the main user-facing pages

## Guiding Approach

The migration should be incremental and route-oriented.

Do not try to redesign and replatform every page at once.

Preferred pattern:

1. create template infrastructure
2. move one page at a time onto the new system
3. use HTMX where server-rendered fragments are natural
4. use JavaScript only where direct browser APIs are clearly better
5. keep old utility routes working until their replacements are complete

## Recommended File Structure

Add a template and static asset structure under the Python app package.

Suggested shape:

```text
src/imghost/
  templates/
    base.html
    partials/
      nav.html
      flash.html
      upload-panel.html
      album-card.html
      media-tile.html
    pages/
      home.html
      login.html
      register.html
      albums.html
      album-detail.html
      public-album.html
      user-gallery.html
      settings.html
      admin-index.html
      admin-users.html
      admin-albums.html
      admin-config.html
      admin-ops.html
  static/
    css/
      base.css
      pages.css
    js/
      upload.js
      album-detail.js
      lightbox.js
      copy.js
```

This does not need to be perfect on day one, but the important move is to stop embedding major page markup in Python string literals.

## Backend Infrastructure Phase

## Phase 1: Add Template And Static Support

Tasks:

- add Jinja2 template support to FastAPI
- mount a static files path
- create a shared template renderer/helper
- create a base layout template
- create shared partials for nav, alerts, and repeated UI blocks

Output:

- a clean page shell
- reusable template rendering primitives
- a place for page-specific and shared styles/scripts

Notes:

- the base layout should own typography, theme variables, navigation, and the common page container
- route handlers should stop assembling large HTML strings directly

Status:

- complete
- implemented with Jinja templates, mounted static assets, a shared base layout, shared nav, and a shared flash partial
- local static assets now use path-only `/static/...` URLs so they remain correct behind HTTPS reverse proxies and across multiple public domains

## Phase 2: Shared Page Utilities

Tasks:

- add shared auth-aware navigation helpers
- add shared page context helpers for current user, runtime flags, and base URL
- normalize redirects for authenticated and admin-only pages
- define a standard flash/message pattern for success and error states

Output:

- less duplicated route logic
- more consistent page behavior

Status:

- complete
- shared page context helpers, auth-aware nav, login redirect helpers, and a shared flash pattern are in place

## Route Migration Order

Follow the agreed route priority rather than bouncing between unrelated pages.

## Phase 3: Public Entry Pages

Pages:

- `/`
- `/login`
- `/register`

Why first:

- they establish the design system
- they define auth and upload entry behavior
- they have relatively limited management complexity

Implementation notes:

- build the landing page as the primary upload-first surface
- move login and register to dedicated templates
- support disabled states for registration and anonymous upload
- use HTMX for auth forms only if it improves UX; full POST + redirect is also acceptable

JavaScript needed here:

- drag and drop upload
- paste-to-upload
- upload progress
- success-state handoff

Status:

- complete for the current route set
- `/`, `/login`, and `/register` are template-backed
- auth is now routed through dedicated pages rather than embedded forms on the home page
- lightweight JavaScript exists for the current auth and upload interactions

## Phase 4: Signed-In Dashboard

Page:

- `/dashboard`

Why next:

- this is the main signed-in home
- it should gather the most common post-login actions in one place

Implementation notes:

- show upload entry point
- link clearly to `/albums`
- link clearly to `/settings`
- show a concise account and usage summary
- include a recent-albums or quick-resume panel
- design a useful empty and first-run state

HTMX candidates:

- recent album list refresh after upload
- lightweight summary-panel refreshes

JavaScript needed here:

- upload interaction reuse from home page

Status:

- planned
- `/dashboard` should be a real signed-in page, not a redirect
- it should own the primary authenticated upload surface

## Phase 5: Signed-In Album List

Page:

- `/albums`

Why after dashboard:

- it is still a core signed-in route, but it is narrower than the dashboard
- it should focus on browsing and resuming owned album work, not on being the general upload home

Implementation notes:

- show owned albums
- design a good empty state
- do not place the primary upload box here

HTMX candidates:

- lightweight filtering or sorting later

JavaScript needed here:

- only what the album-list interactions need

Status:

- complete
- `/albums` should remain an authenticated album list and navigation page
- the primary signed-in upload box belongs on `/dashboard`
- `/albums` now renders owned album cards only, with no primary upload box
- album cards link into owner management and expose public-link, ZIP, and delete actions

## Phase 6: Album Management Workspace

Page:

- `/albums/{id}`

Why this is its own phase:

- it is the most interaction-heavy page
- it combines presentation, mutation, and file operations

Implementation notes:

- build owner-management layout
- add album metadata editing
- add append-files flow
- add ZIP and public-link actions
- add media tile actions
- keep destructive actions controlled and explicit

HTMX candidates:

- title edits
- metadata refresh
- delete-media partial refresh
- cover selection updates

JavaScript needed here:

- append upload
- reorder mode
- drag-and-drop sorting
- clipboard copy
- preview/lightbox behavior

Important rule:

Do not try to force drag-reorder through HTMX alone.
Use JavaScript for the client interaction and send the resulting order to the existing backend endpoint.

Status:

- complete
- `/albums/{id}` is now the owner-management workspace
- title editing, append upload, ZIP/public-link actions, media actions, reorder, and preview/lightbox behavior are implemented
- the append-files flow uses the shared upload UI in a modal and refreshes the album after upload

## Phase 7: Public Album Experience

Page:

- `/a/{id}`

Why after owner workspace:

- it shares media presentation concepts
- it is simpler once the media tile and preview patterns already exist

Implementation notes:

- make this presentation-first
- keep management controls minimal
- expose ZIP action clearly
- add subtle owner edit link when appropriate

HTMX candidates:

- token-driven management affordances if they are exposed in-page

JavaScript needed here:

- media lightbox
- copy-link helpers if desired

Status:

- complete
- `/a/{id}` and `/u/{username}` now use template-backed pages on the shared shell
- `/a/{id}` has the public ZIP action, owner edit link, full-media preview cards, split-link media actions, and public lightbox behavior
- `/u/{username}` now presents the public album list on the same shared visual system

## Phase 7.5: Anonymous Manage Flow

Pages:

- `/manage/{id}?token=...`

Why after the public page:

- anonymous uploads need a real follow-up path for mutation
- the owner album workspace already established the right editing model

Implementation notes:

- keep `/a/{id}` presentation-first
- keep anonymous management token-backed and explicit
- reuse the owner workspace template and client logic rather than building a second bespoke editor
- expose a real `manage_url` in anonymous upload responses so the user can save it outside the browser

Status:

- complete
- anonymous upload responses now include `manage_url`
- `/manage/{id}?token=...` reuses the album workspace UI with token-backed requests
- the browser stores anonymous album access in local storage so `/a/{id}` can show `Manage Album` when that browser already has the token

## Phase 8: Settings

Page:

- `/settings`

Implementation notes:

- organize into clear sections
- keep account, storage, API/ShareX, security, and danger-zone areas distinct

HTMX is a very good fit here for:

- password change
- API key rotation/reveal
- ShareX config action feedback
- account deletion confirmation flow

JavaScript needed here:

- very little
- mostly confirmation helpers or copy interactions

Status:

- complete
- `/settings` now uses the shared template/CSS/JS system instead of the earlier inline utility implementation
- the page is split into account, API/ShareX, security, and danger-zone sections
- API key and ShareX actions use inline warning/status messaging, and password/delete flows use local inline feedback rather than the page-top flash area
- behavior coverage now includes browser-session API key rotation, password change, and account deletion flows in addition to page-shell checks

## Phase 9: Admin Surface

Pages:

- `/admin`
- `/admin/users`
- `/admin/albums`
- `/admin/config`
- `/admin/ops`

Why later:

- the user-facing experience is more important
- admin pages benefit from the shared layout and interaction patterns established earlier

Implementation notes:

- keep admin styling denser and more operational
- split pages cleanly instead of recreating a giant all-in-one utility page

HTMX is a strong fit here for:

- table actions
- inline updates
- user mutation flows
- config updates
- partial refreshes of status panels

JavaScript needed here:

- likely minimal
- mostly only for confirmations and small UX enhancements

Status:

- complete
- `/admin`, `/admin/users`, `/admin/users/new`, `/admin/albums`, `/admin/config`, and `/admin/ops` now exist as split template-backed pages
- `/admin/users` and `/admin/albums` now use paginated listing surfaces rather than loading the full dataset at once
- `/admin/config` is grouped into readable operational sections with inline save feedback and an optional debug payload view
- `/admin/ops` now presents structured runtime health panels, shared network-trust sections, and a bounded audit list with a default page size instead of dumping an unbounded raw response
- `/admin` overview now shows summary cards for storage/runtime and reuses the shared runtime rendering used by `/admin/ops`
- admin runtime/network-trust rendering now lives in shared client code to avoid surface drift between overview and ops

## HTMX Strategy

Use HTMX intentionally, not everywhere.

Good HTMX cases:

- form submission with inline response fragments
- partial page updates
- server-rendered list refreshes
- mutation flows where the server already owns the true state

Avoid overusing HTMX for:

- rich drag interactions
- upload progress orchestration
- large client-side state machines
- media lightbox logic

If a UI interaction is naturally browser-native, write small JavaScript for it.

## JavaScript Strategy

Keep JavaScript modular and page-scoped.

Recommended modules:

- `upload.js`
  Handles drag/drop, paste, file selection, progress UI, and upload success handoff.
- `album-detail.js`
  Handles reorder mode, append uploads, and page-specific controls.
- `lightbox.js`
  Handles image and video preview overlays.
- `copy.js`
  Handles clipboard actions and transient success feedback.

Rules:

- do not create a giant global script file
- prefer `data-*` hooks over brittle selectors
- keep each script focused on one interaction family

## CSS Strategy

Start with a shared design system layer and keep page-specific CSS limited.

Suggested structure:

- `base.css` for tokens, layout primitives, type, buttons, forms, alerts, nav, and media grid foundations
- `pages.css` for route-specific or view-specific refinements

Rules:

- define shared CSS variables for spacing, color, typography, radius, border, and motion
- avoid framework-looking utility soup
- keep admin density separate from public-page presentation

## Routing And Compatibility

Keep route behavior aligned with the route planning docs.

Requirements:

- `/` remains public-first
- `/login` and `/register` redirect signed-in users to `/dashboard`
- `/dashboard`, `/albums`, and `/settings` require auth
- `/albums/{id}` uses owner/admin checks and public redirect behavior as planned
- `/a/{id}` remains stable and public
- `/admin*` requires admin auth
- `/dashboard` is the signed-in home
- `/albums` is a distinct authenticated page, not a replacement for `/dashboard`

Do not break existing stable public URLs during the migration.

## Testing Plan

The UI rewrite should keep the existing browser-page regression mindset.

Add or update tests for:

- page route auth redirects
- disabled registration state
- disabled anonymous upload state
- signed-in redirect to `/dashboard`
- `/dashboard` page shell and upload surface
- `/albums` page shell without the primary upload box
- owner vs public album route behavior
- admin access denial behavior
- critical form flows on the new template-backed pages

Also test:

- upload success and failure states
- album reorder request behavior
- media delete refresh behavior

Not every interaction needs end-to-end browser automation immediately, but route and response behavior should remain covered.

## Documentation Plan

Update docs as the migration lands.

At minimum:

- update UI surface docs
- document template/static asset locations
- document how HTMX and JavaScript responsibilities are divided
- document any new conventions for page handlers and partial responses

## Suggested Delivery Milestones

## Milestone 1

- template infrastructure
- shared base layout
- static files support
- `/`
- `/login`
- `/register`

## Milestone 2

- `/dashboard`
- shared upload module

## Milestone 3

- `/albums`

## Milestone 4

- `/albums/{id}`
- reorder and media-management interactions

## Milestone 5

- `/a/{id}`
- `/settings`

## Milestone 6

- `/admin`
- `/admin/users`
- `/admin/albums`
- `/admin/config`
- `/admin/ops`

## Bottom Line

The implementation should be a controlled migration from inline FastAPI HTML strings to a template-based Python UI with HTMX-enhanced interactions and small JavaScript islands for the browser-native pieces.

That approach matches the current architecture, avoids introducing a separate frontend platform, and still gives enough room to build a polished real product UI.
