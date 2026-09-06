"""Read-only Vimeo → archive sync (GK-091 videos + GK-092 showcases).

We never upload, edit, or delete on Vimeo; the Vimeo dashboard stays Grant/Owner's
editorial surface. Two concerns, both upserts that preserve admin curation:

GK-091 — per-video metadata. Pull `/me/videos` and upsert by `vimeo_id`,
preserving admin-owned curation (`sort_order`, `visibility`).

GK-092 — showcase → module grouping (hybrid auto + admin override). Pull
`/me/albums` (Vimeo "showcases") and their ordered membership, then seed
`ArchiveModule`s (keyed by `vimeo_album_id`) and the many-to-many
`ArchiveVideoModule` links. Admin edits survive re-sync: module renames via a
last-synced snapshot 3-way merge; hide/reorder because sync seeds them once;
membership add/remove via `source` + the `removed_by_admin` tombstone.

Everything is structured so the pure transforms / DB upserts are independently
testable from the network fetch:

- `fetch_vimeo_videos` / `fetch_showcases` — the only functions that touch the network.
- `parse_vimeo_video` / `parse_vimeo_album` — pure transforms of one Vimeo payload.
- `upsert_videos` / `upsert_showcase_modules` / `reconcile_membership` — pure DB upserts.

Failure policy (§5.5): on token-missing / 429 / network error we abort without
mutating any row, so the last good sync keeps rendering. The showcase fetch reads
*all* albums + memberships before any DB write, so a mid-fetch failure mutates nothing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ArchiveModule, ArchiveVideo, ArchiveVideoModule, utcnow

logger = logging.getLogger(__name__)
settings = get_settings()

_VIMEO_API = "https://api.vimeo.com"
_VIDEO_FIELDS = (
    "uri,name,description,duration,pictures,privacy,player_embed_url,"
    "created_time,modified_time"
)
_PER_PAGE = 100
_MAX_PAGES = 50  # hard stop so a paging bug can't loop forever


@dataclass
class SyncResult:
    ok: bool
    created: int = 0
    updated: int = 0
    hidden: int = 0
    total_fetched: int = 0
    skipped: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def _vimeo_id_from_uri(uri: str | None) -> int | None:
    """'/videos/12345' -> 12345."""
    if not uri:
        return None
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _best_thumbnail(pictures: dict | None) -> str | None:
    if not isinstance(pictures, dict):
        return None
    sizes = pictures.get("sizes")
    if isinstance(sizes, list) and sizes:
        # Vimeo returns sizes ascending by width; the last is the largest.
        last = sizes[-1]
        if isinstance(last, dict) and last.get("link"):
            return str(last["link"])[:512]
    base = pictures.get("base_link")
    return str(base)[:512] if base else None


def parse_vimeo_video(raw: dict) -> dict | None:
    """Map one Vimeo API video object to our `archive_videos` column dict.

    Returns None for a payload with no resolvable numeric id (defensive — every
    real Vimeo video has a `/videos/<id>` uri).
    """
    vimeo_id = _vimeo_id_from_uri(raw.get("uri"))
    if vimeo_id is None:
        return None

    privacy = raw.get("privacy") or {}
    duration = raw.get("duration")
    embed = raw.get("player_embed_url") or f"https://player.vimeo.com/video/{vimeo_id}"
    return {
        "vimeo_id": vimeo_id,
        "title": (raw.get("name") or f"Video {vimeo_id}")[:255],
        "description": raw.get("description") or None,
        "duration_seconds": int(duration) if isinstance(duration, (int, float)) else None,
        "thumbnail_url": _best_thumbnail(raw.get("pictures")),
        "vimeo_privacy": (privacy.get("view") if isinstance(privacy, dict) else None),
        "player_embed_url": str(embed)[:512],
    }


async def fetch_vimeo_videos(token: str) -> list[dict]:
    """Fetch all `/me/videos` pages. Raises httpx.HTTPError on failure."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.vimeo.*+json;version=3.4",
    }
    out: list[dict] = []
    path: str | None = f"/me/videos?per_page={_PER_PAGE}&fields={_VIDEO_FIELDS}"
    async with httpx.AsyncClient(base_url=_VIMEO_API, timeout=30.0) as client:
        for _ in range(_MAX_PAGES):
            if not path:
                break
            resp = await client.get(path, headers=headers)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data") or []
            out.extend(d for d in data if isinstance(d, dict))
            paging = body.get("paging") or {}
            path = paging.get("next")
    return out


