from __future__ import annotations

from time import monotonic, sleep

import bcrypt
from fastapi.testclient import TestClient

from imghost.__main__ import main as cli_main
from imghost.models import utcnow

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c\xf8\xcf"
    b"\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def wait_for_thumbnail(client: TestClient, media_id: str, *, suffix: str = "jpg", timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        response = client.get(f"/t/{media_id}.{suffix}")
        if response.status_code == 200:
            return
        assert response.status_code == 202
        sleep(0.02)
    raise AssertionError(f"thumbnail for {media_id} was not ready within {timeout} seconds")


def browser_session_headers(base_url: str = "https://testserver", path: str = "/") -> dict[str, str]:
    origin = base_url.rstrip("/")
    normalized_path = path if path.startswith("/") else f"/{path}"
    return {
        "Origin": origin,
        "Referer": f"{origin}{normalized_path}",
    }


def _extract_cli_value(lines: list[str], prefix: str) -> str:
    for line in reversed(lines):
        if line.startswith(prefix):
            return line.split(": ", 1)[1]
    raise AssertionError(f"expected CLI output line starting with {prefix!r}, got: {lines!r}")


TEST_CLI_PASSWORD = "test-pass-123"


def create_user_and_api_key(capsys, *, username: str, email: str) -> tuple[str, str]:
    assert cli_main(["create-user", "--username", username, "--email", email, "--password", TEST_CLI_PASSWORD]) == 0
    create_output = capsys.readouterr().out.strip().splitlines()
    user_id = _extract_cli_value(create_output, "created user:")
    assert cli_main(["issue-api-key", "--user-id", user_id]) == 0
    issue_lines = capsys.readouterr().out.strip().splitlines()
    api_key = _extract_cli_value(issue_lines, "api_key:")
    return user_id, api_key


def create_admin_and_api_key(capsys, *, username: str, email: str) -> tuple[str, str]:
    assert cli_main(
        ["create-user", "--username", username, "--email", email, "--password", TEST_CLI_PASSWORD, "--admin"]
    ) == 0
    create_output = capsys.readouterr().out.strip().splitlines()
    user_id = _extract_cli_value(create_output, "created user:")
    assert cli_main(["issue-api-key", "--user-id", user_id]) == 0
    issue_lines = capsys.readouterr().out.strip().splitlines()
    api_key = _extract_cli_value(issue_lines, "api_key:")
    return user_id, api_key


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def set_user_password(client: TestClient, user_id: str, password: str) -> None:
    state = client.app.state.imghost
    user = client.portal.call(state.repository.get_user, user_id)
    assert user is not None
    user.password_hash = _hash_password(password)
    user.updated_at = utcnow()
    client.portal.call(state.repository.update_user, user)


def update_album_record(client: TestClient, album_id: str, **updates) -> None:
    state = client.app.state.imghost
    album = client.portal.call(state.repository.get_album, album_id)
    assert album is not None
    for key, value in updates.items():
        setattr(album, key, value)
    album.updated_at = utcnow()
    client.portal.call(state.repository.update_album, album)


def update_media_record(client: TestClient, media_id: str, **updates) -> None:
    state = client.app.state.imghost
    media = client.portal.call(state.repository.get_media, media_id)
    assert media is not None
    for key, value in updates.items():
        setattr(media, key, value)
    client.portal.call(state.repository.update_media, media)


def get_album_record(client: TestClient, album_id: str):
    return client.portal.call(client.app.state.imghost.repository.get_album, album_id)


def get_media_record(client: TestClient, media_id: str):
    return client.portal.call(client.app.state.imghost.repository.get_media, media_id)


def get_user_record(client: TestClient, user_id: str):
    return client.portal.call(client.app.state.imghost.repository.get_user, user_id)
