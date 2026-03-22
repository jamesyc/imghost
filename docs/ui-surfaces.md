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

- admin overview and quick navigation into the focused admin tools

## `/admin/users`

Purpose:

- search and page through users
- open the focused admin detail page for a specific account

This page no longer owns the full patch/reset/delete workflow inline.

## `/admin/users/{user_id}`

Purpose:

- inspect one account in depth
- review storage/account stats
- page through the user’s owned albums
- patch account state
- reset password
- delete user

## `/admin/albums`

Purpose:

- search and page through albums across the system
- review album ownership and summary information
- perform admin album actions

## `/admin/config`

Purpose:

- inspect and update runtime config values

## `/admin/ops`

Purpose:

- runtime-status inspection
- proxy/public-origin trust inspection
- audit log queries