async def upsert_videos(
    session: AsyncSession,
    parsed: list[dict],
    *,
    now: datetime | None = None,
    mark_missing_hidden: bool = True,
) -> SyncResult:
    """Idempotent upsert by `vimeo_id`, preserving admin-owned curation fields.

    `module_id`, `sort_order`, and `visibility` are never overwritten on an
    existing row — those are the admin's editorial decisions. A row whose Vimeo
    id is absent from this sync is flagged `visibility='hidden'` (never deleted),
    but only when `mark_missing_hidden` is set (i.e. the fetch was complete).
    """
    now = now or utcnow()
    result = SyncResult(ok=True, total_fetched=len(parsed))

    existing = {
        v.vimeo_id: v
        for v in (
            await session.execute(select(ArchiveVideo))
        ).scalars().all()
    }
    seen_ids: set[int] = set()

    for row in parsed:
        vimeo_id = row["vimeo_id"]
        seen_ids.add(vimeo_id)
        current = existing.get(vimeo_id)
        if current is None:
            session.add(
                ArchiveVideo(
                    vimeo_id=vimeo_id,
                    title=row["title"],
                    description=row["description"],
                    duration_seconds=row["duration_seconds"],
                    thumbnail_url=row["thumbnail_url"],
                    vimeo_privacy=row["vimeo_privacy"],
                    player_embed_url=row["player_embed_url"],
                    synced_at=now,
                    created_at=now,
                )
            )
            result.created += 1
        else:
            current.title = row["title"]
            current.description = row["description"]
            current.duration_seconds = row["duration_seconds"]
            current.thumbnail_url = row["thumbnail_url"]
            current.vimeo_privacy = row["vimeo_privacy"]
            current.player_embed_url = row["player_embed_url"]
            current.synced_at = now
            result.updated += 1
        # Surface unintentionally-public videos to the admin (BLK-010 hygiene).
        if row["vimeo_privacy"] not in (None, "disable", "nobody"):
            result.warnings.append(
                f"vimeo {vimeo_id}: privacy='{row['vimeo_privacy']}' (not embed-locked)"
            )

    if mark_missing_hidden:
        for vimeo_id, video in existing.items():
            if vimeo_id not in seen_ids and video.visibility != "hidden":
                video.visibility = "hidden"
                video.synced_at = now
                result.hidden += 1

    await session.flush()
    return result


async def sync_videos(
    session: AsyncSession,
    *,
    token: str | None = None,
    now: datetime | None = None,
) -> SyncResult:
    """End-to-end sync orchestration. Never raises; returns a SyncResult."""
    token = token if token is not None else settings.vimeo_api_token
    if not token:
        logger.info("vimeo sync skipped: VIMEO_API_TOKEN not set")
        return SyncResult(ok=False, skipped=True, error="no_token")

    try:
        raw_videos = await fetch_vimeo_videos(token)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code if e.response is not None else "?"
        logger.error("vimeo sync fetch failed: HTTP %s — leaving rows untouched", code)
        return SyncResult(ok=False, error=f"http_{code}")
    except httpx.HTTPError as e:
        logger.error("vimeo sync fetch failed: %s — leaving rows untouched", e)
        return SyncResult(ok=False, error="network")

    parsed = [p for p in (parse_vimeo_video(v) for v in raw_videos) if p is not None]
    result = await upsert_videos(session, parsed, now=now, mark_missing_hidden=True)
    logger.info(
        "vimeo sync: %d fetched, %d created, %d updated, %d hidden",
        result.total_fetched,
        result.created,
        result.updated,
        result.hidden,
    )
    return result


# ─── GK-092: Vimeo Showcases → archive modules (hybrid auto + admin override) ──

