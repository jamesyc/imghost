from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class OAuthIdentity:
    provider: str
    provider_uid: str
    email: str
    email_verified: bool
    display_name: str | None = None
    avatar_url: str | None = None


class OAuthProvider(Protocol):
    name: str

    def authorization_url(self, *, redirect_uri: str, state: str) -> str: ...

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthIdentity: ...
