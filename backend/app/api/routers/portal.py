"""Public/member-facing portal API (GK-091).

Consumed by the `membership_saas/portal/` Next.js app over same-origin `/api/portal/*`.
Auth is the `membership_portal_session` HttpOnly cookie (see `app.api.deps`), except
`/redeem`, whose auth IS the one-time magic-link token in the body.

The bot does NOT call these endpoints — it issues magic links directly via
`portal_auth.issue_magic_link` (CLAUDE.md: bot ⇄ API through Postgres, not HTTP).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DB, CurrentPortalMember, CurrentPortalUser
from app.config import get_settings
from app.db.models import ArchiveModule, ArchiveVideo, ArchiveVideoModule, Plan
from app.services.portal_auth import redeem_magic_link, revoke_session
from app.services.subscription import get_active_subscription

router = APIRouter(prefix="/portal", tags=["portal"])
settings = get_settings()

# Cap for the LEGACY flat `/portal/videos` listing. As of GK-093 the portal UI
# navigates showcase-first (`/portal/modules` → `/portal/modules/{id}`), so the
# flat list is no longer the main path and never needs to hold the whole library;
# the cap just bounds the payload for any remaining caller.
_PORTAL_VIDEOS_LIMIT = 2000


# ─── Schemas ────────────────────────────────────────────────────────────
class RedeemRequest(BaseModel):
    token: str


class RedeemResponse(BaseModel):
    session_token: str
    max_age_seconds: int


class SubscriptionSummary(BaseModel):
    status: str
    plan_code: str | None = None
    plan_name: str | None = None
    expires_at: str | None = None


class MeResponse(BaseModel):
    tg_id: int
    username: str | None = None
    first_name: str | None = None
    has_access: bool
    subscription: SubscriptionSummary | None = None


class ModuleOut(BaseModel):
    id: int
    code: str
    title: str
    description: str | None = None
    sort_order: int
    # Ordered vimeo_ids in this module (GK-092 M2M grouping). A video may appear in
    # several modules; the client resolves each id against `videos` (the catalog).
    video_ids: list[int] = []


class VideoOut(BaseModel):
    vimeo_id: int
    title: str
    description: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None
    module_id: int | None = None
    sort_order: int


class VideosResponse(BaseModel):
    modules: list[ModuleOut]
    videos: list[VideoOut]
    # Visible videos that belong to no active module (rendered under "Остальные").
    orphan_video_ids: list[int] = []


class VideoDetail(BaseModel):
    vimeo_id: int
    title: str
    description: str | None = None
    duration_seconds: int | None = None
    player_embed_url: str
    module_id: int | None = None


class ModuleCard(BaseModel):
    """One showcase block on the archive index (GK-093 drill-down navigation).

    Mirrors a Vimeo Showcase tile: cover + title + video count, no videos yet.
    `id == 0` + `is_orphans` is the synthetic "Остальные видео" tile for visible
    videos that belong to no active module.
    """

    id: int
    code: str
    title: str
    description: str | None = None
    sort_order: int
    video_count: int
    cover_url: str | None = None
    is_orphans: bool = False


class ModulesResponse(BaseModel):
    modules: list[ModuleCard]


class ModuleVideosResponse(BaseModel):
    """One showcase's videos (the second navigation level)."""

    module: ModuleCard
    videos: list[VideoOut]


# Synthetic id/title for the "ungrouped videos" tile + page.
_ORPHANS_TITLE = "Остальные видео"
_ORPHANS_SORT = 10_000_000


