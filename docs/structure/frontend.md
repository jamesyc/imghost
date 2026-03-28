# Frontend Structure

The frontend is a server-rendered app with shared CSS and page-scoped vanilla JavaScript. 

## Templates

- [`src/imghost/templates/base.html`](/home/james/imghost/src/imghost/templates/base.html): shared document shell, asset loading, and common layout framing
- [`src/imghost/templates/partials/nav.html`](/home/james/imghost/src/imghost/templates/partials/nav.html): primary nav
- [`src/imghost/templates/partials/admin-subnav.html`](/home/james/imghost/src/imghost/templates/partials/admin-subnav.html): admin local navigation
- [`src/imghost/templates/pages`](/home/james/imghost/src/imghost/templates/pages): page templates by route surface

## Shared Frontend Assets

- [`src/imghost/static/css/base.css`](/home/james/imghost/src/imghost/static/css/base.css): shared stylesheet entrypoint that imports the split base token/layout/component/form/page/responsive/upload layers
- [`src/imghost/static/js/upload-box.js`](/home/james/imghost/src/imghost/static/js/upload-box.js): upload interactions used by landing, dashboard, and workspace flows
- [`src/imghost/static/js/auth.js`](/home/james/imghost/src/imghost/static/js/auth.js): login and registration behavior
- [`src/imghost/static/js/album-cards.js`](/home/james/imghost/src/imghost/static/js/album-cards.js): shared album card rendering/interaction helpers
- [`src/imghost/static/js/admin-common.js`](/home/james/imghost/src/imghost/static/js/admin-common.js): shared admin page helpers
- [`src/imghost/static/js/pwa.js`](/home/james/imghost/src/imghost/static/js/pwa.js): service-worker registration
- [`src/imghost/static/js/theme-init.js`](/home/james/imghost/src/imghost/static/js/theme-init.js): early theme bootstrap
- [`src/imghost/static/js/theme.js`](/home/james/imghost/src/imghost/static/js/theme.js): runtime theme controls

## Page-Specific JS Entry Points

- Public pages: `home.js`, `public-album.js`
- Signed-in product pages: `dashboard.js`, `albums.js`, `album-detail.js`, `settings.js`
- Admin pages: `admin-index.js`, `admin-users.js`, `admin-users-new.js`, `admin-user-detail.js`, `admin-albums.js`, `admin-config.js`, `admin-ops.js`

Each page route declares its script list in [`src/imghost/web/pages.py`](/home/james/imghost/src/imghost/web/pages.py), which is the best place to verify which assets actually load on a given page.

## Page Context and Bootstrap

- [`src/imghost/web/page_context.py`](/home/james/imghost/src/imghost/web/page_context.py): shared template rendering helpers, route-aware context shaping, and `next` normalization
- [`src/imghost/web/page_views.py`](/home/james/imghost/src/imghost/web/page_views.py): page payload builders for public album pages, public user pages, and shared workspace bootstrap data

The most important split is:

- page routes decide who can access a page and which template/scripts load
- page-context helpers provide shared shell data
- page-view builders shape richer payloads for specific page families

## Current UX Pattern

- Public pages are presentation-first.
- Owner and token-backed album pages are workspace-first and reuse the same template shell.
- Admin pages stay on the shared system but use denser operational layouts.
- The current app intentionally uses vanilla JS plus server-rendered HTML instead of adding more frontend framework machinery.

## Testing Coverage

Frontend structure and page contracts are mainly checked in:

- [`tests/test_pages.py`](/home/james/imghost/tests/test_pages.py)
- [`tests/test_page_views.py`](/home/james/imghost/tests/test_page_views.py)
- [`tests/test_album_api.py`](/home/james/imghost/tests/test_album_api.py)
