from __future__ import annotations

import secrets

ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
ALBUM_ID_LENGTH = 9
MEDIA_ID_LENGTH = 12


def generate_id(length: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def generate_album_id() -> str:
    return generate_id(ALBUM_ID_LENGTH)


def generate_media_id() -> str:
    return generate_id(MEDIA_ID_LENGTH)


def is_valid_id(value: str, length: int) -> bool:
    normalized = value.lower()
    return len(normalized) == length and all(ch in ALPHABET for ch in normalized)

