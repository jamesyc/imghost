from __future__ import annotations

import argparse
import asyncio

from .config import load_settings
from .main import AppState


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m imghost")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser("retry-thumbnails")
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

    parser.error(f"Unknown command: {args.command}")
    return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run_cli(argv))


if __name__ == "__main__":
    raise SystemExit(main())
