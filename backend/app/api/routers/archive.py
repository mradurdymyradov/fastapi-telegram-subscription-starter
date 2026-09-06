"""Admin-facing curation API for the Vimeo member portal archive (GK-091/GK-092).

Admins use these endpoints to manage modules, assign videos to modules (now
many-to-many), set sort order and visibility, and trigger / inspect the Vimeo
sync (per-video metadata + showcase→module grouping). The portal itself never
calls these — it reads `/api/portal/*`. All routes require an admin JWT and write
actions are audit-logged.

GK-092: modules are seeded from Vimeo showcases (`vimeo_album_id` set ⇒ source
"vimeo") and editable; admin-created modules (`vimeo_album_id` NULL ⇒ source
"manual") are never touched by sync. Membership lives in `archive_video_modules`;
an admin removal of a synced membership is a tombstone (`removed_by_admin`) so it
survives re-sync.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from app.api.deps import DB, CurrentAdmin
from app.config import get_settings
from app.db.models import ArchiveModule, ArchiveVideo, ArchiveVideoModule
from app.services.audit import record as audit_record
from app.services.vimeo_sync import recompute_primary_module, sync_archive

router = APIRouter(prefix="/archive", tags=["archive"])
settings = get_settings()

Visibility = Literal["visible", "hidden", "draft"]


# ─── Schemas ────────────────────────────────────────────────────────────
class VideoModuleMembership(BaseModel):
    module_id: int
    source: str  # "vimeo" | "admin"


class VideoAdminOut(BaseModel):
    id: int
    vimeo_id: int
    title: str
    description: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    # Legacy derived "primary module" (lowest-sorted active module); grouping is M2M.
    module_id: int | None = None
    # Effective memberships (admin tombstones excluded) — the M2M grouping.
    memberships: list[VideoModuleMembership] = []
    sort_order: int
    visibility: str
    vimeo_privacy: str | None = None
    player_embed_url: str | None = None
    synced_at: str | None = None
    # True when the Vimeo privacy isn't embed-locked → leaking outside the portal.
    privacy_warning: bool = False


class VideoPatch(BaseModel):
    # Module assignment moved to the membership endpoints (M2M). This patches only
    # per-video presentation fields.
    sort_order: Annotated[int | None, Field(default=None, ge=0, le=100000)] = None
    visibility: Visibility | None = None
    title: Annotated[str | None, Field(default=None, min_length=1, max_length=255)] = None


class ModuleOut(BaseModel):
    id: int
    code: str
    title: str
    description: str | None = None
    sort_order: int
    is_active: bool
    video_count: int = 0
    # "vimeo" ⇒ seeded from a Vimeo showcase (sync-managed); "manual" ⇒ admin-created.
    source: str = "manual"
    vimeo_album_id: str | None = None


class ModuleIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    sort_order: Annotated[int, Field(ge=0, le=100000)] = 0
    is_active: bool = True


class ModulePatch(BaseModel):
    title: Annotated[str | None, Field(default=None, min_length=1, max_length=255)] = None
    description: Annotated[str | None, Field(default=None, max_length=2000)] = None
    sort_order: Annotated[int | None, Field(default=None, ge=0, le=100000)] = None
    is_active: bool | None = None


class SyncStatus(BaseModel):
    token_configured: bool
    last_synced_at: str | None = None
    total_videos: int
    visible_videos: int
    hidden_videos: int
    privacy_warnings: int
    # GK-092 showcase grouping.
    synced_modules: int = 0
    manual_modules: int = 0
    total_memberships: int = 0


class SyncRunResult(BaseModel):
    # Per-video metadata sync (GK-091).
    ok: bool
    skipped: bool
    created: int
    updated: int
    hidden: int
    total_fetched: int
    error: str | None = None
    warnings: list[str] = []
    # Showcase→module sync (GK-092).
    showcase_ok: bool = False
    showcase_skipped: bool = False
    showcase_error: str | None = None
    modules_created: int = 0
    modules_updated: int = 0
    memberships_added: int = 0
    memberships_removed: int = 0


_PRIVACY_OK = {None, "disable", "nobody"}


def _privacy_warning(vimeo_privacy: str | None) -> bool:
    return vimeo_privacy not in _PRIVACY_OK


def _video_out(v: ArchiveVideo, memberships: list[ArchiveVideoModule]) -> VideoAdminOut:
    return VideoAdminOut(
        id=v.id,
        vimeo_id=v.vimeo_id,
        title=v.title,
        description=v.description,
        duration_seconds=v.duration_seconds,
        thumbnail_url=v.thumbnail_url,
        module_id=v.module_id,
        memberships=[
            VideoModuleMembership(module_id=m.module_id, source=m.source)
            for m in memberships
        ],
        sort_order=v.sort_order,
        visibility=v.visibility,
        vimeo_privacy=v.vimeo_privacy,
        player_embed_url=v.player_embed_url,
        synced_at=v.synced_at.isoformat() if v.synced_at else None,
        privacy_warning=_privacy_warning(v.vimeo_privacy),
    )


def _module_out(m: ArchiveModule, video_count: int) -> ModuleOut:
    return ModuleOut(
        id=m.id,
        code=m.code,
        title=m.title,
        description=m.description,
        sort_order=m.sort_order,
        is_active=m.is_active,
        video_count=video_count,
        source="vimeo" if m.vimeo_album_id else "manual",
        vimeo_album_id=m.vimeo_album_id,
    )


# ─── Videos ─────────────────────────────────────────────────────────────
@router.get("/videos", response_model=list[VideoAdminOut])
async def list_videos(db: DB, _: CurrentAdmin):
    rows = (
        await db.execute(
            select(ArchiveVideo).order_by(
                ArchiveVideo.module_id.asc().nulls_last(),
                ArchiveVideo.sort_order.asc(),
                ArchiveVideo.id.asc(),
            )
        )
    ).scalars().all()
    mems = (
        await db.execute(
            select(ArchiveVideoModule).where(
                ArchiveVideoModule.removed_by_admin.is_(False)
            )
        )
    ).scalars().all()
    by_video: dict[int, list[ArchiveVideoModule]] = {}
    for m in mems:
        by_video.setdefault(m.video_id, []).append(m)
    return [_video_out(v, by_video.get(v.id, [])) for v in rows]


@router.patch("/videos/{video_id}", response_model=VideoAdminOut)
async def update_video(
    video_id: int, payload: VideoPatch, db: DB, admin: CurrentAdmin, request: Request
):
    v = (
        await db.execute(select(ArchiveVideo).where(ArchiveVideo.id == video_id))
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(404, "Video not found")

    fields = payload.model_dump(exclude_unset=True)
    for key, value in fields.items():
        setattr(v, key, value)
    await db.flush()
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="archive.video.update",
        target_type="archive_video",
        target_id=v.id,
        details=fields,
        request=request,
    )
    mems = (
        await db.execute(
            select(ArchiveVideoModule).where(
                ArchiveVideoModule.video_id == v.id,
                ArchiveVideoModule.removed_by_admin.is_(False),
            )
        )
    ).scalars().all()
    return _video_out(v, mems)


# ─── Video ↔ module membership (M2M, GK-092) ────────────────────────────
@router.post("/videos/{video_id}/modules/{module_id}")
async def add_video_module(
    video_id: int, module_id: int, db: DB, admin: CurrentAdmin, request: Request
):
    """Add a video to a module (admin membership), or un-tombstone a synced one."""
    video = (
        await db.execute(select(ArchiveVideo.id).where(ArchiveVideo.id == video_id))
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(404, "Video not found")
    module = (
        await db.execute(select(ArchiveModule.id).where(ArchiveModule.id == module_id))
    ).scalar_one_or_none()
    if module is None:
        raise HTTPException(404, "Module not found")

    row = (
        await db.execute(
            select(ArchiveVideoModule).where(
                ArchiveVideoModule.video_id == video_id,
                ArchiveVideoModule.module_id == module_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        max_order = (
            await db.execute(
                select(func.max(ArchiveVideoModule.sort_order)).where(
                    ArchiveVideoModule.module_id == module_id
                )
            )
        ).scalar_one()
        db.add(
            ArchiveVideoModule(
                video_id=video_id,
                module_id=module_id,
                source="admin",
                sort_order=(max_order or 0) + 1,
                removed_by_admin=False,
            )
        )
    else:
        # Re-adding a previously tombstoned (or already present) membership.
        row.removed_by_admin = False
    await db.flush()
    await recompute_primary_module(db, video_id)
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="archive.membership.add",
        target_type="archive_video",
        target_id=video_id,
        details={"module_id": module_id},
        request=request,
    )
    return {"ok": True}


@router.delete("/videos/{video_id}/modules/{module_id}")
async def remove_video_module(
    video_id: int, module_id: int, db: DB, admin: CurrentAdmin, request: Request
):
    """Remove a video from a module.

    An admin-added membership is deleted outright; a Vimeo-synced one is tombstoned
    (`removed_by_admin=True`) so the removal survives the next sync.
    """
    row = (
        await db.execute(
            select(ArchiveVideoModule).where(
                ArchiveVideoModule.video_id == video_id,
                ArchiveVideoModule.module_id == module_id,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.removed_by_admin:
        raise HTTPException(404, "Membership not found")
    if row.source == "admin":
        await db.delete(row)
    else:
        row.removed_by_admin = True
    await db.flush()
    await recompute_primary_module(db, video_id)
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="archive.membership.remove",
        target_type="archive_video",
        target_id=video_id,
        details={"module_id": module_id, "source": row.source},
        request=request,
    )
    return {"ok": True}


# ─── Modules ────────────────────────────────────────────────────────────
@router.get("/modules", response_model=list[ModuleOut])
async def list_modules(db: DB, _: CurrentAdmin):
    # Count effective memberships per module (admin tombstones excluded).
    counts = dict(
        (
            await db.execute(
                select(
                    ArchiveVideoModule.module_id, func.count(ArchiveVideoModule.id)
                )
                .where(ArchiveVideoModule.removed_by_admin.is_(False))
                .group_by(ArchiveVideoModule.module_id)
            )
        ).all()
    )
    rows = (
        await db.execute(
            select(ArchiveModule).order_by(ArchiveModule.sort_order.asc(), ArchiveModule.id.asc())
        )
    ).scalars().all()
    return [_module_out(m, int(counts.get(m.id, 0))) for m in rows]


@router.post("/modules", response_model=ModuleOut)
async def create_module(payload: ModuleIn, db: DB, admin: CurrentAdmin, request: Request):
    dupe = (
        await db.execute(select(ArchiveModule.id).where(ArchiveModule.code == payload.code))
    ).scalar_one_or_none()
    if dupe is not None:
        raise HTTPException(409, "Module code already exists")
    m = ArchiveModule(**payload.model_dump())
    db.add(m)
    await db.flush()
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="archive.module.create",
        target_type="archive_module",
        target_id=m.id,
        details=payload.model_dump(),
        request=request,
    )
    return _module_out(m, 0)


@router.patch("/modules/{module_id}", response_model=ModuleOut)
async def update_module(
    module_id: int, payload: ModulePatch, db: DB, admin: CurrentAdmin, request: Request
):
    m = (
        await db.execute(select(ArchiveModule).where(ArchiveModule.id == module_id))
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "Module not found")
    fields = payload.model_dump(exclude_unset=True)
    for key, value in fields.items():
        setattr(m, key, value)
    await db.flush()
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="archive.module.update",
        target_type="archive_module",
        target_id=m.id,
        details=fields,
        request=request,
    )
    count = (
        await db.execute(
            select(func.count(ArchiveVideoModule.id)).where(
                ArchiveVideoModule.module_id == m.id,
                ArchiveVideoModule.removed_by_admin.is_(False),
            )
        )
    ).scalar_one()
    return _module_out(m, int(count))


@router.delete("/modules/{module_id}")
async def delete_module(module_id: int, db: DB, admin: CurrentAdmin, request: Request):
    m = (
        await db.execute(select(ArchiveModule).where(ArchiveModule.id == module_id))
    ).scalar_one_or_none()
    if m is None:
        raise HTTPException(404, "Module not found")
    if m.vimeo_album_id is not None:
        # The next sync would just recreate it. Admin should hide (is_active=false).
        raise HTTPException(
            409,
            "Synced module — hide it instead (the next Vimeo sync would recreate a deleted showcase).",
        )
    # Manual module: drop its memberships and detach the legacy primary-module FK,
    # then delete. Videos themselves are never destroyed.
    await db.execute(
        delete(ArchiveVideoModule).where(ArchiveVideoModule.module_id == module_id)
    )
    await db.execute(
        update(ArchiveVideo)
        .where(ArchiveVideo.module_id == module_id)
        .values(module_id=None)
    )
    await db.delete(m)
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="archive.module.delete",
        target_type="archive_module",
        target_id=module_id,
        details={"code": m.code},
        request=request,
    )
    return {"ok": True}


# ─── Sync ───────────────────────────────────────────────────────────────
@router.get("/sync/status", response_model=SyncStatus)
async def sync_status(db: DB, _: CurrentAdmin):
    total = (await db.execute(select(func.count(ArchiveVideo.id)))).scalar_one()
    visible = (
        await db.execute(
            select(func.count(ArchiveVideo.id)).where(ArchiveVideo.visibility == "visible")
        )
    ).scalar_one()
    hidden = (
        await db.execute(
            select(func.count(ArchiveVideo.id)).where(ArchiveVideo.visibility == "hidden")
        )
    ).scalar_one()
    last = (await db.execute(select(func.max(ArchiveVideo.synced_at)))).scalar_one()
    warnings = (
        await db.execute(
            select(func.count(ArchiveVideo.id)).where(
                ArchiveVideo.vimeo_privacy.isnot(None),
                ArchiveVideo.vimeo_privacy.notin_(["disable", "nobody"]),
            )
        )
    ).scalar_one()
    synced_modules = (
        await db.execute(
            select(func.count(ArchiveModule.id)).where(
                ArchiveModule.vimeo_album_id.isnot(None)
            )
        )
    ).scalar_one()
    manual_modules = (
        await db.execute(
            select(func.count(ArchiveModule.id)).where(
                ArchiveModule.vimeo_album_id.is_(None)
            )
        )
    ).scalar_one()
    memberships = (
        await db.execute(
            select(func.count(ArchiveVideoModule.id)).where(
                ArchiveVideoModule.removed_by_admin.is_(False)
            )
        )
    ).scalar_one()
    return SyncStatus(
        token_configured=bool(settings.vimeo_api_token),
        last_synced_at=last.isoformat() if last else None,
        total_videos=int(total),
        visible_videos=int(visible),
        hidden_videos=int(hidden),
        privacy_warnings=int(warnings),
        synced_modules=int(synced_modules),
        manual_modules=int(manual_modules),
        total_memberships=int(memberships),
    )


@router.post("/sync", response_model=SyncRunResult)
async def run_sync(db: DB, admin: CurrentAdmin, request: Request):
    result = await sync_archive(db)
    v, s = result.videos, result.showcases
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="archive.sync.run",
        target_type="archive",
        target_id=None,
        details={
            "videos": {
                "ok": v.ok,
                "skipped": v.skipped,
                "created": v.created,
                "updated": v.updated,
                "hidden": v.hidden,
                "error": v.error,
            },
            "showcases": {
                "ok": s.ok,
                "skipped": s.skipped,
                "modules_created": s.modules_created,
                "modules_updated": s.modules_updated,
                "memberships_added": s.memberships_added,
                "memberships_removed": s.memberships_removed,
                "error": s.error,
            },
        },
        request=request,
    )
    return SyncRunResult(
        ok=v.ok,
        skipped=v.skipped,
        created=v.created,
        updated=v.updated,
        hidden=v.hidden,
        total_fetched=v.total_fetched,
        error=v.error,
        warnings=v.warnings[:50],
        showcase_ok=s.ok,
        showcase_skipped=s.skipped,
        showcase_error=s.error,
        modules_created=s.modules_created,
        modules_updated=s.modules_updated,
        memberships_added=s.memberships_added,
        memberships_removed=s.memberships_removed,
    )
