"""Outgoing webhook dispatcher for AmoCRM / Make / Zapier integrations.

When a domain event fires (payment.succeeded, subscription.created, etc.),
we look up enabled IntegrationWebhook rows matching the provider/event and POST the payload.
All deliveries are logged to webhook_log for visibility in the admin panel.

Security:
- URL is re-validated on every send (SSRF guard) — defence in depth in case
  the row was tampered with via direct DB access.
- Request is signed with X-Membership-Signature: sha256=hex(hmac(secret, body))
  so the receiver can prove integrity and we never ship the secret over the wire.
- Response body is truncated to 2KB and only headers we know are logged, so a
  malicious endpoint can't bloat the WebhookLog table.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import IntegrationWebhook, WebhookLog
from app.services.url_safety import UnsafeURLError, assert_safe_outbound_url

logger = logging.getLogger(__name__)
settings = get_settings()

_HTTP_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_RESP_BYTES = 2048
_USER_AGENT = "membership_saas-webhook/1.0"


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def dispatch(session: AsyncSession, event: str, payload: dict[str, Any]) -> None:
    # GK-120: AmoCRM/Make/Zapier integrations are launch-hidden by default.
    # When ENABLE_WEBHOOK_INTEGRATIONS=false we record one log row for audit
    # but never POST to a legacy/existing IntegrationWebhook URL.
    if not settings.enable_webhook_integrations:
        session.add(
            WebhookLog(
                webhook_id=None,
                event=event,
                payload=payload,
                response_status=None,
                response_body="disabled: ENABLE_WEBHOOK_INTEGRATIONS=false",
            )
        )
        return

    q = select(IntegrationWebhook).where(IntegrationWebhook.enabled.is_(True))
    res = await session.execute(q)
    hooks = [h for h in res.scalars().all() if not h.events or event in h.events]

    if not hooks:
        session.add(
            WebhookLog(
                webhook_id=None,
                event=event,
                payload=payload,
                response_status=None,
                response_body="no hooks",
            )
        )
        return

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:
        for hook in hooks:
            # Re-validate URL on every send — guard against DB tampering.
            try:
                assert_safe_outbound_url(hook.url, require_https=settings.is_prod)
            except UnsafeURLError as e:
                logger.warning("refusing to call unsafe webhook url %s: %s", hook.url, e)
                session.add(
                    WebhookLog(
                        webhook_id=hook.id,
                        event=event,
                        payload={"event": event, "data": payload},
                        response_status=None,
                        response_body=f"refused: {e}",
                    )
                )
                continue

            body_dict = {
                "event": event,
                "data": payload,
                "provider": hook.provider,
                "delivery_id": uuid.uuid4().hex,
            }
            body_bytes = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")

            headers = {
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            }
            secret = hook.secret or settings.outgoing_webhook_signing_secret
            if secret:
                headers["X-Membership-Signature"] = _sign(secret, body_bytes)

            status: int | None = None
            response_text: str = ""
            try:
                resp = await client.post(hook.url, content=body_bytes, headers=headers)
                status = resp.status_code
                # Read at most 2KB; some endpoints can stream megabytes.
                response_text = resp.text[:_MAX_RESP_BYTES]
            except Exception as e:
                response_text = f"error: {type(e).__name__}"
                logger.warning("webhook %s failed: %s", hook.url, e)
            session.add(
                WebhookLog(
                    webhook_id=hook.id,
                    event=event,
                    payload=body_dict,
                    response_status=status,
                    response_body=response_text,
                )
            )
