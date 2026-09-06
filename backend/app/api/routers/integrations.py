from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentAdmin
from app.config import get_settings
from app.db.models import IntegrationWebhook, WebhookLog
from app.services.audit import record as audit_record
from app.services.url_safety import UnsafeURLError, assert_safe_outbound_url
from app.services.webhooks import dispatch

router = APIRouter(prefix="/integrations", tags=["integrations"])
settings = get_settings()

_ALLOWED_EVENTS = {
    "payment.succeeded",
    "subscription.activated",
    "subscription.expired",
    "test.ping",
}


def _require_integrations_enabled() -> None:
    """GK-120: AmoCRM/Make/Zapier webhook integrations are launch-hidden.

    Read paths (list/logs) stay open so admins can audit/clean up legacy
    rows. Mutating paths (create/test) refuse until ENABLE_WEBHOOK_INTEGRATIONS=true.
    Existing rows are never deleted by this gate.
    """
    if not settings.enable_webhook_integrations:
        raise HTTPException(
            status_code=403,
            detail=(
                "Webhook integrations are disabled for launch. "
                "Set ENABLE_WEBHOOK_INTEGRATIONS=true to re-enable AmoCRM/Make/Zapier."
            ),
        )


class WebhookIn(BaseModel):
    provider: str = Field(..., max_length=32)  # amocrm | make | zapier | custom
    name: str = Field(..., min_length=1, max_length=128)
    url: str = Field(..., min_length=1, max_length=500)
    secret: str | None = Field(default=None, max_length=255)
    events: list[str] = Field(default_factory=list, max_length=32)
    enabled: bool = True


class WebhookOut(WebhookIn):
    id: int
    created_at: datetime


class LogOut(BaseModel):
    id: int
    webhook_id: int | None
    event: str
    response_status: int | None
    created_at: datetime


@router.get("/webhooks", response_model=list[WebhookOut])
async def list_webhooks(db: DB, _: CurrentAdmin):
    rows = (await db.execute(select(IntegrationWebhook).order_by(IntegrationWebhook.id.desc()))).scalars().all()
    return [
        WebhookOut(
            id=h.id,
            provider=h.provider,
            name=h.name,
            url=h.url,
            secret=h.secret,
            events=h.events or [],
            enabled=h.enabled,
            created_at=h.created_at,
        )
        for h in rows
    ]


def _validate_payload(payload: WebhookIn) -> None:
    if payload.provider not in ("amocrm", "make", "zapier", "custom"):
        raise HTTPException(400, "Unknown provider")
    # SSRF guard. In prod we force HTTPS; in dev we still block private IPs.
    try:
        assert_safe_outbound_url(payload.url, require_https=settings.is_prod)
    except UnsafeURLError as e:
        raise HTTPException(400, f"Invalid URL: {e}") from e
    unknown = [ev for ev in (payload.events or []) if ev not in _ALLOWED_EVENTS]
    if unknown:
        raise HTTPException(
            400, f"Unknown event(s): {','.join(unknown)}. Allowed: {sorted(_ALLOWED_EVENTS)}"
        )


@router.post("/webhooks", response_model=WebhookOut)
async def create_webhook(payload: WebhookIn, db: DB, admin: CurrentAdmin, request: Request):
    _require_integrations_enabled()
    _validate_payload(payload)
    h = IntegrationWebhook(**payload.model_dump())
    db.add(h)
    await db.flush()
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="webhook.create",
        target_type="webhook",
        target_id=h.id,
        # Never log the secret.
        details={"provider": h.provider, "url": h.url, "events": h.events},
        request=request,
    )
    return WebhookOut(id=h.id, created_at=h.created_at, **payload.model_dump())


@router.delete("/webhooks/{wid}")
async def delete_webhook(wid: int, db: DB, admin: CurrentAdmin, request: Request):
    h = (await db.execute(select(IntegrationWebhook).where(IntegrationWebhook.id == wid))).scalar_one_or_none()
    if not h:
        raise HTTPException(404, "Webhook not found")
    await db.delete(h)
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="webhook.delete",
        target_type="webhook",
        target_id=wid,
        details={"provider": h.provider, "url": h.url},
        request=request,
    )
    return {"ok": True}


@router.post("/webhooks/{wid}/test")
async def test_webhook(wid: int, db: DB, _: CurrentAdmin):
    _require_integrations_enabled()
    h = (await db.execute(select(IntegrationWebhook).where(IntegrationWebhook.id == wid))).scalar_one_or_none()
    if not h:
        raise HTTPException(404, "Webhook not found")
    await dispatch(db, "test.ping", {"hello": "world", "webhook_id": wid})
    return {"ok": True}


@router.get("/logs", response_model=list[LogOut])
async def list_logs(db: DB, _: CurrentAdmin, limit: int = Query(50, ge=1, le=500)):
    rows = (await db.execute(select(WebhookLog).order_by(WebhookLog.id.desc()).limit(limit))).scalars().all()
    return [
        LogOut(
            id=row.id,
            webhook_id=row.webhook_id,
            event=row.event,
            response_status=row.response_status,
            created_at=row.created_at,
        )
        for row in rows
    ]
