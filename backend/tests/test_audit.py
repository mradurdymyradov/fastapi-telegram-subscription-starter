"""Coverage for the moderation-journal audit view (GK-386).

Fully mocked — exercises the router functions directly with a fake session,
no DB/Redis. Asserts actor-name resolution, the filter wiring, and the
actors lookup mapping.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.routers import audit

NOW = datetime(2026, 6, 24, tzinfo=UTC)
ADMIN = SimpleNamespace(id=1)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    """Captures the statement and returns canned rows from `.all()`."""

    def __init__(self, rows):
        self.rows = rows
        self.last_stmt = None

    async def execute(self, stmt):
        self.last_stmt = stmt
        return FakeResult(self.rows)


async def call_list(db, **kw):
    """Invoke the endpoint with real defaults for its Query(...) params.

    Calling a FastAPI route function directly bypasses dependency resolution, so
    the unbound `Query(...)` defaults would otherwise leak through as FieldInfo
    objects. Passing concrete values mirrors what FastAPI injects at runtime.
    """
    params = dict(action=None, actor_admin_id=None, category=None, limit=200)
    params.update(kw)
    return await audit.list_audit(db, ADMIN, **params)


def make_log(**kw):
    base = dict(
        id=1,
        actor_admin_id=5,
        actor_ip="1.2.3.4",
        action="payment.approve",
        target_type="payment",
        target_id="42",
        details={"amount": 990.0},
        created_at=NOW,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_list_audit_resolves_actor_label():
    rows = [
        (make_log(), "curator@example.com"),
        (make_log(id=2, actor_admin_id=None, action="broadcast.create"), None),
    ]
    db = FakeSession(rows)

    out = await call_list(db)

    assert out[0].actor_admin_id == 5
    assert out[0].actor_label == "curator@example.com"
    assert out[0].action == "payment.approve"
    assert out[0].target_type == "payment"
    assert out[0].target_id == "42"
    # System / automatic entry (no admin) keeps a null label, not "admin #None".
    assert out[1].actor_admin_id is None
    assert out[1].actor_label is None


@pytest.mark.asyncio
async def test_list_audit_no_filters_has_no_where():
    db = FakeSession([])
    await call_list(db)
    assert db.last_stmt.whereclause is None


@pytest.mark.asyncio
async def test_list_audit_moderation_category_filters():
    db = FakeSession([])
    await call_list(db, category="moderation")
    assert db.last_stmt.whereclause is not None


@pytest.mark.asyncio
async def test_list_audit_actor_filter():
    db = FakeSession([])
    await call_list(db, actor_admin_id=5)
    assert db.last_stmt.whereclause is not None


@pytest.mark.asyncio
async def test_list_audit_action_filter():
    db = FakeSession([])
    await call_list(db, action="support.reply")
    assert db.last_stmt.whereclause is not None


@pytest.mark.asyncio
async def test_list_actors_maps_rows():
    db = FakeSession([(5, "a@example.com"), (6, "b@example.com")])

    out = await audit.list_actors(db, ADMIN)

    assert [r.actor_admin_id for r in out] == [5, 6]
    assert out[0].email == "a@example.com"
    assert out[1].email == "b@example.com"


def test_moderation_action_set_is_curated():
    # The journal's default view must include payment + refund + support
    # moderation, and must not accidentally pull in technical admin actions.
    assert "payment.approve" in audit.MODERATION_ACTIONS
    assert "payment.refund.resolve" in audit.MODERATION_ACTIONS
    assert "support.reply" in audit.MODERATION_ACTIONS
    assert "admin.totp.enable" not in audit.MODERATION_ACTIONS
    assert "broadcast.create" not in audit.MODERATION_ACTIONS
