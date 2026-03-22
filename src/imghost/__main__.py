from __future__ import annotations

import argparse
import asyncio
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from .audit import actions
from .audit.context import build_cli_process_context, cli_actor
from .audit.models import AuditObject
from .app_state import AppState
from .config import load_settings
from .models import User, utcnow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m imghost")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("retry-thumbnails")
    subparsers.add_parser("init-storage")
    subparsers.add_parser("run-worker")

    create_user = subparsers.add_parser("create-user")
    create_user.add_argument("--username", required=True)
    create_user.add_argument("--email", required=True)
    create_user.add_argument("--admin", action="store_true")
    create_user.add_argument("--quota-bytes", type=int, default=None)

    issue_key = subparsers.add_parser("issue-api-key")
    issue_key.add_argument("--user-id", required=True)
    return parser


def _database_name(dsn: str) -> str:
    parsed = urlsplit(dsn)
    return parsed.path.lstrip("/") or "postgres"


def _database_host(dsn: str) -> str:
    parsed = urlsplit(dsn)
    return (parsed.hostname or "").strip().lower()


def _is_local_database(dsn: str) -> bool:
    host = _database_host(dsn)
    return host in {"", "localhost", "127.0.0.1", "::1", "postgres"}


def _is_test_database(dsn: str) -> bool:
    return "test" in _database_name(dsn).lower()


def _requires_cli_confirmation(command: str, *, dry_run: bool = False) -> bool:
    if command == "prune" and dry_run:
        return False
    return command in {"create-user", "issue-api-key", "prune", "init-storage", "run-worker", "retry-thumbnails"}


def _confirm_risky_cli_target(
    dsn: str,
    *,
    command: str,
    dry_run: bool = False,
    stdin_isatty: bool | None = None,
    prompt_fn=input,
) -> bool:
    if not _requires_cli_confirmation(command, dry_run=dry_run):
        return True
    if _is_local_database(dsn) or _is_test_database(dsn):
        return True
    if stdin_isatty is None:
        stdin_isatty = sys.stdin.isatty()
    database_name = _database_name(dsn)
    database_host = _database_host(dsn) or "<unknown>"
    if not stdin_isatty:
        print(
            f"Refusing to run '{command}' against non-local, non-test database '{database_name}' on '{database_host}' without interactive confirmation."
        )
        return False
    response = prompt_fn(
        f"About to run '{command}' against non-local, non-test database '{database_name}' on '{database_host}'. Type 'y' to continue: "
    ).strip().lower()
    if response not in {"y", "yes"}:
        print("Aborted.")
        return False
    return True


async def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command_argv = argv or []
    command_correlation_id = f"cli-{uuid4()}"
    process_context = build_cli_process_context(command_argv)
    settings = load_settings()
    if not _confirm_risky_cli_target(
        settings.database_url,
        command=args.command,
        dry_run=bool(getattr(args, "dry_run", False)),
    ):
        return 1
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    state = AppState(settings, run_task_worker=args.command in {"retry-thumbnails", "run-worker"})
    await state.database.connect()

    try:
        if args.command == "prune":
            result = await state.uploads.prune_expired_albums(dry_run=args.dry_run)
            await state.audit.emit_action(
                event_type=actions.CLI_COMMAND_EXECUTED,
                action="cli.prune",
                result="success",
                actor=cli_actor(),
                object=AuditObject(type="cli_command", id="prune"),
                metadata={
                    "command": "prune",
                    "dry_run": args.dry_run,
                    "album_ids": result.album_ids,
                    "item_count": result.item_count,
                    "bytes_freed": result.bytes_freed,
                    "correlation_id": command_correlation_id,
                    "source": "cli",
                },
                process=process_context,
            )
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
            await state.audit.emit_action(
                event_type=actions.CLI_COMMAND_EXECUTED,
                action="cli.retry_thumbnails",
                result="success",
                actor=cli_actor(),
                object=AuditObject(type="cli_command", id="retry-thumbnails"),
                metadata={"command": "retry-thumbnails", "enqueued": enqueued, "correlation_id": command_correlation_id, "source": "cli"},
                process=process_context,
            )
            print(f"re-enqueued thumbnails: {enqueued}")
            return 0

        if args.command == "init-storage":
            await state.storage.init_storage()
            await state.audit.emit_action(
                event_type=actions.CLI_COMMAND_EXECUTED,
                action="cli.init_storage",
                result="success",
                actor=cli_actor(),
                object=AuditObject(type="cli_command", id="init-storage"),
                metadata={"command": "init-storage", "correlation_id": command_correlation_id, "source": "cli"},
                process=process_context,
            )
            print("storage initialized")
            return 0

        if args.command == "run-worker":
            await state.audit.emit_action(
                event_type=actions.CLI_COMMAND_EXECUTED,
                action="cli.run_worker.start",
                result="success",
                actor=cli_actor(),
                object=AuditObject(type="cli_command", id="run-worker"),
                metadata={"command": "run-worker", "correlation_id": command_correlation_id, "source": "cli"},
                process=process_context,
            )
            await state.redis.ensure_startup_ready()
            await state.tasks.start()
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                await state.tasks.stop()

        if args.command == "create-user":
            user = User(
                id=str(uuid4()),
                username=args.username,
                email=args.email,
                password_hash=None,
                is_admin=args.admin,
                suspended=False,
                quota_bytes=args.quota_bytes,
                rate_limit_rpm=None,
                rate_limit_bph=None,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            await state.repository.create_user(user)
            await state.audit.emit_action(
                event_type=actions.CLI_COMMAND_EXECUTED,
                action="cli.create_user",
                result="success",
                actor=cli_actor(),
                object=AuditObject(type="user", id=user.id),
                metadata={
                    "command": "create-user",
                    "username": user.username,
                    "email": user.email,
                    "is_admin": user.is_admin,
                    "quota_bytes": user.quota_bytes,
                    "correlation_id": command_correlation_id,
                    "source": "cli",
                },
                process=process_context,
            )
            print(f"created user: {user.id}")
            return 0

        if args.command == "issue-api-key":
            user = await state.repository.get_user(args.user_id)
            if user is None:
                print("user not found")
                return 1
            issued = await state.uploads.issue_api_key(user)
            await state.audit.emit_action(
                event_type=actions.CLI_COMMAND_EXECUTED,
                action="cli.issue_api_key",
                result="success",
                actor=cli_actor(),
                object=AuditObject(type="user", id=user.id),
                metadata={
                    "command": "issue-api-key",
                    "user_id": user.id,
                    "api_key_id": issued.api_key.id,
                    "correlation_id": command_correlation_id,
                    "source": "cli",
                },
                process=process_context,
            )
            print(f"user_id: {user.id}")
            print(f"api_key: {issued.raw_key}")
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2
    finally:
        await state.database.close()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_cli(argv))


if __name__ == "__main__":
    raise SystemExit(main())
