from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentAdmin
from app.db.models import Plan
from app.services.audit import record as audit_record

router = APIRouter(prefix="/plans", tags=["plans"])


class PlanOut(BaseModel):
    id: int
    code: str
    name: str
    description: str | None
    price_rub: float
    price_usd: float
    duration_days: int
    is_active: bool
    sort_order: int


class PlanIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    price_rub: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    price_usd: Annotated[int, Field(ge=0, le=1_000_000)] = 0
    duration_days: Annotated[int, Field(ge=1, le=3650)] = 30
    is_active: bool = True
    sort_order: Annotated[int, Field(ge=0, le=10_000)] = 0


@router.get("", response_model=list[PlanOut])
async def list_plans(db: DB, _: CurrentAdmin):
    rows = (await db.execute(select(Plan).order_by(Plan.sort_order))).scalars().all()
    return [PlanOut(**{c.name: getattr(p, c.name) for c in Plan.__table__.columns}) for p in rows]


@router.post("", response_model=PlanOut)
async def create_plan(payload: PlanIn, db: DB, admin: CurrentAdmin, request: Request):
    p = Plan(**payload.model_dump())
    db.add(p)
    await db.flush()
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="plan.create",
        target_type="plan",
        target_id=p.id,
        details=payload.model_dump(),
        request=request,
    )
    return PlanOut(id=p.id, **payload.model_dump())


@router.put("/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: int, payload: PlanIn, db: DB, admin: CurrentAdmin, request: Request
):
    p = (await db.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if not p:
        raise HTTPException(404, "Plan not found")
    before = {c.name: getattr(p, c.name) for c in Plan.__table__.columns}
    new = payload.model_dump()
    for k, v in new.items():
        setattr(p, k, v)
    diff = {k: {"from": str(before.get(k)), "to": str(new[k])} for k in new if str(before.get(k)) != str(new[k])}
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="plan.update",
        target_type="plan",
        target_id=p.id,
        details={"diff": diff},
        request=request,
    )
    return PlanOut(id=p.id, **payload.model_dump())
