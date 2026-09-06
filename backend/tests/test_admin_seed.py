"""GK-442: the seed-admin path must not make its own credentials unremovable.

Two mechanisms, each sensible alone, composed into a deadlock: `_bootstrap()`
recreated the `ADMIN_DEFAULT_*` owner unconditionally at every API start, and
GK-436's config guard listed both keys as always-required and refused to boot
without them. So the account could not be deleted while the keys were in `.env`,
and the keys could not be removed without the process failing to start.
Demonstrated on 11.08: deleting `admin@example.com` succeeded, and would
have been undone by the next restart.

These pin the four things that make the switch safe: off means nothing is
created, off means a deleted account stays deleted, on still behaves as before,
and an empty `admin_users` is never a silent lockout.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.api import main as api_main
from app.config import _DEFAULT_ADMIN_PASSWORD, Settings
from app.db.models import AdminUser


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Answers the two shapes `_seed_default_admin` asks for."""

    def __init__(self, *, existing=None):
        self.existing = existing
        self.added = []
        self.queries = 0

    async def execute(self, _query):
        self.queries += 1
        return FakeResult(self.existing)

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def settings_with(monkeypatch):
    def _apply(**overrides):
        values = {
            "seed_default_admin": False,
            "admin_default_email": "seed@example.test",
            "admin_default_password": "a-real-24-character-pw",
            "allow_default_admin_password": False,
        }
        values.update(overrides)
        monkeypatch.setattr(api_main, "settings", SimpleNamespace(**values))

    return _apply


@pytest.mark.asyncio
async def test_with_seeding_off_a_deleted_admin_stays_deleted(settings_with):
    """The 11.08 case: the account was gone, and the next start put it back."""
    settings_with(seed_default_admin=False)
    session = FakeSession(existing=7)  # some other admin exists

    await api_main._seed_default_admin(session)

    assert session.added == []


@pytest.mark.asyncio
async def test_with_seeding_off_and_no_admins_at_all_it_says_so_loudly(
    settings_with, caplog
):
    settings_with(seed_default_admin=False)
    session = FakeSession(existing=None)

    with caplog.at_level(logging.ERROR, logger=api_main.logger.name):
        await api_main._seed_default_admin(session)

    assert session.added == []
    assert caplog.records, "an unreachable panel must not be a silent lockout"
    message = caplog.records[0].getMessage()
    assert "SEED_DEFAULT_ADMIN" in message
    # The way out has to be in the message; nobody locked out reads a runbook first.
    assert "password_hash=hash_password" in message


@pytest.mark.asyncio
async def test_with_seeding_on_a_missing_owner_is_created(settings_with):
    settings_with(seed_default_admin=True)
    session = FakeSession(existing=None)

    await api_main._seed_default_admin(session)

    assert len(session.added) == 1
    created = session.added[0]
    assert isinstance(created, AdminUser)
    assert created.email == "seed@example.test"
    assert created.role == "owner"
    assert created.password_hash != "a-real-24-character-pw"


@pytest.mark.asyncio
async def test_with_seeding_on_an_existing_owner_is_left_alone(settings_with):
    settings_with(seed_default_admin=True)
    session = FakeSession(existing=SimpleNamespace(id=1, email="seed@example.test"))

    await api_main._seed_default_admin(session)

    assert session.added == []


@pytest.mark.asyncio
async def test_the_demo_password_is_still_refused_when_seeding_is_on(settings_with, caplog):
    settings_with(
        seed_default_admin=True,
        admin_default_password="demo1234",
        allow_default_admin_password=False,
    )
    session = FakeSession(existing=None)

    with caplog.at_level(logging.ERROR, logger=api_main.logger.name):
        await api_main._seed_default_admin(session)

    assert session.added == []
    assert "demo password" in caplog.records[0].getMessage()


# ---------------------------------------------------------------------------
# The startup gate, not the seed function.
#
# The tests above monkeypatch `api_main.settings` with a SimpleNamespace, so
# they exercise `_seed_default_admin` and never `_bootstrap`'s call to
# `validate_security()`. That gap hid the other half of the deadlock: taking
# `ADMIN_DEFAULT_PASSWORD` out of the environment — which is the whole point of
# the flag, and what GK-429 asks an operator to do — left pydantic supplying the
# demo default, and `validate_security` failed on it unconditionally. In prod
# that is a `RuntimeError` at startup, so `api` and `bot` would not boot at all.
# Found 2026-08-16 by booting the API against a launch-shaped env; these use a
# real `Settings` so the next change to that check has to answer for it.
# ---------------------------------------------------------------------------


def _prod_settings(**overrides):
    values = {
        "_env_file": None,
        "app_env": "prod",
        "jwt_secret": "x" * 32,
        "private_channel_id": "-1001",
        "practice_chat_id": "-1002",
        # Passed explicitly rather than left to the field default: the test
        # process inherits a real `ADMIN_DEFAULT_PASSWORD` from `.env`, which
        # would silently stand in for the value an absent key falls back to and
        # make these assertions test nothing.
        "admin_default_password": _DEFAULT_ADMIN_PASSWORD,
    }
    values.update(overrides)
    return Settings(**values)


def _password_errors(settings) -> list[str]:
    return [e for e in settings.validate_security() if "ADMIN_DEFAULT_PASSWORD" in e]


def test_absent_admin_password_does_not_block_startup_when_seeding_is_off():
    """The launch shape: both ADMIN_DEFAULT_* keys gone from the environment."""
    assert _password_errors(_prod_settings(seed_default_admin=False)) == []


def test_demo_admin_password_still_blocks_startup_when_seeding_is_on():
    """Turning seeding on re-arms the check — the protection is not lost."""
    errors = _password_errors(
        _prod_settings(seed_default_admin=True, allow_default_admin_password=False)
    )
    assert len(errors) == 1
    assert "SEED_DEFAULT_ADMIN is on" in errors[0]


def test_seeding_on_with_a_strong_password_is_accepted():
    assert (
        _password_errors(
            _prod_settings(seed_default_admin=True, admin_default_password="a-real-24-character-pw")
        )
        == []
    )


def test_seeding_on_with_the_demo_password_is_allowed_only_by_explicit_opt_in():
    assert (
        _password_errors(
            _prod_settings(seed_default_admin=True, allow_default_admin_password=True)
        )
        == []
    )