# ─── Routes ─────────────────────────────────────────────────────────────
@router.post("/redeem", response_model=RedeemResponse)
async def redeem(body: RedeemRequest, request: Request, db: DB) -> RedeemResponse:
    """Consume a one-time magic link and open a portal session.

    Called server-side by the portal `/auth/magic` route, which then sets the
    HttpOnly `membership_portal_session` cookie from the returned token. Errors are
    deliberately coarse (the client only learns "invalid" vs "no access").
    """
    result = await redeem_magic_link(
        db,
        body.token,
        user_agent=request.headers.get("user-agent"),
        ip=request.headers.get("x-forwarded-for", "").split(",")[0].strip() or None,
    )
    if not result.ok or result.session_token is None:
        if result.reason == "no_access":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Subscription inactive"
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid link")
    # Commit the new session NOW, before the token reaches the client. get_db
    # otherwise commits during teardown — after the response is sent — which
    # races the portal's immediate follow-up request (the cookie would point at
    # an as-yet-uncommitted session and the next /me or /videos would 401).
    await db.commit()
    return RedeemResponse(
        session_token=result.session_token,
        max_age_seconds=settings.portal_session_days * 86400,
    )


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentPortalUser, db: DB) -> MeResponse:
    sub = await get_active_subscription(db, user.id)
    summary: SubscriptionSummary | None = None
    if sub is not None:
        # Fetch the plan explicitly — get_active_subscription does not eager-load
        # the relationship, and a lazy load under async SQLAlchemy would raise.
        plan = (
            await db.execute(select(Plan).where(Plan.id == sub.plan_id))
        ).scalar_one_or_none()
        summary = SubscriptionSummary(
            status=sub.status,
            plan_code=getattr(plan, "code", None),
            plan_name=getattr(plan, "name", None),
            expires_at=sub.expires_at.isoformat() if sub.expires_at else None,
        )
    return MeResponse(
        tg_id=user.tg_id,
        username=user.username,
        first_name=user.first_name,
        has_access=sub is not None,
        subscription=summary,
    )


@router.get("/videos", response_model=VideosResponse)
async def list_videos(_: CurrentPortalMember, db: DB) -> VideosResponse:
    modules = (
        await db.execute(
            select(ArchiveModule)
            .where(ArchiveModule.is_active.is_(True))
            .order_by(ArchiveModule.sort_order.asc(), ArchiveModule.id.asc())
        )
    ).scalars().all()
    # Cap the catalog so a large library (e.g. 1000s of synced videos) does not
    # produce a multi-MB page. Phase-2 follow-up: per-module pagination.
    videos = (
        await db.execute(
            select(ArchiveVideo)
            .where(ArchiveVideo.visibility == "visible")
            .order_by(
                ArchiveVideo.module_id.asc().nulls_last(),
                ArchiveVideo.sort_order.asc(),
                ArchiveVideo.id.asc(),
            )
            .limit(_PORTAL_VIDEOS_LIMIT)
        )
    ).scalars().all()
    catalog_ids = {v.id for v in videos}
    vimeo_by_id = {v.id: v.vimeo_id for v in videos}

    # Effective M2M memberships into active modules, in Vimeo/admin order (GK-092).
    # A video may land in several modules → it appears under each.
    mem_rows = (
        await db.execute(
            select(ArchiveVideoModule.module_id, ArchiveVideoModule.video_id)
            .join(ArchiveModule, ArchiveModule.id == ArchiveVideoModule.module_id)
            .where(
                ArchiveVideoModule.removed_by_admin.is_(False),
                ArchiveModule.is_active.is_(True),
            )
            .order_by(
                ArchiveVideoModule.module_id.asc(),
                ArchiveVideoModule.sort_order.asc(),
                ArchiveVideoModule.id.asc(),
            )
        )
    ).all()
    per_module: dict[int, list[int]] = {}
    grouped_video_ids: set[int] = set()
    for module_id, video_id in mem_rows:
        if video_id not in catalog_ids:
            continue  # video hidden or beyond the catalog cap
        per_module.setdefault(module_id, []).append(vimeo_by_id[video_id])
        grouped_video_ids.add(video_id)

    orphan_video_ids = [
        v.vimeo_id for v in videos if v.id not in grouped_video_ids
    ]

    return VideosResponse(
        modules=[
            ModuleOut(
                id=m.id,
                code=m.code,
                title=m.title,
                description=m.description,
                sort_order=m.sort_order,
                video_ids=per_module.get(m.id, []),
            )
            for m in modules
        ],
        videos=[
            VideoOut(
                vimeo_id=v.vimeo_id,
                title=v.title,
                description=v.description,
                duration_seconds=v.duration_seconds,
                thumbnail_url=v.thumbnail_url,
                module_id=v.module_id,
                sort_order=v.sort_order,
            )
            for v in videos
        ],
        orphan_video_ids=orphan_video_ids,
    )


