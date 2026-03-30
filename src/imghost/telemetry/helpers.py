from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from fastapi import Request

from . import actions
from .context import (
    anonymous_actor,
    build_cli_process_context,
    build_request_context,
    build_runtime_process_context,
    cli_actor,
    hash_client_ip,
    user_actor,
)
from .models import TelemetryActor, TelemetryObject

if TYPE_CHECKING:
    from ..models import Media
    from ..models import User
    from .models import TelemetryProcessContext
    from .service import TelemetryService
    from .state import TelemetryState

logger = logging.getLogger(__name__)


async def emit_request_action(
    telemetry: "TelemetryService",
    request: Request,
    *,
    event_type: str,
    action: str,
    result: str,
    actor: TelemetryActor,
    object: TelemetryObject,
    metadata: Mapping[str, Any] | None = None,
    reason: str | None = None,
    auth_method: str | None = None,
    source: str = "web",
) -> None:
    request_context = build_request_context(request, auth_method=auth_method)
    payload = dict(metadata or {})
    payload.setdefault("correlation_id", request_context.correlation_id)
    payload.setdefault("source", source)
    await telemetry.emit_event(
        event_type=event_type,
        action=action,
        result=result,
        actor=actor,
        object=object,
        metadata=payload,
        request=request_context,
        process=build_runtime_process_context(source),
        reason=reason,
        actor_ip_hash=hash_client_ip(request_context.client_ip),
    )


async def record_api_key_auth_failed(
    telemetry: "TelemetryService",
    request: Request,
    *,
    actor: "User | None",
    object_type: str,
    object_id: str | None,
    reason: str,
    admin_denial: bool = False,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.ADMIN_ACCESS_DENIED if admin_denial else actions.API_KEY_INVALID,
        action="apikey.auth.failed",
        result="denied",
        actor=user_actor(actor) if actor is not None else anonymous_actor(),
        object=TelemetryObject(type=object_type, id=object_id),
        reason=reason,
        auth_method="api_key",
        source="api",
    )


async def record_login_failed(
    telemetry: "TelemetryService",
    request: Request,
    *,
    login_identifier: str,
    reason: str,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.LOGIN_FAILED,
        action="auth.login.failed",
        result="denied",
        actor=anonymous_actor(),
        object=TelemetryObject(type="auth", id=login_identifier),
        metadata={"login_identifier": login_identifier, "reason": reason},
        reason=reason,
        auth_method="password",
        source="web",
    )


async def record_auth_rate_limited(
    telemetry: "TelemetryService",
    request: Request,
    *,
    scope: str,
    method: str,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.AUTH_RATE_LIMITED,
        action="auth.rate_limited",
        result="denied",
        actor=anonymous_actor(),
        object=TelemetryObject(type="auth_rate_limit", id=scope),
        metadata={"scope": scope, "method": method},
        reason="rate_limited",
        auth_method=method,
        source="api" if method == "api_key" else "web",
    )


async def record_login_succeeded(
    telemetry: "TelemetryService",
    request: Request,
    *,
    user: "User",
    remember_me: bool,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.USER_LOGIN,
        action="auth.login.success",
        result="success",
        actor=user_actor(user),
        object=TelemetryObject(type="user", id=user.id),
        metadata={"target_user_id": user.id, "remember_me": remember_me},
        auth_method="password",
        source="web",
    )


async def record_registration_denied(
    telemetry: "TelemetryService",
    request: Request,
    *,
    username: str,
    email: str,
    reason: str = "registration_disabled",
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.REGISTRATION_DENIED,
        action="auth.registration.denied",
        result="denied",
        actor=anonymous_actor(),
        object=TelemetryObject(type="registration", id=username or None),
        metadata={"username": username, "email": email},
        reason=reason,
        auth_method="anonymous",
        source="web",
    )


async def record_logout_succeeded(
    telemetry: "TelemetryService",
    request: Request,
    *,
    user: "User",
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.LOGOUT,
        action="auth.logout",
        result="success",
        actor=user_actor(user),
        object=TelemetryObject(type="user", id=user.id),
        metadata={"target_user_id": user.id},
        auth_method="session",
        source="web",
    )


async def record_api_key_authenticated(
    telemetry: "TelemetryService",
    request: Request,
    *,
    user: "User",
    api_key_id: str,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.API_KEY_AUTHENTICATED,
        action="apikey.auth.success",
        result="success",
        actor=user_actor(user),
        object=TelemetryObject(type="user", id=user.id),
        metadata={"api_key_id": api_key_id},
        auth_method="api_key",
        source="api",
    )


async def record_admin_access_denied(
    telemetry: "TelemetryService",
    request: Request,
    *,
    actor: "User | None",
    object_type: str,
    reason: str,
    source: str,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.ADMIN_ACCESS_DENIED,
        action="auth.admin.denied",
        result="denied",
        actor=user_actor(actor) if actor is not None else anonymous_actor(),
        object=TelemetryObject(type=object_type, id=request.url.path),
        reason=reason,
        source=source,
    )


