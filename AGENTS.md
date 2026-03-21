# Repository Guidelines

## Project Structure & Module Organization
Application code lives in [`src/imghost`](/home/james/imghost/src/imghost). `main.py` wires routes and page rendering, while modules such as `service.py`, `repositories.py`, `storage.py`, and `tasks.py` hold business logic, persistence, storage, and background work. Templates are under [`src/imghost/templates`](/home/james/imghost/src/imghost/templates), with shared partials in `partials/` and page views in `pages/`. Frontend assets live in [`src/imghost/static`](/home/james/imghost/src/imghost/static) with one shared stylesheet, `css/base.css`, and page-specific scripts in `js/`. Tests are in [`tests`](/home/james/imghost/tests). Planning and UX docs are in [`plan`](/home/james/imghost/plan).

## Build, Test, and Development Commands
- `uv run pytest`: run the full test suite.
- `uv run pytest tests/test_pages.py tests/test_album_api.py`: run focused page/API checks during UI work.
- `uv run python -m imghost run-worker`: start the background worker.
- `uv run python -m imghost init-storage`: initialize object storage.
- `docker compose -f docker/docker-compose.yml --env-file docker/.env up --build -d app`: rebuild and redeploy the app container only.
- `docker compose -f docker/docker-compose.yml --env-file docker/.env up --build -d`: rebuild the full stack when infra changes.

## Coding Style & Naming Conventions
Use Python 3.12 with 4-space indentation and type hints where practical. Follow the existing split between route orchestration in `main.py` and reusable logic in service/repository modules. Prefer snake_case for Python functions, variables, and modules; use kebab-free, descriptive template and asset names such as `public-album.html` and `album-detail.js`. Reuse shared UI patterns in `base.css` and shared JS helpers before adding page-specific variants.

## Testing Guidelines
Pytest is the test runner. Add or update tests for every behavioral change, especially route behavior, payload shape, and template shell regressions. Name tests as `test_<feature>.py` and individual cases with explicit scenario names, for example `test_public_album_page_uses_template_shell`. Keep focused checks near the affected area rather than adding broad smoke coverage only.

## Commit & Pull Request Guidelines
Recent commits use short imperative subjects such as `Unify dashboard and albums card rendering` and `Refine signed-in album and dashboard flows`. Keep commit titles concise, capitalized, and action-oriented. PRs should include: what changed, why it changed, test coverage run, and screenshots for UI updates. Call out route, API, or deployment-impacting changes explicitly.

## Security & Configuration Tips
Do not commit `.env` secrets or generated storage data. Use the Docker env file in [`docker/.env`](/home/james/imghost/docker/.env) for local configuration. When changing uploads, public links, or auth flows, verify both anonymous and authenticated behavior.