@router.get("/videos/{vimeo_id}", response_model=VideoDetail)
async def video_detail(vimeo_id: int, _: CurrentPortalMember, db: DB) -> VideoDetail:
    video = (
        await db.execute(
            select(ArchiveVideo).where(
                ArchiveVideo.vimeo_id == vimeo_id,
                ArchiveVideo.visibility == "visible",
            )
        )
    ).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return VideoDetail(
        vimeo_id=video.vimeo_id,
        title=video.title,
        description=video.description,
        duration_seconds=video.duration_seconds,
        player_embed_url=video.player_embed_url
        or f"https://player.vimeo.com/video/{video.vimeo_id}",
        module_id=video.module_id,
    )


@router.get("/modules", response_model=ModulesResponse)
async def list_module_cards(_: CurrentPortalMember, db: DB) -> ModulesResponse:
    """Archive index: one card per active showcase (GK-093 drill-down level 1).

    Returns only metadata + a cover thumbnail + a visible-video count — never the
    videos themselves — so the index stays light no matter how big the library is.
    Empty modules are omitted; a synthetic "Остальные" card is appended when some
    visible videos belong to no active module.
    """
    modules = (
        await db.execute(
            select(ArchiveModule)
            .where(ArchiveModule.is_active.is_(True))
            .order_by(ArchiveModule.sort_order.asc(), ArchiveModule.id.asc())
        )
    ).scalars().all()

    # Effective memberships into active modules, joined to visible videos, in the
    # per-module order. One pass gives us count + the fallback cover (first
    # video's thumbnail) for showcases that do not provide their own cover.
    rows = (
        await db.execute(
            select(
                ArchiveVideoModule.module_id,
                ArchiveVideo.id,
                ArchiveVideo.thumbnail_url,
            )
            .join(ArchiveVideo, ArchiveVideo.id == ArchiveVideoModule.video_id)
            .join(ArchiveModule, ArchiveModule.id == ArchiveVideoModule.module_id)
            .where(
                ArchiveVideoModule.removed_by_admin.is_(False),
                ArchiveModule.is_active.is_(True),
                ArchiveVideo.visibility == "visible",
            )
            .order_by(
                ArchiveVideoModule.module_id.asc(),
                ArchiveVideoModule.sort_order.asc(),
                ArchiveVideoModule.id.asc(),
            )
        )
    ).all()
    counts: dict[int, int] = {}
    cover: dict[int, str | None] = {}
    grouped: set[int] = set()
    for module_id, video_id, thumb in rows:
        counts[module_id] = counts.get(module_id, 0) + 1
        if module_id not in cover and thumb:
            cover[module_id] = thumb
        grouped.add(video_id)

    cards = [
        ModuleCard(
            id=m.id,
            code=m.code,
            title=m.title,
            description=m.description,
            sort_order=m.sort_order,
            video_count=counts.get(m.id, 0),
            cover_url=m.cover_url or cover.get(m.id),
        )
        for m in modules
        if counts.get(m.id, 0) > 0
    ]

    # Orphans = visible videos in no active-module effective membership.
    visible = (
        await db.execute(
            select(ArchiveVideo.id, ArchiveVideo.thumbnail_url)
            .where(ArchiveVideo.visibility == "visible")
            .order_by(
                ArchiveVideo.module_id.asc().nulls_last(),
                ArchiveVideo.sort_order.asc(),
                ArchiveVideo.id.asc(),
            )
        )
    ).all()
    orphan_count = 0
    orphan_cover: str | None = None
    for vid_id, thumb in visible:
        if vid_id not in grouped:
            orphan_count += 1
            if orphan_cover is None and thumb:
                orphan_cover = thumb
    if orphan_count > 0:
        cards.append(
            ModuleCard(
                id=0,
                code="orphans",
                title=_ORPHANS_TITLE,
                description=None,
                sort_order=_ORPHANS_SORT,
                video_count=orphan_count,
                cover_url=orphan_cover,
                is_orphans=True,
            )
        )
    return ModulesResponse(modules=cards)


