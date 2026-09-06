from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.api.deps import DB, CurrentAdmin
from app.db.models import PromoCode, PromoRedemption, User
from app.services.audit import record as audit_record

router = APIRouter(prefix="/promocodes", tags=["promocodes"])


class PromoCodeOut(BaseModel):
    id: int
    code: str
    description: str | None
    discount_type: str
    percent_off: float | None
    amount_off: float | None
    amount_off_currency: str
    applies_to_plan_codes: list[str]
    max_redemptions: int | None
    redeemed_count: int
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    referrer_user_id: int | None
    referrer_username: str | None = None
    created_at: datetime
    updated_at: datetime


class PromoCodeIn(BaseModel):
    code: str = Field(..., min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = Field(default=None, max_length=2000)
    discount_type: str = Field(default="percent", pattern=r"^(percent|fixed)$")
    percent_off: float | None = Field(default=None, ge=0, le=100)
    amount_off: float | None = Field(default=None, ge=0, le=1_000_000)
    amount_off_currency: str = Field(default="USD", pattern=r"^(USD|RUB)$")
    applies_to_plan_codes: list[str] = Field(default_factory=list, max_length=50)
    max_redemptions: int | None = Field(default=None, ge=1, le=10_000_000)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    is_active: bool = True
    referrer_user_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate(self) -> "PromoCodeIn":
        self.code = self.code.strip().upper()
        if self.discount_type == "percent":
            if not self.percent_off or self.percent_off <= 0:
                raise ValueError("percent_off must be > 0 for a percent code")
            self.amount_off = None
        else:  # fixed
            if not self.amount_off or self.amount_off <= 0:
                raise ValueError("amount_off must be > 0 for a fixed code")
            self.percent_off = None
        # Treat naive datetimes as UTC so comparisons against an aware
        # ``utcnow()`` in the promo service never raise.
        if self.valid_from is not None and self.valid_from.tzinfo is None:
            self.valid_from = self.valid_from.replace(tzinfo=UTC)
        if self.valid_until is not None and self.valid_until.tzinfo is None:
            self.valid_until = self.valid_until.replace(tzinfo=UTC)
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        self.applies_to_plan_codes = sorted(
            {c.strip().lower() for c in self.applies_to_plan_codes if c and c.strip()}
        )
        return self


class PromoRedemptionOut(BaseModel):
    id: int
    user_id: int
    username: str | None
    payment_id: int | None
    status: str
    plan_code: str | None
    currency: str
    original_amount: float
    discount_amount: float
    final_amount: float
    created_at: datetime
    cancelled_at: datetime | None


def _to_out(promo: PromoCode, referrer_username: str | None = None) -> PromoCodeOut:
    return PromoCodeOut(
        id=promo.id,
        code=promo.code,
        description=promo.description,
        discount_type=promo.discount_type,
        percent_off=float(promo.percent_off) if promo.percent_off is not None else None,
        amount_off=float(promo.amount_off) if promo.amount_off is not None else None,
        amount_off_currency=promo.amount_off_currency,
        applies_to_plan_codes=list(promo.applies_to_plan_codes or []),
        max_redemptions=promo.max_redemptions,
        redeemed_count=int(promo.redeemed_count or 0),
        valid_from=promo.valid_from,
        valid_until=promo.valid_until,
        is_active=promo.is_active,
        referrer_user_id=promo.referrer_user_id,
        referrer_username=referrer_username,
        created_at=promo.created_at,
        updated_at=promo.updated_at,
    )


async def _referrer_exists(db: DB, user_id: int) -> bool:
    return (
        await db.execute(select(User.id).where(User.id == user_id))
    ).scalar_one_or_none() is not None


@router.get("", response_model=list[PromoCodeOut])
async def list_promocodes(
    db: DB,
    _: CurrentAdmin,
    active: bool | None = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    referrer = aliased(User)
    base = select(PromoCode, referrer.username).outerjoin(
        referrer, referrer.id == PromoCode.referrer_user_id
    )
    if active is not None:
        base = base.where(PromoCode.is_active.is_(active))
    rows = (
        await db.execute(
            base.order_by(PromoCode.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return [_to_out(promo, username) for promo, username in rows]


@router.post("", response_model=PromoCodeOut)
async def create_promocode(payload: PromoCodeIn, db: DB, admin: CurrentAdmin, request: Request):
    existing = (
        await db.execute(select(PromoCode.id).where(PromoCode.code == payload.code))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "A promo code with this code already exists")
    if payload.referrer_user_id is not None and not await _referrer_exists(db, payload.referrer_user_id):
        raise HTTPException(400, "referrer_user_id does not match an existing user")

    promo = PromoCode(
        code=payload.code,
        description=payload.description,
        discount_type=payload.discount_type,
        percent_off=payload.percent_off,
        amount_off=payload.amount_off,
        amount_off_currency=payload.amount_off_currency,
        applies_to_plan_codes=payload.applies_to_plan_codes,
        max_redemptions=payload.max_redemptions,
        valid_from=payload.valid_from,
        valid_until=payload.valid_until,
        is_active=payload.is_active,
        referrer_user_id=payload.referrer_user_id,
        created_by_admin_id=admin.id,
    )
    db.add(promo)
    try:
        await db.flush()
    except IntegrityError as exc:  # pragma: no cover - race on unique code
        raise HTTPException(409, "A promo code with this code already exists") from exc

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="promocode.create",
        target_type="promo_code",
        target_id=promo.id,
        details=payload.model_dump(mode="json"),
        request=request,
    )
    return _to_out(promo)


@router.put("/{promo_id}", response_model=PromoCodeOut)
async def update_promocode(
    promo_id: int, payload: PromoCodeIn, db: DB, admin: CurrentAdmin, request: Request
):
    promo = (
        await db.execute(select(PromoCode).where(PromoCode.id == promo_id))
    ).scalar_one_or_none()
    if promo is None:
        raise HTTPException(404, "Promo code not found")

    if payload.code != promo.code:
        clash = (
            await db.execute(
                select(PromoCode.id).where(
                    PromoCode.code == payload.code, PromoCode.id != promo_id
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(409, "A promo code with this code already exists")
    if payload.referrer_user_id is not None and not await _referrer_exists(db, payload.referrer_user_id):
        raise HTTPException(400, "referrer_user_id does not match an existing user")
    if payload.max_redemptions is not None and payload.max_redemptions < int(promo.redeemed_count or 0):
        raise HTTPException(
            400,
            f"max_redemptions ({payload.max_redemptions}) is below the "
            f"current redeemed_count ({int(promo.redeemed_count or 0)})",
        )

    before = {
        "code": promo.code,
        "discount_type": promo.discount_type,
        "percent_off": float(promo.percent_off) if promo.percent_off is not None else None,
        "amount_off": float(promo.amount_off) if promo.amount_off is not None else None,
        "is_active": promo.is_active,
        "max_redemptions": promo.max_redemptions,
    }
    promo.code = payload.code
    promo.description = payload.description
    promo.discount_type = payload.discount_type
    promo.percent_off = payload.percent_off
    promo.amount_off = payload.amount_off
    promo.amount_off_currency = payload.amount_off_currency
    promo.applies_to_plan_codes = payload.applies_to_plan_codes
    promo.max_redemptions = payload.max_redemptions
    promo.valid_from = payload.valid_from
    promo.valid_until = payload.valid_until
    promo.is_active = payload.is_active
    promo.referrer_user_id = payload.referrer_user_id

    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="promocode.update",
        target_type="promo_code",
        target_id=promo.id,
        details={"before": before, "after": payload.model_dump(mode="json")},
        request=request,
    )
    return _to_out(promo)


@router.delete("/{promo_id}", response_model=PromoCodeOut)
async def deactivate_promocode(promo_id: int, db: DB, admin: CurrentAdmin, request: Request):
    """Soft-disable a promo code (kept for audit; redemptions reference it)."""
    promo = (
        await db.execute(select(PromoCode).where(PromoCode.id == promo_id))
    ).scalar_one_or_none()
    if promo is None:
        raise HTTPException(404, "Promo code not found")
    promo.is_active = False
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="promocode.deactivate",
        target_type="promo_code",
        target_id=promo.id,
        details={"code": promo.code},
        request=request,
    )
    return _to_out(promo)


@router.get("/{promo_id}/redemptions", response_model=list[PromoRedemptionOut])
async def list_redemptions(
    promo_id: int,
    db: DB,
    _: CurrentAdmin,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0, le=1_000_000),
):
    exists = (
        await db.execute(select(PromoCode.id).where(PromoCode.id == promo_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404, "Promo code not found")
    rows = (
        await db.execute(
            select(PromoRedemption, User.username)
            .outerjoin(User, User.id == PromoRedemption.user_id)
            .where(PromoRedemption.promo_code_id == promo_id)
            .order_by(PromoRedemption.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        PromoRedemptionOut(
            id=r.id,
            user_id=r.user_id,
            username=username,
            payment_id=r.payment_id,
            status=r.status,
            plan_code=r.plan_code,
            currency=r.currency,
            original_amount=float(r.original_amount),
            discount_amount=float(r.discount_amount),
            final_amount=float(r.final_amount),
            created_at=r.created_at,
            cancelled_at=r.cancelled_at,
        )
        for r, username in rows
    ]
