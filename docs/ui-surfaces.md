# UI Surfaces

The browser UI is intentionally utility-oriented and mainly exists to exercise backend flows.

## `/`

Purpose:

- sign in
- register
- anonymous upload

## `/dashboard`

Purpose:

- authenticated upload
- owned album inspection
- owned album mutation
- API-key-driven testing mode for dashboard actions

This page is no longer the home for account settings.

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

## `/album-tools`

Purpose:

- token-based operations for anonymous/public albums without writing manual HTTP requests

