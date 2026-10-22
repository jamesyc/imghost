import asyncio

import httpx2
import pytest
from fastapi import HTTPException

from imghost.oauth import google


@pytest.mark.parametrize("token_status", [200, 400])
def test_google_oauth_exchange_uses_httpx2_and_preserves_error_handling(monkeypatch, token_status) -> None:
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/token":
            body = {"access_token": "test-token"} if token_status == 200 else {"error_description": "Invalid code"}
            return httpx2.Response(token_status, json=body)
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx2.Response(200, json={"sub": "subject", "email": "alice@example.com", "email_verified": True})

    real_client = httpx2.AsyncClient
    monkeypatch.setattr(google.httpx2, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx2.MockTransport(handle),
    ))
    provider = google.GoogleOAuthProvider(client_id="client", client_secret="secret")
    exchange = provider.exchange_code(code="code", redirect_uri="http://localhost/callback", code_verifier="verifier")
    if token_status == 400:
        with pytest.raises(HTTPException, match="Invalid code"):
            asyncio.run(exchange)
        assert len(requests) == 1
    else:
        identity = asyncio.run(exchange)
        assert identity.provider_uid == "subject"
        assert identity.email_verified
        assert b"code_verifier=verifier" in requests[0].content
        assert len(requests) == 2