async def record_csrf_blocked(telemetry: "TelemetryService", request: Request) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.CSRF_BLOCKED,
        action="csrf.blocked",
        result="denied",
        actor=anonymous_actor(),
        object=TelemetryObject(type="request", id=request.url.path),
        reason="untrusted_csrf_source",
        auth_method="session",
        source="web",
    )


async def record_admin_api_read(
    telemetry: "TelemetryService",
    request: Request,
    *,
    admin: "User",
    resource: str,
    object_type: str = "admin_api",
    object_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    payload = {"resource": resource}
    if metadata:
        payload.update(metadata)
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.ADMIN_API_READ,
        action=f"{resource}.read",
        result="success",
        actor=user_actor(admin, actor_type="admin"),
        object=TelemetryObject(type=object_type, id=object_id or request.url.path),
        metadata=payload,
        source="api",
    )


async def record_admin_page_viewed(
    telemetry: "TelemetryService",
    request: Request,
    *,
    user: "User",
    page_name: str,
    object_id: str | None = None,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.ADMIN_PAGE_VIEWED,
        action=f"{page_name}.view",
        result="success",
        actor=user_actor(user, actor_type="admin"),
        object=TelemetryObject(type="admin_page", id=object_id or request.url.path),
        metadata={"page": page_name},
        source="web",
    )


async def record_oauth_disconnected(
    telemetry: "TelemetryService",
    request: Request,
    *,
    user: "User",
    provider: str,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.OAUTH_DISCONNECTED,
        action="oauth.disconnected",
        result="success",
        actor=user_actor(user),
        object=TelemetryObject(type="user", id=user.id),
        metadata={"provider": provider},
        auth_method="session",
        source="api",
    )


async def record_oauth_denied(
    telemetry: "TelemetryService",
    request: Request,
    *,
    reason: str,
    actor: "User | None" = None,
    object_id: str | None = None,
) -> None:
    await emit_request_action(
        telemetry,
        request,
        event_type=actions.OAUTH_DENIED,
        action="oauth.denied",
        result="denied",
        actor=user_actor(actor) if actor is not None else anonymous_actor(),
        object=TelemetryObject(type="oauth", id=object_id or "google"),
        metadata={"provider": "google"},
        reason=reason,
        auth_method="oauth",
        source="web",
    )


async def record_oauth_succeeded(
    telemetry: "TelemetryService",
    request: Request,
    *,
    user: "User",
    provider: str,
    provider_uid: str,
    outcome: str,
) -> None:
    event_type = actions.OAUTH_LINKED if outcome == "linked" else actions.OAUTH_LOGIN
    action = "oauth.linked" if outcome == "linked" else "oauth.login.success"
    await emit_request_action(
        telemetry,
        request,
        event_type=event_type,
        action=action,
        result="success",
        actor=user_actor(user),
        object=TelemetryObject(type="user", id=user.id),
        metadata={"provider": provider, "provider_uid": provider_uid, "outcome": outcome},
        auth_method="oauth",
        source="web",
    )


async def record_system_event(
    telemetry: "TelemetryService",
    *,
    event_type: str,
    action: str,
    source: str,
    object_type: str = "system",
    object_id: str = "imghost",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    await telemetry.emit_event(
        event_type=event_type,
        action=action,
        result="success",
        actor=TelemetryActor(id=None, type=source),
        object=TelemetryObject(type=object_type, id=object_id),
        metadata={**dict(metadata or {}), "source": source},
        process=build_runtime_process_context(source),
    )


async def record_cli_command(
    telemetry: "TelemetryService",
    *,
    action: str,
    object_type: str,
    object_id: str,
    metadata: Mapping[str, Any] | None = None,
    argv: list[str],
) -> None:
    await telemetry.emit_event(
        event_type=actions.CLI_COMMAND_EXECUTED,
        action=action,
        result="success",
        actor=cli_actor(),
        object=TelemetryObject(type=object_type, id=object_id),
        metadata={**dict(metadata or {}), "source": "cli"},
        process=build_cli_process_context(argv),
    )


def record_thumbnail_failure(
    *,
    telemetry_state: "TelemetryState | None",
    media: "Media",
    correlation_id: str,
    reason: str,
    error: Exception,
) -> None:
    details = {
        "reason": reason,
        "media_id": media.id,
        "correlation_id": correlation_id,
        "storage_key": media.storage_key,
        "format": media.format,
    }
    logger.warning(
        "thumbnail_generation_failed",
        extra={**details, "error_type": type(error).__name__},
        exc_info=error,
    )
    if telemetry_state is not None:
        telemetry_state.record_task_failure(task_name="generate_thumbnail", details=details)