# Module fields seeded from the showcase and kept in the 3-way merge. `is_active`
# (hide) and `sort_order` (reorder) are seeded once on create and NEVER re-written
# by sync, so those admin overrides always survive.
_MODULE_MERGE_FIELDS = ("title", "description", "cover_url")
_ALBUM_FIELDS = "uri,name,description,pictures"


@dataclass
class ShowcaseFetch:
    """Everything the showcase sync read from Vimeo, captured before any DB write."""

    albums: list[dict]                  # parsed album dicts, in Vimeo order
    members: dict[str, list[int]]       # vimeo_album_id -> ordered vimeo video ids


@dataclass
class ShowcaseSyncResult:
    ok: bool
    skipped: bool = False
    modules_created: int = 0
    modules_updated: int = 0
    memberships_added: int = 0
    memberships_removed: int = 0
    albums_seen: int = 0
    error: str | None = None
    # Videos whose memberships changed — used to recompute the derived primary module.
    affected_video_ids: set[int] = field(default_factory=set)


@dataclass
class ArchiveSyncResult:
    """Combined result of the per-video sync and the showcase sync."""

    videos: SyncResult
    showcases: ShowcaseSyncResult


def _album_id_from_uri(uri: str | None) -> str | None:
    """'/me/albums/12345' or '/users/9/albums/12345' -> '12345'."""
    if not uri:
        return None
    tail = uri.rstrip("/").rsplit("/", 1)[-1]
    return tail or None


def parse_vimeo_album(raw: dict, index: int = 0) -> dict | None:
    """Map one Vimeo album (showcase) payload to our module-seed dict.

    `index` is the album's position as returned by the API and seeds the module
    `sort_order` (a default the admin can override). Returns None for a payload
    with no resolvable album id.
    """
    album_id = _album_id_from_uri(raw.get("uri"))
    if not album_id:
        return None
    return {
        "vimeo_album_id": album_id,
        "title": (raw.get("name") or f"Showcase {album_id}")[:255],
        "description": raw.get("description") or None,
        "cover_url": _best_thumbnail(raw.get("pictures")),
        "sort_order": index,
    }


