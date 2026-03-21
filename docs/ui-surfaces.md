# UI Surfaces

The browser UI is now a real template-backed product/admin surface, though it still leans practical over polished.

## `/`

Purpose:

- sign in
- register
- anonymous upload

## `/dashboard`

Purpose:

- authenticated upload
- recent owned album inspection
- quick navigation into albums and settings

This page is no longer the home for account settings.

## `/albums`

Purpose:

- owned album list
- resume work on existing albums
- owner actions like public-link access, ZIP access, and deletion

## `/albums/{album_id}`

Purpose:

- owner album workspace
- metadata editing
- reorder
- append upload
- media deletion
- cover selection

## `/manage/{album_id}`

Purpose:

- token-backed anonymous album workspace
- same general album-management surface, but authorized by token instead of owner auth

## `/settings`

Purpose:

- current-user summary
- API key reveal/rotation
- ShareX config export
- password change
- account deletion

## `/admin`

Purpose:

- user management
- album management
- runtime config
- audit log
- runtime/ops inspection
