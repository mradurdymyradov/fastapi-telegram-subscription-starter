"""Regression coverage for GK-454's concurrent first-update race."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RaceLosingSession:
    """Initial SELECT misses; re-SELECT returns the concurrent winner's row."""

    def __init__(self, winner):
        self._results = [_ScalarResult(None), SimpleNamespace(), _ScalarResult(winner)]
        self.queries = []
        self.flushed = False

    async def execute(self, query):
        self.queries.append(query)
        return self._results.pop(0)

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_concurrent_first_update_reselects_winner_instead_of_failing(monkeypatch):
    from app.bot.middlewares import user as user_middleware

    winner = SimpleNamespace(
        id=17,
        tg_id=9001,
        username="new-member",
        first_name="New",
        is_banned=False,
    )
    tg_user = SimpleNamespace(
        id=9001,
        username="new-member",
        first_name="New",
        last_name=None,
        language_code="ru",
    )
    session = _RaceLosingSession(winner)
    data = {"event_from_user": tg_user, "session": session}
    handler = AsyncMock(return_value="dispatched")
    monkeypatch.setattr(
        user_middleware,
        "ensure_unique_code",
        AsyncMock(return_value="RACE0001"),
    )

    result = await user_middleware.UserMiddleware()(handler, SimpleNamespace(), data)

    assert result == "dispatched"
    assert data["user"] is winner
    handler.assert_awaited_once()
    assert session.flushed is False

    assert len(session.queries) == 3
    insert_sql = str(
        session.queries[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "ON CONFLICT (tg_id) DO NOTHING" in insert_sql
