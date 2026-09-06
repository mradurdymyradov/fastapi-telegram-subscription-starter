from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DB, CurrentAdmin
from app.db.models import AdminUser, ReconciliationItem, ReconciliationRun, utcnow
from app.services.audit import record as audit_record
from app.services.reconciliation import run_reconciliation

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


class RunOut(BaseModel):
    id: int
    status: str
    triggered_by: str
    provider_scope: str
    started_at: datetime
    finished_at: datetime | None
    items_count: int
    open_items_count: int
    summary: dict[str, Any]
    error: str | None


class RunsPage(BaseModel):
    items: list[RunOut]
    total: int


class ItemOut(BaseModel):
    id: int
    run_id: int
    provider: str
    severity: str
    issue_type: str
    entity_type: str
    entity_id: str | None
    external_id: str | None
    status: str
    title: str
    description: str
    expected_state: dict[str, Any]
    observed_state: dict[str, Any]
    resolve_note: str | None
    resolved_by_admin_id: int | None
    resolved_by_email: str | None
    resolved_at: datetime | None
    created_at: datetime
    first_seen_at: datetime | None


class ItemsPage(BaseModel):
    items: list[ItemOut]
    total: int


class RunRequest(BaseModel):
    providers: list[str] | None = None


class ResolveRequest(BaseModel):
    action: str = Field("resolve", pattern="^(resolve|reopen)$")
    note: str | None = Field(default=None, max_length=1000)


@router.get("/runs", response_model=RunsPage)
async def list_runs(
    db: DB,
    _: CurrentAdmin,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> RunsPage:
    total = int((await db.execute(select(func.count(ReconciliationRun.id)))).scalar_one() or 0)
    rows = (
        await db.execute(
            select(ReconciliationRun)
            .order_by(ReconciliationRun.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return RunsPage(items=[_run_out(row) for row in rows], total=total)


@router.post("/runs", response_model=RunOut)
async def trigger_run(
    payload: RunRequest,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
) -> RunOut:
    run = await run_reconciliation(
        db,
        providers=payload.providers,
        triggered_by="admin",
    )
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="reconciliation.run",
        target_type="reconciliation_run",
        target_id=run.id,
        details={
            "provider_scope": run.provider_scope,
            "items_count": run.items_count,
            "open_items_count": run.open_items_count,
        },
        request=request,
    )
    return _run_out(run)


@router.get("/runs/{run_id}/items", response_model=ItemsPage)
async def list_items(
    run_id: int,
    db: DB,
    _: CurrentAdmin,
    status: str | None = Query(None, pattern="^(open|resolved)$"),
    provider: str | None = Query(None, pattern="^(stripe|lava|usdt)$"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> ItemsPage:
    exists = (
        await db.execute(select(ReconciliationRun.id).where(ReconciliationRun.id == run_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404, "Reconciliation run not found")

    base = select(ReconciliationItem).where(ReconciliationItem.run_id == run_id)
    if status:
        base = base.where(ReconciliationItem.status == status)
    if provider:
        base = base.where(ReconciliationItem.provider == provider)
    total = int((await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one() or 0)
    rows = (
        await db.execute(
            base.outerjoin(
                AdminUser,
                AdminUser.id == ReconciliationItem.resolved_by_admin_id,
            )
            .add_columns(AdminUser.email)
            .order_by(
                ReconciliationItem.status.asc(),
                ReconciliationItem.severity.desc(),
                ReconciliationItem.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ItemsPage(items=[_item_out(item, email) for item, email in rows], total=total)


@router.post("/items/{item_id}/resolve", response_model=ItemOut)
async def resolve_item(
    item_id: int,
    payload: ResolveRequest,
    db: DB,
    admin: CurrentAdmin,
    request: Request,
) -> ItemOut:
    item = (
        await db.execute(
            select(ReconciliationItem)
            .where(ReconciliationItem.id == item_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Reconciliation item not found")

    if payload.action == "resolve":
        note = (payload.note or "").strip()
        if not note:
            raise HTTPException(400, "Resolve note is required")
        item.status = "resolved"
        item.resolve_note = note
        item.resolved_by_admin_id = admin.id
        item.resolved_at = utcnow()
    else:
        item.status = "open"
        item.resolve_note = None
        item.resolved_by_admin_id = None
        item.resolved_at = None

    await _refresh_run_open_count(db, item.run_id)
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action=f"reconciliation.item_{payload.action}",
        target_type="reconciliation_item",
        target_id=item.id,
        details={"run_id": item.run_id, "note": payload.note or None},
        request=request,
    )
    email = (
        await db.execute(select(AdminUser.email).where(AdminUser.id == item.resolved_by_admin_id))
    ).scalar_one_or_none()
    return _item_out(item, email)


async def _refresh_run_open_count(db: DB, run_id: int) -> None:
    run = (
        await db.execute(
            select(ReconciliationRun).where(ReconciliationRun.id == run_id).with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        return
    run.open_items_count = int(
        (
            await db.execute(
                select(func.count(ReconciliationItem.id)).where(
                    ReconciliationItem.run_id == run_id,
                    ReconciliationItem.status == "open",
                )
            )
        ).scalar_one()
        or 0
    )


def _run_out(run: ReconciliationRun) -> RunOut:
    return RunOut(
        id=run.id,
        status=run.status,
        triggered_by=run.triggered_by,
        provider_scope=run.provider_scope,
        started_at=run.started_at,
        finished_at=run.finished_at,
        items_count=run.items_count,
        open_items_count=run.open_items_count,
        summary=run.summary or {},
        error=run.error,
    )


def _item_out(item: ReconciliationItem, resolved_by_email: str | None = None) -> ItemOut:
    return ItemOut(
        id=item.id,
        run_id=item.run_id,
        provider=item.provider,
        severity=item.severity,
        issue_type=item.issue_type,
        entity_type=item.entity_type,
        entity_id=item.entity_id,
        external_id=item.external_id,
        status=item.status,
        title=item.title,
        description=item.description,
        expected_state=item.expected_state or {},
        observed_state=item.observed_state or {},
        resolve_note=item.resolve_note,
        resolved_by_admin_id=item.resolved_by_admin_id,
        resolved_by_email=resolved_by_email,
        resolved_at=item.resolved_at,
        created_at=item.created_at,
        first_seen_at=getattr(item, "first_seen_at", None),
    )
