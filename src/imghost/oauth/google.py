from __future__ import annotations

from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from .base import OAuthIdentity

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class GoogleOAuthProvider:
    name = "google"

    def __init__(self, *, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        return f"{GOOGLE_AUTHORIZATION_URL}?{urlencode({
            'client_id': self.client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'openid email profile',
            'state': state,
            'access_type': 'online',
            'prompt': 'select_account',
        })}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthIdentity:
        async with httpx.AsyncClient(timeout=10.0) as client:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
            token_payload = token_response.json() if token_response.content else {}
            if token_response.status_code >= 400:
                detail = token_payload.get("error_description") or token_payload.get("error") or "OAuth token exchange failed."
                raise HTTPException(status_code=400, detail=detail)
            access_token = token_payload.get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="OAuth token exchange failed.")

            userinfo_response = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            userinfo_payload = userinfo_response.json() if userinfo_response.content else {}
            if userinfo_response.status_code >= 400:
                detail = userinfo_payload.get("error_description") or userinfo_payload.get("error") or "OAuth user lookup failed."
                raise HTTPException(status_code=400, detail=detail)

        subject = str(userinfo_payload.get("sub") or "").strip()
        email = str(userinfo_payload.get("email") or "").strip().lower()
        if not subject or not email:
            raise HTTPException(status_code=400, detail="OAuth provider did not return a usable identity.")
        return OAuthIdentity(
            provider=self.name,
            provider_uid=subject,
            email=email,
            email_verified=bool(userinfo_payload.get("email_verified")),
            display_name=(userinfo_payload.get("name") or "").strip() or None,
            avatar_url=(userinfo_payload.get("picture") or "").strip() or None,
        )