async def _get_all(client: httpx.AsyncClient, path: str, headers: dict) -> list[dict]:
    """Page through a Vimeo collection endpoint, returning all `data` items."""
    out: list[dict] = []
    next_path: str | None = path
    for _ in range(_MAX_PAGES):
        if not next_path:
            break
        resp = await client.get(next_path, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or []
        out.extend(d for d in data if isinstance(d, dict))
        next_path = (body.get("paging") or {}).get("next")
    return out


async def fetch_showcases(token: str) -> ShowcaseFetch:
    """Fetch all showcases and their ordered membership. Raises httpx.HTTPError.

    Reads everything up front (albums + each album's video list) so the caller can
    abort without mutating a single row if any page fails (GK-091 failure policy).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.vimeo.*+json;version=3.4",
    }
    albums: list[dict] = []
    members: dict[str, list[int]] = {}
    async with httpx.AsyncClient(base_url=_VIMEO_API, timeout=30.0) as client:
        raw_albums = await _get_all(
            client, f"/me/albums?per_page={_PER_PAGE}&fields={_ALBUM_FIELDS}", headers
        )
        for i, raw in enumerate(raw_albums):
            parsed = parse_vimeo_album(raw, index=i)
            if parsed is None:
                continue
            albums.append(parsed)
            album_id = parsed["vimeo_album_id"]
            raw_vids = await _get_all(
                client,
                f"/me/albums/{album_id}/videos?per_page={_PER_PAGE}&fields=uri",
                headers,
            )
            members[album_id] = [
                vid
                for vid in (_vimeo_id_from_uri(rv.get("uri")) for rv in raw_vids)
                if vid is not None
            ]
    return ShowcaseFetch(albums=albums, members=members)


def _merge_module(module: ArchiveModule, parsed: dict) -> None:
    """3-way merge of an existing sync-managed module against fresh Vimeo values.

    We only overwrite a field that still equals its last-synced value (i.e. the
    admin hasn't renamed it). The last-synced snapshot is then refreshed to the
    latest Vimeo values so a later admin edit is detected on the next run.
    """
    snapshot = module.vimeo_synced if isinstance(module.vimeo_synced, dict) else None
    for f in _MODULE_MERGE_FIELDS:
        vimeo_value = parsed[f]
        last = snapshot.get(f) if snapshot else None
        current = getattr(module, f)
        if snapshot is None or f not in snapshot or current == last:
            setattr(module, f, vimeo_value)
    # Reassign (not mutate) so SQLAlchemy marks the JSON column dirty.
    module.vimeo_synced = {f: parsed[f] for f in _MODULE_MERGE_FIELDS}


async def upsert_showcase_modules(
    session: AsyncSession, albums: list[dict], *, now: datetime | None = None
) -> ShowcaseSyncResult:
    """Create/update one `ArchiveModule` per showcase, keyed by `vimeo_album_id`.

    Admin-created modules (no `vimeo_album_id`) are never loaded here, so sync can
    never touch them. Re-running is idempotent — modules are upserted in place.
    """
    now = now or utcnow()
    res = ShowcaseSyncResult(ok=True, albums_seen=len(albums))

    modules = (
        await session.execute(
            select(ArchiveModule).where(ArchiveModule.vimeo_album_id.isnot(None))
        )
    ).scalars().all()
    by_album = {m.vimeo_album_id: m for m in modules}

    for parsed in albums:
        album_id = parsed["vimeo_album_id"]
        module = by_album.get(album_id)
        if module is None:
            module = ArchiveModule(
                code=f"vimeo-{album_id}",
                title=parsed["title"],
                description=parsed["description"],
                cover_url=parsed["cover_url"],
                sort_order=parsed["sort_order"],
                is_active=True,
                vimeo_album_id=album_id,
                vimeo_synced={f: parsed[f] for f in _MODULE_MERGE_FIELDS},
                created_at=now,
                updated_at=now,
            )
            session.add(module)
            by_album[album_id] = module
            res.modules_created += 1
        else:
            _merge_module(module, parsed)
            module.updated_at = now
            res.modules_updated += 1

    # Flush so freshly-created modules get ids before membership reconciliation.
    await session.flush()
    return res


async def reconcile_membership(
    session: AsyncSession,
    members: dict[str, list[int]],
    *,
    now: datetime | None = None,
) -> ShowcaseSyncResult:
    """Reconcile `source='vimeo'` memberships against Vimeo showcase membership.

    - Adds a vimeo membership for each video Vimeo lists under a showcase (unless a
      row for that (video, module) pair already exists in any form).
    - Deletes vimeo memberships whose video has left the showcase on Vimeo.
    - NEVER touches `source='admin'` rows, an existing vimeo row's `sort_order`
      (admin reorder), or a `removed_by_admin` tombstone (admin removal) that is
      still backed by the showcase.

    New rows seed `sort_order` from Vimeo's order; existing rows keep theirs.
    """
    now = now or utcnow()
    res = ShowcaseSyncResult(ok=True, albums_seen=len(members))

    modules = (
        await session.execute(
            select(ArchiveModule).where(ArchiveModule.vimeo_album_id.isnot(None))
        )
    ).scalars().all()
    by_album = {m.vimeo_album_id: m for m in modules}

    videos = (await session.execute(select(ArchiveVideo))).scalars().all()
    video_by_vimeo = {v.vimeo_id: v for v in videos}

    existing = (await session.execute(select(ArchiveVideoModule))).scalars().all()
    by_pair = {(m.video_id, m.module_id): m for m in existing}
    vimeo_by_module: dict[int, dict[int, ArchiveVideoModule]] = {}
    for m in existing:
        if m.source == "vimeo":
            vimeo_by_module.setdefault(m.module_id, {})[m.video_id] = m

    for album_id, vimeo_video_ids in members.items():
        module = by_album.get(album_id)
        if module is None:
            continue
        desired: dict[int, int] = {}  # our video.id -> Vimeo order index
        for order, vid in enumerate(vimeo_video_ids):
            v = video_by_vimeo.get(vid)
            if v is None:
                continue  # video not in our catalog yet (next /me/videos sync adds it)
            desired.setdefault(v.id, order)

        for v_id, order in desired.items():
            if (v_id, module.id) in by_pair:
                continue  # pair already linked (vimeo or admin) — unique constraint
            row = ArchiveVideoModule(
                video_id=v_id,
                module_id=module.id,
                source="vimeo",
                sort_order=order,
                removed_by_admin=False,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            by_pair[(v_id, module.id)] = row
            res.memberships_added += 1
            res.affected_video_ids.add(v_id)

        for v_id, row in list(vimeo_by_module.get(module.id, {}).items()):
            if v_id not in desired:
                await session.delete(row)
                res.memberships_removed += 1
                res.affected_video_ids.add(v_id)

    await session.flush()
    return res


async def recompute_primary_module(session: AsyncSession, video_id: int) -> None:
    """Refresh `ArchiveVideo.module_id` to the video's lowest-sorted active module.

    `module_id` is a legacy convenience (GK-091 single-group FK); grouping is now
    M2M. We keep it pointing at a sensible "primary" module for the admin list.
    Called by the showcase sync and the admin membership endpoints.
    """
    primary = (
        await session.execute(
            select(ArchiveModule.id)
            .join(ArchiveVideoModule, ArchiveVideoModule.module_id == ArchiveModule.id)
            .where(
                ArchiveVideoModule.video_id == video_id,
                ArchiveVideoModule.removed_by_admin.is_(False),
                ArchiveModule.is_active.is_(True),
            )
            .order_by(ArchiveModule.sort_order.asc(), ArchiveModule.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    await session.execute(
        update(ArchiveVideo).where(ArchiveVideo.id == video_id).values(module_id=primary)
    )


async def sync_showcases(
    session: AsyncSession,
    *,
    token: str | None = None,
    now: datetime | None = None,
) -> ShowcaseSyncResult:
    """End-to-end showcase sync. Never raises; returns a ShowcaseSyncResult.

    Reads all Vimeo showcase data first; only on a complete read does it mutate
    modules + memberships, so a fetch failure leaves the last good grouping intact.
    """
    token = token if token is not None else settings.vimeo_api_token
    if not token:
        logger.info("vimeo showcase sync skipped: VIMEO_API_TOKEN not set")
        return ShowcaseSyncResult(ok=False, skipped=True, error="no_token")

    try:
        fetch = await fetch_showcases(token)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code if e.response is not None else "?"
        logger.error("vimeo showcase fetch failed: HTTP %s — leaving rows untouched", code)
        return ShowcaseSyncResult(ok=False, error=f"http_{code}")
    except httpx.HTTPError as e:
        logger.error("vimeo showcase fetch failed: %s — leaving rows untouched", e)
        return ShowcaseSyncResult(ok=False, error="network")

    mod_res = await upsert_showcase_modules(session, fetch.albums, now=now)
    mem_res = await reconcile_membership(session, fetch.members, now=now)
    for video_id in mem_res.affected_video_ids:
        await recompute_primary_module(session, video_id)

    logger.info(
        "vimeo showcase sync: %d albums, %d modules created, %d updated, "
        "%d memberships added, %d removed",
        mod_res.albums_seen,
        mod_res.modules_created,
        mod_res.modules_updated,
        mem_res.memberships_added,
        mem_res.memberships_removed,
    )
    return ShowcaseSyncResult(
        ok=True,
        modules_created=mod_res.modules_created,
        modules_updated=mod_res.modules_updated,
        memberships_added=mem_res.memberships_added,
        memberships_removed=mem_res.memberships_removed,
        albums_seen=mod_res.albums_seen,
        affected_video_ids=mem_res.affected_video_ids,
    )


async def sync_archive(
    session: AsyncSession,
    *,
    token: str | None = None,
    now: datetime | None = None,
) -> ArchiveSyncResult:
    """Run both the per-video metadata sync and the showcase grouping sync.

    They are independent: a failure in one does not abort the other (each obeys the
    abort-without-mutating policy on its own fetch). Videos sync first so showcase
    membership can map onto freshly-upserted `archive_videos` rows.
    """
    video_result = await sync_videos(session, token=token, now=now)
    showcase_result = await sync_showcases(session, token=token, now=now)
    return ArchiveSyncResult(videos=video_result, showcases=showcase_result)
