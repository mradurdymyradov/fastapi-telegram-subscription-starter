"""GK-405: admin JWTs carry an account epoch (`ver`) so a credential change or an
explicit "revoke all sessions" invalidates every previously issued token before it
would otherwise expire.

These tests drive the real `create_access_token` / `decode_token` /
`current_admin` code paths (only Redis jti-revocation is stubbed out), so they
exercise the actual sign-and-compare of the epoch claim end to end.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import deps
from app.api.routers import auth as auth_router
from app.api.routers.auth import ChangePasswordIn, LoginIn, TotpDisableIn
from app.services.security import create_access_token
from app.services.totp import generate_totp_secret


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDB:
    def __init__(self, admin):
        self.admin = admin
        self.added = []

    async def execute(self, _query):
        return ScalarResult(self.admin)

    def add(self, obj):
        self.added.append(obj)


def make_admin(**overrides):
    data = {
        "id": 1,
        "email": "admin@example.com",
        "password_hash": "hash:correct-password",
        "role": "owner",
        "is_active": True,
        "token_version": 0,
        "totp_enabled": False,
        "totp_secret": None,
        "totp_confirmed_at": None,
        "totp_disabled_at": None,
        "totp_last_counter": None,
        "totp_recovery_hashes": [],
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_request():
    return SimpleNamespace(
        state=SimpleNamespace(),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def issue(admin):
    """Mint a real admin JWT stamped with the admin's current epoch."""
    token, _jti, _ttl = create_access_token(
        str(admin.id),
        {"email": admin.email, "role": admin.role, "ver": admin.token_version},
    )
    return token


async def resolve(admin, token):
    """Run the real current_admin dependency against a bearer token."""
    return await deps.current_admin(FakeDB(admin), make_request(), f"Bearer {token}")


@pytest.fixture(autouse=True)
def no_redis_revocation(monkeypatch):
    async def not_revoked(_jti):
        return False

    monkeypatch.setattr(deps, "is_token_revoked", not_revoked)


@pytest.fixture()
def fast_auth(monkeypatch):
    async def no_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(auth_router.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(auth_router, "verify_password", lambda raw, hashed: hashed == f"hash:{raw}")
    monkeypatch.setattr(auth_router, "hash_password", lambda raw: f"hash:{raw}")


@pytest.mark.asyncio
async def test_change_password_invalidates_all_prior_tokens(fast_auth):
    admin = make_admin()
    tok1 = issue(admin)
    tok2 = issue(admin)

    # Both sessions authenticate before the password change.
    assert await resolve(admin, tok1) is admin
    assert await resolve(admin, tok2) is admin

    await auth_router.change_password(
        ChangePasswordIn(current_password="correct-password", new_password="brand-new-passphrase"),
        FakeDB(admin),
        admin,
    )
    assert admin.token_version == 1

    # Every pre-change token is now 401 — including the one that made the call.
    for tok in (tok1, tok2):
        with pytest.raises(HTTPException) as exc:
            await resolve(admin, tok)
        assert exc.value.status_code == 401

    # A token minted at the new epoch works.
    assert await resolve(admin, issue(admin)) is admin


@pytest.mark.asyncio
async def test_totp_disable_invalidates_prior_tokens(fast_auth, monkeypatch):
    monkeypatch.setattr(auth_router, "_verify_totp_or_recovery", lambda _admin, _code: ("totp", 7))
    admin = make_admin(totp_enabled=True, totp_secret=generate_totp_secret())
    tok = issue(admin)
    assert await resolve(admin, tok) is admin

    await auth_router.totp_disable(
        TotpDisableIn(current_password="correct-password", code="123456"),
        FakeDB(admin),
        admin,
        make_request(),
    )
    assert admin.token_version == 1
    assert admin.totp_enabled is False

    with pytest.raises(HTTPException) as exc:
        await resolve(admin, tok)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_revoke_sessions_kills_every_token_including_current(fast_auth):
    admin = make_admin()
    current = issue(admin)
    assert await resolve(admin, current) is admin

    db = FakeDB(admin)
    await auth_router.revoke_sessions(make_request(), db, admin)
    assert admin.token_version == 1
    assert db.added and db.added[0].action == "admin.sessions.revoke_all"

    with pytest.raises(HTTPException) as exc:
        await resolve(admin, current)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_recovery_login_advances_epoch(fast_auth, monkeypatch):
    async def allow_rate_limit(*_args, **_kwargs):
        return True, 1

    monkeypatch.setattr(auth_router, "rl_hit", allow_rate_limit)
    monkeypatch.setattr(auth_router, "_verify_totp_or_recovery", lambda _admin, _code: ("recovery", None))

    admin = make_admin(totp_enabled=True, totp_secret=generate_totp_secret())
    old = issue(admin)
    assert await resolve(admin, old) is admin

    res = await auth_router.login(
        LoginIn(email=admin.email, password="correct-password", totp_code="recovery-code"),
        make_request(),
        FakeDB(admin),
    )
    assert admin.token_version == 1
    assert res.token is not None

    # The old session is burned; the recovery-login token carries the new epoch.
    with pytest.raises(HTTPException) as exc:
        await resolve(admin, old)
    assert exc.value.status_code == 401
    assert await resolve(admin, res.token) is admin


@pytest.mark.asyncio
async def test_legacy_token_without_ver_claim_is_graceful():
    """Tokens minted before this feature (no `ver` claim) count as epoch 0."""
    admin = make_admin()
    legacy, _jti, _ttl = create_access_token(
        str(admin.id), {"email": admin.email, "role": admin.role}
    )
    assert await resolve(admin, legacy) is admin

    # Once the account epoch advances, the legacy token is rejected like any stale one.
    admin.token_version = 1
    with pytest.raises(HTTPException) as exc:
        await resolve(admin, legacy)
    assert exc.value.status_code == 401
