from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

from .config import load_settings
from .main import AppState
from .models import User, utcnow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m imghost")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("retry-thumbnails")

    create_user = subparsers.add_parser("create-user")
    create_user.add_argument("--username", required=True)
    create_user.add_argument("--email", required=True)
    create_user.add_argument("--admin", action="store_true")
    create_user.add_argument("--quota-bytes", type=int, default=None)

    issue_key = subparsers.add_parser("issue-api-key")
    issue_key.add_argument("--user-id", required=True)
    return parser


async def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    state = AppState(settings)

    if args.command == "prune":
        result = await state.uploads.prune_expired_albums(dry_run=args.dry_run)
        mode = "dry-run" if args.dry_run else "deleted"
        print(
            f"prune {mode}: albums={len(result.album_ids)} items={result.item_count} bytes={result.bytes_freed}"
        )
        if result.album_ids:
            print("\n".join(result.album_ids))
        return 0

    if args.command == "retry-thumbnails":
        await state.tasks.start()
        try:
            enqueued = await state.recover_thumbnails(include_failed=True)
            await state.tasks.join()
        finally:
            await state.tasks.stop()
        print(f"re-enqueued thumbnails: {enqueued}")
        return 0

    if args.command == "create-user":
        user = User(
            id=str(uuid4()),
            username=args.username,
            email=args.email,
            password_hash=None,
            is_admin=args.admin,
            suspended=False,
            quota_bytes=args.quota_bytes,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        await state.repository.create_user(user)
        print(f"created user: {user.id}")
        return 0

    if args.command == "issue-api-key":
        user = await state.repository.get_user(args.user_id)
        if user is None:
            print("user not found")
            return 1
        issued = await state.uploads.issue_api_key(user)
        print(f"user_id: {user.id}")
        print(f"api_key: {issued.raw_key}")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_cli(argv))


if __name__ == "__main__":
    raise SystemExit(main())
