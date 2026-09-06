"""Append-only audit log for administrator actions.

Call `record(...)` from any admin-mutating endpoint. The current request's
client IP is captured if available. Failures are logged but never raise —
losing an audit row should not break the user-facing operation.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog

logger = logging.getLogger(__name__)


def _client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client:
        return request.client.host[:64]
    return None


async def record(
    session: AsyncSession,
    *,
    actor_admin_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
) -> None:
    try:
        session.add(
            AuditLog(
                actor_admin_id=actor_admin_id,
                actor_ip=_client_ip(request),
                action=action[:64],
                target_type=(target_type or None) and target_type[:32],
                target_id=str(target_id)[:64] if target_id is not None else None,
                details=details or {},
            )
        )
    except Exception as e:
        logger.warning("audit.record failed for action=%s: %s", action, e)
