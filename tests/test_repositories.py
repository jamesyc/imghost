import asyncio

from imghost.repositories import PostgresRepository


class RecordingUserRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def get_user(self, user_id: str):
        self.calls.append(("get_user", user_id))
        return {"id": user_id}

    async def summarize_users(self, user_ids: list[str]):
        self.calls.append(("summarize_users", tuple(user_ids)))
        return {user_id: {"album_count": 0, "media_count": 0, "storage_used_bytes": 0} for user_id in user_ids}


class RecordingAlbumRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def get_album(self, album_id: str):
        self.calls.append(("get_album", album_id))
        return {"id": album_id}

    async def update_media_positions(self, album_id: str, positions: dict[str, int]):
        self.calls.append(("update_media_positions", album_id, dict(positions)))
        return [{"id": media_id, "position": position} for media_id, position in positions.items()]


def test_postgres_repository_facade_delegates_to_split_repositories() -> None:
    repository = object.__new__(PostgresRepository)
    repository.database = None
    repository.users = RecordingUserRepo()
    repository.albums = RecordingAlbumRepo()

    user = asyncio.run(repository.get_user("user-1"))
    summary = asyncio.run(repository.summarize_users(["user-1", "user-2"]))
    album = asyncio.run(repository.get_album("album-1"))
    reordered = asyncio.run(repository.update_media_positions("album-1", {"media-1": 1000}))

    assert user == {"id": "user-1"}
    assert summary["user-2"]["storage_used_bytes"] == 0
    assert album == {"id": "album-1"}
    assert reordered == [{"id": "media-1", "position": 1000}]
    assert repository.users.calls == [
        ("get_user", "user-1"),
        ("summarize_users", ("user-1", "user-2")),
    ]
    assert repository.albums.calls == [
        ("get_album", "album-1"),
        ("update_media_positions", "album-1", {"media-1": 1000}),
    ]
