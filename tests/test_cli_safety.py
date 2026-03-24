from __future__ import annotations

from imghost.__main__ import (
    _confirm_risky_cli_target,
    _process_role_for_command,
    _requires_cli_confirmation,
    _runs_worker_for_command,
    _worker_queues_for_command,
    build_parser,
)


def test_cli_confirmation_bypasses_local_database() -> None:
    assert _confirm_risky_cli_target(
        "postgresql://imghost:imghost@localhost:5432/imghost",
        command="create-user",
        stdin_isatty=False,
    )
    assert _confirm_risky_cli_target(
        "postgresql://imghost:imghost@pgbouncer:5432/imghost",
        command="create-user",
        stdin_isatty=False,
    )


def test_cli_confirmation_bypasses_test_database() -> None:
    assert _confirm_risky_cli_target(
        "postgresql://imghost:imghost@db.example.com:5432/imghost_test",
        command="issue-api-key",
        stdin_isatty=False,
    )


def test_cli_confirmation_skips_prune_dry_run() -> None:
    assert _confirm_risky_cli_target(
        "postgresql://imghost:imghost@db.example.com:5432/imghost_prod",
        command="prune",
        dry_run=True,
        stdin_isatty=False,
    )


def test_worker_parser_accepts_split_worker_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["run-worker-thumbnails"]).command == "run-worker-thumbnails"
    assert parser.parse_args(["run-worker-cleanup"]).command == "run-worker-cleanup"
    assert parser.parse_args(["run-worker-default"]).command == "run-worker-default"
    assert parser.parse_args(["run-scheduler"]).command == "run-scheduler"


def test_worker_command_queue_selection_uses_split_commands_and_default_config() -> None:
    configured = ("cleanup", "thumbnails")

    assert _worker_queues_for_command("run-worker", configured_queues=configured) == configured
    assert _worker_queues_for_command("run-worker-thumbnails", configured_queues=configured) == ("thumbnails",)
    assert _worker_queues_for_command("run-worker-cleanup", configured_queues=configured) == ("cleanup",)
    assert _worker_queues_for_command("run-worker-default", configured_queues=configured) == ("default",)
    assert _worker_queues_for_command("retry-thumbnails", configured_queues=configured) == ("thumbnails",)


def test_worker_command_detection_and_confirmation_cover_split_commands() -> None:
    assert _runs_worker_for_command("run-worker") is True
    assert _runs_worker_for_command("run-worker-thumbnails") is True
    assert _runs_worker_for_command("run-worker-cleanup") is True
    assert _runs_worker_for_command("run-worker-default") is True
    assert _runs_worker_for_command("prune") is False

    assert _requires_cli_confirmation("run-worker-thumbnails") is True
    assert _requires_cli_confirmation("run-worker-cleanup") is True
    assert _requires_cli_confirmation("run-worker-default") is True
    assert _requires_cli_confirmation("run-scheduler") is True


def test_process_role_selection_matches_command_type() -> None:
    assert _process_role_for_command("run-worker") == "worker"
    assert _process_role_for_command("run-worker-thumbnails") == "worker"
    assert _process_role_for_command("retry-thumbnails") == "worker"
    assert _process_role_for_command("run-scheduler") == "scheduler"
    assert _process_role_for_command("prune") == "app"


def test_cli_confirmation_refuses_noninteractive_nonlocal_nontest_target(capsys) -> None:
    allowed = _confirm_risky_cli_target(
        "postgresql://imghost:imghost@db.example.com:5432/imghost_prod",
        command="create-user",
        stdin_isatty=False,
    )

    assert allowed is False
    assert "without interactive confirmation" in capsys.readouterr().out


def test_cli_confirmation_accepts_interactive_yes() -> None:
    prompts: list[str] = []

    def approve(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    allowed = _confirm_risky_cli_target(
        "postgresql://imghost:imghost@db.example.com:5432/imghost_prod",
        command="init-storage",
        stdin_isatty=True,
        prompt_fn=approve,
    )

    assert allowed is True
    assert len(prompts) == 1
    assert "Type 'y' to continue" in prompts[0]


def test_cli_confirmation_rejects_interactive_non_yes(capsys) -> None:
    allowed = _confirm_risky_cli_target(
        "postgresql://imghost:imghost@db.example.com:5432/imghost_prod",
        command="run-worker",
        stdin_isatty=True,
        prompt_fn=lambda _: "n",
    )

    assert allowed is False
    assert "Aborted." in capsys.readouterr().out
