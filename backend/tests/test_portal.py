"""GK-091 member portal: access predicate, magic-link lifecycle, session
management, and Vimeo sync parse/upsert. Fully mocked — no DB/Redis/network.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.api.routers import portal as portal_router
from app.services import portal_auth, subscription, vimeo_sync
from app.services.portal_auth import _hash_token

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# ─── Fake session primitives ────────────────────────────────────────────
class ScalarOne:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class ScalarsList:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class RowsList:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


class RowCount:
    def __init__(self, n):
        self.rowcount = n


class SeqSession:
    """Returns queued execute() results in order; records add()/flush()."""

    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.flushed = False

    async def execute(self, _query):
        return self.results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


class InspectSession(SeqSession):
    def __init__(self, *results):
        super().__init__(*results)
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        return await super().execute(query)


# ─── has_portal_access reuses channel-access predicate ──────────────────
@pytest.mark.asyncio
async def test_has_portal_access_true_when_active_subscription(monkeypatch):
    async def fake_active(_session, _user_id):
        return SimpleNamespace(id=1, status="active")

    monkeypatch.setattr(subscription, "get_active_subscription", fake_active)
    assert await subscription.has_portal_access(object(), 10) is True


@pytest.mark.asyncio
async def test_has_portal_access_false_when_no_subscription(monkeypatch):
    async def fake_active(_session, _user_id):
        return None

    monkeypatch.setattr(subscription, "get_active_subscription", fake_active)
    assert await subscription.has_portal_access(object(), 10) is False


# ─── Magic-link issuance ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_issue_magic_link_returns_token_and_stores_hash(monkeypatch):
    async def yes(_s, _uid):
        return True

    monkeypatch.setattr(portal_auth, "has_portal_access", yes)
    session = SeqSession(ScalarsList([]))  # cap check: no existing links
    user = SimpleNamespace(id=7)

    raw = await portal_auth.issue_magic_link(session, user, now=NOW)

    assert raw is not None
    link = session.added[0]
    assert link.user_id == 7
    assert link.used_at is None
    # Stored as hash only — never the raw token.
    assert link.token_hash == _hash_token(raw)
    assert link.token_hash != raw
    assert link.expires_at == NOW + timedelta(minutes=15)
    assert session.flushed is True


@pytest.mark.asyncio
async def test_issue_magic_link_denied_without_access(monkeypatch):
    async def no(_s, _uid):
        return False

    monkeypatch.setattr(portal_auth, "has_portal_access", no)
    session = SeqSession()
    raw = await portal_auth.issue_magic_link(session, SimpleNamespace(id=7), now=NOW)
    assert raw is None
    assert session.added == []


@pytest.mark.asyncio
async def test_issue_magic_link_caps_active_links(monkeypatch):
    async def yes(_s, _uid):
        return True

    monkeypatch.setattr(portal_auth, "has_portal_access", yes)
    # max_active default 3 → keep at most 2 existing, invalidate the oldest.
    existing = [
        SimpleNamespace(id=i, used_at=None, created_at=NOW - timedelta(minutes=10 - i))
        for i in range(3)
    ]
    session = SeqSession(ScalarsList(existing))
    await portal_auth.issue_magic_link(session, SimpleNamespace(id=7), now=NOW)

    # Oldest (index 0) invalidated so total active stays within the cap.
    assert existing[0].used_at == NOW
    assert existing[1].used_at is None
    assert existing[2].used_at is None


@pytest.mark.asyncio
async def test_repeated_magic_link_requests_each_return_fresh_token(monkeypatch):
    async def yes(_s, _uid):
        return True

    monkeypatch.setattr(portal_auth, "has_portal_access", yes)
    session = SeqSession(ScalarsList([]), ScalarsList([]))
    user = SimpleNamespace(id=7)

    first = await portal_auth.issue_magic_link(session, user, now=NOW)
    second = await portal_auth.issue_magic_link(session, user, now=NOW)

    assert first is not None and second is not None and first != second
    assert len(session.added) == 2
    assert all(link.used_at is None for link in session.added)
    assert all(link.expires_at == NOW + timedelta(minutes=15) for link in session.added)


# ─── Magic-link redemption ──────────────────────────────────────────────
def _link(**over):
    data = {"id": 5, "user_id": 7, "used_at": None, "expires_at": NOW + timedelta(minutes=5)}
    data.update(over)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_redeem_success_opens_session(monkeypatch):
    async def yes(_s, _uid):
        return True

    monkeypatch.setattr(portal_auth, "has_portal_access", yes)
    raw_token = "tok"
    session = SeqSession(
        ScalarOne(_link()),
        RowCount(1),
        ScalarOne(7),  # per-user transaction lock
        ScalarsList([]),  # no existing live sessions
    )

    result = await portal_auth.redeem_magic_link(session, raw_token, now=NOW)

    assert result.ok is True
    assert result.user_id == 7
    assert result.session_token is not None
    # A PortalSession row was created with the session token's hash.
    sess_row = session.added[0]
    assert sess_row.token_hash == _hash_token(result.session_token)
    assert sess_row.user_id == 7


@pytest.mark.asyncio
async def test_redeem_expired_link_rejected(monkeypatch):
    monkeypatch.setattr(portal_auth, "has_portal_access", lambda *a: True)
    session = SeqSession(ScalarOne(_link(expires_at=NOW - timedelta(minutes=1))))
    result = await portal_auth.redeem_magic_link(session, "tok", now=NOW)
    assert result.ok is False
    assert result.reason == "invalid"
    assert session.added == []


@pytest.mark.asyncio
async def test_redeem_used_link_rejected(monkeypatch):
    monkeypatch.setattr(portal_auth, "has_portal_access", lambda *a: True)
    session = SeqSession(ScalarOne(_link(used_at=NOW - timedelta(minutes=1))))
    result = await portal_auth.redeem_magic_link(session, "tok", now=NOW)
    assert result.ok is False
    assert result.reason == "invalid"


@pytest.mark.asyncio
async def test_redeem_second_click_loses_atomic_claim(monkeypatch):
    async def yes(_s, _uid):
        return True

    monkeypatch.setattr(portal_auth, "has_portal_access", yes)
    # Link looked unused on read, but the atomic UPDATE claimed 0 rows (a racing
    # first click already consumed it) → treated as invalid.
    session = SeqSession(ScalarOne(_link()), RowCount(0))
    result = await portal_auth.redeem_magic_link(session, "tok", now=NOW)
    assert result.ok is False
    assert result.reason == "invalid"
    assert session.added == []


@pytest.mark.asyncio
async def test_redeem_rejected_when_access_lost_after_issue(monkeypatch):
    async def no(_s, _uid):
        return False

    monkeypatch.setattr(portal_auth, "has_portal_access", no)
    session = SeqSession(ScalarOne(_link()), RowCount(1))
    result = await portal_auth.redeem_magic_link(session, "tok", now=NOW)
    assert result.ok is False
    assert result.reason == "no_access"
    assert session.added == []


# ─── Session load / revoke ──────────────────────────────────────────────
def _session_row(**over):
    data = {
        "id": 1,
        "user_id": 7,
        "created_at": NOW - timedelta(days=1),
        "revoked_at": None,
        "expires_at": NOW + timedelta(days=20),
        "last_seen_at": NOW - timedelta(days=1),
    }
    data.update(over)
    return SimpleNamespace(**data)


@pytest.mark.asyncio
async def test_create_fourth_session_revokes_oldest_live_session():
    active = [
        _session_row(id=1, created_at=NOW - timedelta(days=3)),
        _session_row(id=2, created_at=NOW - timedelta(days=2)),
        _session_row(id=3, created_at=NOW - timedelta(days=1)),
    ]
    session = SeqSession(ScalarOne(7), ScalarsList(active))

    raw, expires_at = await portal_auth.create_session(session, 7, now=NOW)

    assert active[0].revoked_at == NOW
    assert active[1].revoked_at is None
    assert active[2].revoked_at is None
    assert session.added[0].token_hash == _hash_token(raw)
    assert expires_at == NOW + timedelta(days=30)


@pytest.mark.asyncio
async def test_session_cap_is_configurable(monkeypatch):
    monkeypatch.setattr(portal_auth.settings, "portal_session_max_active", 2)
    active = [
        _session_row(id=1, created_at=NOW - timedelta(days=2)),
        _session_row(id=2, created_at=NOW - timedelta(days=1)),
    ]
    session = SeqSession(ScalarOne(7), ScalarsList(active))

    await portal_auth.create_session(session, 7, now=NOW)

    assert active[0].revoked_at == NOW
    assert active[1].revoked_at is None


@pytest.mark.asyncio
async def test_session_creation_locks_account_and_only_counts_live_rows():
    live = _session_row(id=3)
    session = InspectSession(ScalarOne(7), ScalarsList([live]))

    await portal_auth.create_session(session, 7, now=NOW)

    user_lock_sql = str(session.queries[0].compile()).upper()
    live_rows_sql = str(session.queries[1].compile()).upper()
    assert "FROM USERS" in user_lock_sql and "FOR UPDATE" in user_lock_sql
    assert "FROM PORTAL_SESSIONS" in live_rows_sql and "FOR UPDATE" in live_rows_sql
    assert "PORTAL_SESSIONS.REVOKED_AT IS NULL" in live_rows_sql
    assert "PORTAL_SESSIONS.EXPIRES_AT >" in live_rows_sql
    # One existing live row plus the new one is under the default cap. Expired
    # and revoked rows are excluded by the DB predicates above, so neither can
    # cause an unnecessary eviction.
    assert live.revoked_at is None


@pytest.mark.asyncio
async def test_load_session_valid_updates_last_seen():
    row = _session_row()
    session = SeqSession(ScalarOne(row))
    out = await portal_auth.load_session(session, "tok", ip="1.2.3.4", now=NOW)
    assert out is row
    assert row.last_seen_at == NOW
    assert row.ip_last_seen == "1.2.3.4"
    # 20 days remaining > 7-day slide threshold → not extended.
    assert row.expires_at == NOW + timedelta(days=20)


@pytest.mark.asyncio
async def test_load_session_slides_when_near_expiry():
    row = _session_row(expires_at=NOW + timedelta(days=3))
    session = SeqSession(ScalarOne(row))
    out = await portal_auth.load_session(session, "tok", now=NOW)
    assert out is row
    assert row.expires_at == NOW + timedelta(days=30)


@pytest.mark.asyncio
async def test_load_session_revoked_or_expired_returns_none():
    revoked = SeqSession(ScalarOne(_session_row(revoked_at=NOW)))
    assert await portal_auth.load_session(revoked, "tok", now=NOW) is None

    expired = SeqSession(ScalarOne(_session_row(expires_at=NOW - timedelta(minutes=1))))
    assert await portal_auth.load_session(expired, "tok", now=NOW) is None


@pytest.mark.asyncio
async def test_load_session_no_cookie_returns_none():
    assert await portal_auth.load_session(SeqSession(), None) is None


@pytest.mark.asyncio
async def test_revoke_session_reports_success():
    session = SeqSession(RowCount(1))
    assert await portal_auth.revoke_session(session, "tok", now=NOW) is True


# ─── Vimeo parse ────────────────────────────────────────────────────────
SAMPLE = {
    "uri": "/videos/987654321",
    "name": "Практика заземления",
    "description": "30-минутная практика",
    "duration": 1830,
    "pictures": {
        "base_link": "https://i.vimeocdn.com/video/base",
        "sizes": [
            {"width": 200, "link": "https://i.vimeocdn.com/video/small.jpg"},
            {"width": 1280, "link": "https://i.vimeocdn.com/video/large.jpg"},
        ],
    },
    "privacy": {"view": "disable"},
    "player_embed_url": "https://player.vimeo.com/video/987654321",
}


def test_parse_vimeo_video_extracts_fields():
    parsed = vimeo_sync.parse_vimeo_video(SAMPLE)
    assert parsed is not None
    assert parsed["vimeo_id"] == 987654321
    assert parsed["title"] == "Практика заземления"
    assert parsed["duration_seconds"] == 1830
    assert parsed["thumbnail_url"] == "https://i.vimeocdn.com/video/large.jpg"
    assert parsed["vimeo_privacy"] == "disable"
    assert parsed["player_embed_url"] == "https://player.vimeo.com/video/987654321"


def test_parse_vimeo_video_falls_back_embed_url():
    parsed = vimeo_sync.parse_vimeo_video({"uri": "/videos/42", "name": "x"})
    assert parsed["player_embed_url"] == "https://player.vimeo.com/video/42"


def test_parse_vimeo_video_none_without_id():
    assert vimeo_sync.parse_vimeo_video({"name": "no uri"}) is None


# ─── Vimeo upsert ───────────────────────────────────────────────────────
class UpsertSession:
    def __init__(self, existing):
        self._existing = existing
        self.added = []
        self.flushed = False

    async def execute(self, _query):
        return ScalarsList(self._existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_upsert_creates_new_videos():
    session = UpsertSession(existing=[])
    parsed = [vimeo_sync.parse_vimeo_video(SAMPLE)]
    result = await vimeo_sync.upsert_videos(session, parsed, now=NOW)
    assert result.created == 1
    assert result.updated == 0
    assert session.added[0].vimeo_id == 987654321
    assert session.flushed is True


@pytest.mark.asyncio
async def test_upsert_idempotent_preserves_curation():
    # Existing row already curated into module 3, hidden, custom sort.
    existing = SimpleNamespace(
        vimeo_id=987654321,
        title="old",
        description=None,
        duration_seconds=None,
        thumbnail_url=None,
        vimeo_privacy=None,
        player_embed_url=None,
        module_id=3,
        sort_order=99,
        visibility="visible",
        synced_at=None,
    )
    session = UpsertSession(existing=[existing])
    parsed = [vimeo_sync.parse_vimeo_video(SAMPLE)]
    result = await vimeo_sync.upsert_videos(session, parsed, now=NOW)

    assert result.created == 0
    assert result.updated == 1
    # Metadata refreshed…
    assert existing.title == "Практика заземления"
    assert existing.synced_at == NOW
    # …but admin curation untouched.
    assert existing.module_id == 3
    assert existing.sort_order == 99
    assert existing.visibility == "visible"
    assert session.added == []


@pytest.mark.asyncio
async def test_upsert_marks_missing_video_hidden():
    gone = SimpleNamespace(vimeo_id=111, visibility="visible", synced_at=None)
    session = UpsertSession(existing=[gone])
    parsed = [vimeo_sync.parse_vimeo_video(SAMPLE)]  # 987654321, not 111
    result = await vimeo_sync.upsert_videos(session, parsed, now=NOW)

    assert result.created == 1  # the sample video
    assert result.hidden == 1
    assert gone.visibility == "hidden"


@pytest.mark.asyncio
async def test_upsert_flags_public_video_warning():
    public = dict(SAMPLE, privacy={"view": "anybody"})
    session = UpsertSession(existing=[])
    result = await vimeo_sync.upsert_videos(
        session, [vimeo_sync.parse_vimeo_video(public)], now=NOW
    )
    assert any("anybody" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_sync_videos_skips_without_token():
    result = await vimeo_sync.sync_videos(SeqSession(), token="", now=NOW)
    assert result.ok is False
    assert result.skipped is True
    assert result.error == "no_token"


# ─── GK-092: Vimeo showcases → modules + M2M membership ──────────────────
class ShowcaseSession:
    """Returns queued execute() results in order; records add/delete/flush."""

    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.deleted = []
        self.flushed = False

    async def execute(self, _query):
        return self.results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flushed = True


def _album(
    uri="/me/albums/100",
    name="Продажи",
    description=None,
    cover_url=None,
    index=0,
):
    raw = {"uri": uri, "name": name, "description": description}
    if cover_url:
        raw["pictures"] = {"sizes": [{"link": cover_url}]}
    return vimeo_sync.parse_vimeo_album(raw, index=index)


# ─── Album parse ────────────────────────────────────────────────────────
def test_parse_vimeo_album_extracts_fields():
    parsed = vimeo_sync.parse_vimeo_album(
        {
            "uri": "/me/albums/123456",
            "name": "Тренинг в Нью-Йорке",
            "description": "опис",
            "pictures": {
                "sizes": [
                    {"link": "https://i.vimeocdn.com/album/small.jpg"},
                    {"link": "https://i.vimeocdn.com/album/large.jpg"},
                ]
            },
        },
        index=3,
    )
    assert parsed["vimeo_album_id"] == "123456"
    assert parsed["title"] == "Тренинг в Нью-Йорке"
    assert parsed["description"] == "опис"
    assert parsed["cover_url"] == "https://i.vimeocdn.com/album/large.jpg"
    assert parsed["sort_order"] == 3


def test_parse_vimeo_album_handles_user_scoped_uri():
    parsed = vimeo_sync.parse_vimeo_album({"uri": "/users/9/albums/777", "name": "x"})
    assert parsed["vimeo_album_id"] == "777"


def test_parse_vimeo_album_none_without_uri():
    assert vimeo_sync.parse_vimeo_album({"name": "no uri"}) is None


# ─── Module upsert (create + idempotent + 3-way merge) ──────────────────
@pytest.mark.asyncio
async def test_upsert_showcase_modules_creates_module():
    session = ShowcaseSession(ScalarsList([]))  # no existing sync-managed modules
    res = await vimeo_sync.upsert_showcase_modules(session, [_album()], now=NOW)
    assert res.modules_created == 1
    assert res.modules_updated == 0
    m = session.added[0]
    assert m.vimeo_album_id == "100"
    assert m.code == "vimeo-100"
    assert m.title == "Продажи"
    assert m.cover_url is None
    assert m.vimeo_synced == {
        "title": "Продажи",
        "description": None,
        "cover_url": None,
    }
    assert session.flushed is True


@pytest.mark.asyncio
async def test_upsert_showcase_modules_idempotent_no_dupes():
    existing = SimpleNamespace(
        id=5,
        vimeo_album_id="100",
        code="vimeo-100",
        title="Продажи",
        description=None,
        cover_url=None,
        sort_order=0,
        is_active=True,
        vimeo_synced={"title": "Продажи", "description": None, "cover_url": None},
        updated_at=None,
    )
    session = ShowcaseSession(ScalarsList([existing]))
    res = await vimeo_sync.upsert_showcase_modules(session, [_album()], now=NOW)
    assert res.modules_created == 0
    assert res.modules_updated == 1
    assert session.added == []  # upsert in place, never a duplicate


@pytest.mark.asyncio
async def test_upsert_showcase_modules_preserves_admin_rename():
    # Last sync stored title "Продажи"; the admin then renamed it.
    existing = SimpleNamespace(
        id=5,
        vimeo_album_id="100",
        code="vimeo-100",
        title="Продажи 2024",  # admin's rename
        description="старое описание",  # unedited (still equals snapshot)
        cover_url="https://admin.example/custom.jpg",  # admin override
        sort_order=0,
        is_active=True,
        vimeo_synced={
            "title": "Продажи",
            "description": "старое описание",
            "cover_url": "https://vimeo.example/old.jpg",
        },
        updated_at=None,
    )
    session = ShowcaseSession(ScalarsList([existing]))
    album = _album(
        name="Продажи NEW",
        description="новое описание",
        cover_url="https://vimeo.example/new.jpg",
    )
    await vimeo_sync.upsert_showcase_modules(session, [album], now=NOW)
    # Admin's title survives; the unedited description follows Vimeo.
    assert existing.title == "Продажи 2024"
    assert existing.description == "новое описание"
    assert existing.cover_url == "https://admin.example/custom.jpg"
    # Snapshot refreshed to the latest Vimeo values for the next run's comparison.
    assert existing.vimeo_synced == {
        "title": "Продажи NEW",
        "description": "новое описание",
        "cover_url": "https://vimeo.example/new.jpg",
    }


# ─── GK-383: showcase cover precedence ───────────────────────────────────
def _portal_module(module_id, *, cover_url):
    return SimpleNamespace(
        id=module_id,
        code=f"module-{module_id}",
        title=f"Module {module_id}",
        description=None,
        cover_url=cover_url,
        sort_order=module_id,
        is_active=True,
    )


def _portal_video(video_id, *, thumbnail_url):
    return SimpleNamespace(
        id=video_id,
        vimeo_id=10_000 + video_id,
        title=f"Video {video_id}",
        description=None,
        duration_seconds=60,
        thumbnail_url=thumbnail_url,
        module_id=None,
        sort_order=0,
    )


@pytest.mark.asyncio
async def test_list_module_cards_prefers_showcase_cover_with_video_fallback():
    modules = [
        _portal_module(1, cover_url="https://vimeo.example/showcase.jpg"),
        _portal_module(2, cover_url=None),
    ]
    session = SeqSession(
        ScalarsList(modules),
        RowsList(
            [
                (1, 11, "https://vimeo.example/lesson-1.jpg"),
                (2, 22, "https://vimeo.example/lesson-2.jpg"),
            ]
        ),
        RowsList(
            [
                (11, "https://vimeo.example/lesson-1.jpg"),
                (22, "https://vimeo.example/lesson-2.jpg"),
            ]
        ),
    )

    response = await portal_router.list_module_cards(SimpleNamespace(), session)

    assert response.modules[0].cover_url == "https://vimeo.example/showcase.jpg"
    assert response.modules[1].cover_url == "https://vimeo.example/lesson-2.jpg"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_cover", "expected"),
    [
        ("https://vimeo.example/showcase.jpg", "https://vimeo.example/showcase.jpg"),
        (None, "https://vimeo.example/lesson.jpg"),
    ],
)
async def test_module_card_videos_prefers_showcase_cover_with_video_fallback(
    module_cover, expected
):
    module = _portal_module(1, cover_url=module_cover)
    video = _portal_video(11, thumbnail_url="https://vimeo.example/lesson.jpg")
    session = SeqSession(ScalarOne(module), RowsList([(video, 0)]))

    response = await portal_router.module_card_videos(
        module.id, SimpleNamespace(), session
    )

    assert response.module.cover_url == expected


# ─── Membership reconciliation ──────────────────────────────────────────
def _mod(id, album_id):
    return SimpleNamespace(id=id, vimeo_album_id=album_id)


def _vid(id, vimeo_id):
    return SimpleNamespace(id=id, vimeo_id=vimeo_id)


def _mem(id, video_id, module_id, source="vimeo", removed=False, sort_order=0):
    return SimpleNamespace(
        id=id,
        video_id=video_id,
        module_id=module_id,
        source=source,
        removed_by_admin=removed,
        sort_order=sort_order,
    )


@pytest.mark.asyncio
async def test_reconcile_membership_places_videos_in_vimeo_order():
    session = ShowcaseSession(
        ScalarsList([_mod(10, "100")]),       # modules
        ScalarsList([_vid(1, 111), _vid(2, 222)]),  # videos
        ScalarsList([]),                       # memberships
    )
    res = await vimeo_sync.reconcile_membership(session, {"100": [222, 111]}, now=NOW)
    assert res.memberships_added == 2
    added = sorted(session.added, key=lambda r: r.sort_order)
    assert (added[0].video_id, added[0].sort_order) == (2, 0)  # 222 first in Vimeo
    assert (added[1].video_id, added[1].sort_order) == (1, 1)
    assert all(r.source == "vimeo" for r in session.added)


@pytest.mark.asyncio
async def test_reconcile_membership_multi_showcase():
    session = ShowcaseSession(
        ScalarsList([_mod(10, "100"), _mod(20, "200")]),
        ScalarsList([_vid(1, 111)]),
        ScalarsList([]),
    )
    res = await vimeo_sync.reconcile_membership(session, {"100": [111], "200": [111]}, now=NOW)
    # The single video lands under BOTH modules (M2M — confirmed).
    assert res.memberships_added == 2
    assert sorted(r.module_id for r in session.added) == [10, 20]


@pytest.mark.asyncio
async def test_reconcile_membership_keeps_admin_membership():
    admin_row = _mem(99, video_id=1, module_id=10, source="admin")
    session = ShowcaseSession(
        ScalarsList([_mod(10, "100")]),
        ScalarsList([_vid(1, 111)]),
        ScalarsList([admin_row]),
    )
    # Showcase no longer lists the video, but the admin added it manually.
    res = await vimeo_sync.reconcile_membership(session, {"100": []}, now=NOW)
    assert res.memberships_added == 0
    assert res.memberships_removed == 0
    assert session.deleted == []  # admin membership untouched


@pytest.mark.asyncio
async def test_reconcile_membership_respects_admin_tombstone():
    tomb = _mem(99, video_id=1, module_id=10, source="vimeo", removed=True)
    session = ShowcaseSession(
        ScalarsList([_mod(10, "100")]),
        ScalarsList([_vid(1, 111)]),
        ScalarsList([tomb]),
    )
    # Video is still in the showcase, but the admin removed it in our portal.
    res = await vimeo_sync.reconcile_membership(session, {"100": [111]}, now=NOW)
    assert res.memberships_added == 0  # pair exists → no duplicate
    assert res.memberships_removed == 0  # still in showcase → not deleted
    assert session.added == []
    assert session.deleted == []
    assert tomb.removed_by_admin is True  # tombstone preserved across sync


@pytest.mark.asyncio
async def test_reconcile_membership_removes_when_video_leaves_showcase():
    row = _mem(99, video_id=1, module_id=10, source="vimeo")
    session = ShowcaseSession(
        ScalarsList([_mod(10, "100")]),
        ScalarsList([_vid(1, 111)]),
        ScalarsList([row]),
    )
    res = await vimeo_sync.reconcile_membership(session, {"100": []}, now=NOW)
    assert res.memberships_removed == 1
    assert session.deleted == [row]


# ─── Orchestration / failure policy ─────────────────────────────────────
@pytest.mark.asyncio
async def test_sync_showcases_skips_without_token():
    res = await vimeo_sync.sync_showcases(ShowcaseSession(), token="", now=NOW)
    assert res.ok is False
    assert res.skipped is True
    assert res.error == "no_token"


@pytest.mark.asyncio
async def test_sync_archive_skips_both_without_token():
    res = await vimeo_sync.sync_archive(SeqSession(), token="", now=NOW)
    assert res.videos.skipped is True
    assert res.showcases.skipped is True


@pytest.mark.asyncio
async def test_sync_showcases_aborts_without_mutating_on_network_error(monkeypatch):
    async def boom(_token):
        raise httpx.ConnectError("vimeo down")

    monkeypatch.setattr(vimeo_sync, "fetch_showcases", boom)
    session = ShowcaseSession()  # any DB access would IndexError on the empty queue
    res = await vimeo_sync.sync_showcases(session, token="tok", now=NOW)
    assert res.ok is False
    assert res.error == "network"
    assert session.added == [] and session.deleted == [] and session.flushed is False


@pytest.mark.asyncio
async def test_sync_showcases_aborts_without_mutating_on_http_error(monkeypatch):
    async def boom(_token):
        raise httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://api.vimeo.com/me/albums"),
            response=httpx.Response(429),
        )

    monkeypatch.setattr(vimeo_sync, "fetch_showcases", boom)
    session = ShowcaseSession()
    res = await vimeo_sync.sync_showcases(session, token="tok", now=NOW)
    assert res.ok is False
    assert res.error == "http_429"
    assert session.flushed is False
