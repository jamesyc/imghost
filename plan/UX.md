# UX Technical Plan

This file captures the technical frontend direction for the next UI phase.

The goal is to choose a stack that is durable, easy to operate, and well-matched to a Python backend without introducing a separate Node-based frontend toolchain.

## Recommendation

Use a server-rendered frontend built with:

- FastAPI-rendered HTML templates
- HTMX
- small amounts of vanilla JavaScript where needed
- plain CSS

This is the conservative default for this project.

It keeps the UI close to the Python app, avoids a second application runtime, and is a better fit for the current architecture than a modern SPA stack.

## Why This Stack

The backend already exists as a real FastAPI application and already serves browser pages.

That means the next UI phase does not need:

- a separate frontend deployment
- a Node build pipeline
- a client-side router
- a large browser-state framework

The product does need richer interactions than the current utility UI:

- upload progress
- drag and drop
- paste handling
- album management
- reorder interactions
- clearer public and authenticated page flows

HTMX is a good fit for this level of complexity if we stay disciplined about where JavaScript is still the better tool.

## What We Are Not Choosing

We are deliberately not choosing:

- jQuery
- React + Vite + Node toolchain
- Next.js
- a heavy SPA architecture
- a large component framework as the visual identity

Those can be valid choices in other projects, but they are not the best default fit for a Python-first app with this deployment model.

## Practical Shape

Recommended baseline:

- FastAPI serves page routes and HTML responses
- templates handle page shells, layout, and partial rendering
- HTMX handles partial page updates, form submission, and lighter interactive flows
- vanilla JavaScript handles the interactions HTMX is not ideal for
- CSS remains first-party and project-specific

This keeps the frontend modern enough to improve the UX while staying operationally simple.

## Use HTMX For

HTMX is a strong fit for:

- login and registration flows
- settings forms
- account actions
- admin CRUD screens
- album metadata edits
- partial page refreshes
- empty, loading, and error state replacement
- pagination or filtering if needed later

These are all server-driven interactions where HTML fragments are a natural response format.

## Use JavaScript For

HTMX should not be forced into every interaction.

Use small purpose-built JavaScript for:

- drag and drop uploads
- paste-to-upload
- upload progress bars
- client-side media previews before upload
- album reorder interactions
- lightbox behavior
- clipboard copy helpers

These are browser-native interaction problems, and using direct JavaScript for them is simpler than trying to express everything through HTMX attributes.

## Template Direction

Move away from inline HTML strings in `main.py`.

Preferred direction:

- create a real templates directory
- create a shared base layout
- split page sections into reusable partials
- render fragments separately for HTMX requests where appropriate

This will make the UI easier to change, easier to review, and easier to test than keeping the next product UI embedded in large Python string literals.

## CSS Direction

Use first-party CSS for the product UI.

Recommended structure:

- one shared base stylesheet
- page-specific styles only where necessary
- clear design tokens for spacing, color, type, and layout

Avoid pulling in a large CSS framework unless a concrete problem appears that it genuinely solves.

## Backend Integration

Preferred model:

- FastAPI owns both page routes and API routes
- public media routes remain unchanged
- the new UI replaces the current utility pages incrementally

This keeps responsibilities simple:

- Python owns business logic, auth, upload orchestration, HTML rendering, and media serving
- HTMX and small JavaScript layers improve interactivity in the browser

## Delivery Model

The most practical implementation path is:

1. move page rendering into templates
2. add a shared layout and navigation system
3. rebuild the main product pages as template-backed routes
4. introduce HTMX for partial updates and non-full-page actions
5. add focused JavaScript only for interactions that truly need it
6. retire the current inline utility pages gradually

This reduces migration risk and keeps the backend stable while the UI improves.

The current visual direction also includes a client-side light/dark theme selector stored in `localStorage`, not on the server.

## Current Status

As of the current implementation:

- template and static asset support are in place
- the shared base layout and nav exist
- the home page is template-backed
- `/login` and `/register` exist as dedicated pages
- shared page context and login redirect helpers exist
- `/dashboard`, `/albums`, `/settings`, and `/admin` are the authenticated product/admin surfaces in the planned route model
- `/dashboard` is restored as the signed-in home with the primary upload box and quick-resume content
- `/albums` is narrowed to owned album browsing and management entry points
- `/albums/{id}` is implemented as the owner album workspace with inline editing, append upload, reorder, and lightbox behavior
- `/a/{id}` and `/u/{username}` now use the same shared shell and visual system as the signed-in pages
- `/settings` now uses the shared shell as a real account page with sectioned account/API/security/danger-zone UI and inline action-local feedback
- browser-page tests have been split into a dedicated page-focused test module

The next major frontend step is redesigning the admin surface onto the same system and cleaning up the remaining old flash-driven feedback patterns on other pages.

## Bottom Line

If we remove the Node-based frontend stack and keep the architecture aligned with a Python backend, the frontend direction is:

- FastAPI templates
- HTMX
- vanilla JavaScript where appropriate
- plain CSS

That is the recommended technical direction for the real UI phase.
