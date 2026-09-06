"""Regression coverage for admin broadcast dispatch."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.routers import broadcasts

NOW = datetime(2026, 6, 24, tzinfo=UTC)


class FakeSession:
    def __init__(self, *, commit_error: Exception | None = None):
        self.added = []
        self.events = []
        self.commit_error = commit_error

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.events.append("flush")
        self.added[0].id = 42
        self.added[0].created_at = NOW

    async def commit(self):
        self.events.append("commit")
        if self.commit_error:
            raise self.commit_error


class FakeBackgroundTasks:
    def __init__(self, db):
        self.db = db
        self.calls = []

    def add_task(self, func, *args, **kwargs):
        assert self.db.events[-1] == "commit"
        self.db.events.append("schedule")
        self.calls.append((func, args, kwargs))


@pytest.mark.asyncio
async def test_create_broadcast_commits_before_scheduling(monkeypatch):
    db = FakeSession()
    bg = FakeBackgroundTasks(db)

    async def fake_audit(_db, **kwargs):
        assert kwargs["action"] == "broadcast.create"
        assert kwargs["target_id"] == 42
        db.events.append("audit")

    monkeypatch.setattr(broadcasts, "audit_record", fake_audit)

    result = await broadcasts.create_broadcast(
        broadcasts.BroadcastIn(title="Launch", message="Hello", segment="active"),
        bg,
        db,
        SimpleNamespace(id=7),
        SimpleNamespace(),
    )

    assert db.events == ["flush", "audit", "commit", "schedule"]
    assert bg.calls == [(broadcasts._run_broadcast, (42,), {})]
    assert result.id == 42
    assert result.status == "draft"
    assert result.segment == "active"
    assert result.created_at == NOW


@pytest.mark.asyncio
async def test_create_broadcast_does_not_schedule_when_commit_fails(monkeypatch):
    db = FakeSession(commit_error=RuntimeError("database unavailable"))
    bg = FakeBackgroundTasks(db)

    async def fake_audit(_db, **_kwargs):
        db.events.append("audit")

    monkeypatch.setattr(broadcasts, "audit_record", fake_audit)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await broadcasts.create_broadcast(
            broadcasts.BroadcastIn(title="Launch", message="Hello"),
            bg,
            db,
            SimpleNamespace(id=7),
            SimpleNamespace(),
        )

    assert db.events == ["flush", "audit", "commit"]
    assert bg.calls == []