@router.get("/modules/{module_id}", response_model=ModuleVideosResponse)
async def module_card_videos(
    module_id: int, _: CurrentPortalMember, db: DB
) -> ModuleVideosResponse:
    """One showcase's videos in showcase order (GK-093 drill-down level 2)."""
    module = (
        await db.execute(
            select(ArchiveModule).where(
                ArchiveModule.id == module_id, ArchiveModule.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if module is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Module not found")

    rows = (
        await db.execute(
            select(ArchiveVideo, ArchiveVideoModule.sort_order)
            .join(ArchiveVideoModule, ArchiveVideoModule.video_id == ArchiveVideo.id)
            .where(
                ArchiveVideoModule.module_id == module_id,
                ArchiveVideoModule.removed_by_admin.is_(False),
                ArchiveVideo.visibility == "visible",
            )
            .order_by(
                ArchiveVideoModule.sort_order.asc(), ArchiveVideoModule.id.asc()
            )
        )
    ).all()
    videos = [
        VideoOut(
            vimeo_id=v.vimeo_id,
            title=v.title,
            description=v.description,
            duration_seconds=v.duration_seconds,
            thumbnail_url=v.thumbnail_url,
            module_id=module.id,
            sort_order=order,
        )
        for v, order in rows
    ]
    card = ModuleCard(
        id=module.id,
        code=module.code,
        title=module.title,
        description=module.description,
        sort_order=module.sort_order,
        video_count=len(videos),
        cover_url=module.cover_url or (videos[0].thumbnail_url if videos else None),
    )
    return ModuleVideosResponse(module=card, videos=videos)


@router.get("/orphans", response_model=ModuleVideosResponse)
async def orphan_card_videos(_: CurrentPortalMember, db: DB) -> ModuleVideosResponse:
    """The "Остальные видео" page: visible videos in no active module (GK-093)."""
    grouped_subq = (
        select(ArchiveVideoModule.video_id)
        .join(ArchiveModule, ArchiveModule.id == ArchiveVideoModule.module_id)
        .where(
            ArchiveVideoModule.removed_by_admin.is_(False),
            ArchiveModule.is_active.is_(True),
        )
    )
    rows = (
        await db.execute(
            select(ArchiveVideo)
            .where(
                ArchiveVideo.visibility == "visible",
                ArchiveVideo.id.not_in(grouped_subq),
            )
            .order_by(ArchiveVideo.sort_order.asc(), ArchiveVideo.id.asc())
        )
    ).scalars().all()
    videos = [
        VideoOut(
            vimeo_id=v.vimeo_id,
            title=v.title,
            description=v.description,
            duration_seconds=v.duration_seconds,
            thumbnail_url=v.thumbnail_url,
            module_id=None,
            sort_order=v.sort_order,
        )
        for v in rows
    ]
    card = ModuleCard(
        id=0,
        code="orphans",
        title=_ORPHANS_TITLE,
        description=None,
        sort_order=_ORPHANS_SORT,
        video_count=len(videos),
        cover_url=videos[0].thumbnail_url if videos else None,
        is_orphans=True,
    )
    return ModuleVideosResponse(module=card, videos=videos)


@router.post("/logout")
async def logout(request: Request, db: DB) -> dict:
    """Revoke the current session row. Portal clears the cookie on its side.

    Intentionally not gated on `current_portal_user` so a stale/expired cookie
    can still be cleaned up without a 401 first.
    """
    raw = request.cookies.get("membership_portal_session")
    await revoke_session(db, raw)
    # Commit before responding so the revoke is effective immediately (see the
    # redeem note above on get_db's deferred teardown commit).
    await db.commit()
    return {"ok": True}
