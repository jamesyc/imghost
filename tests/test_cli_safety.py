from __future__ import annotations

from imghost.__main__ import _confirm_risky_cli_target


def test_cli_confirmation_bypasses_local_database() -> None:
    assert _confirm_risky_cli_target(
        "postgresql://imghost:imghost@localhost:5432/imghost",
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
