from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routers import auth as auth_router
from app.api.routers.auth import LoginIn, TotpConfirmIn, TotpEnrollIn
from app.services.totp import generate_totp_code, generate_totp_secret


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


def request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture(autouse=True)
def fast_auth(monkeypatch):
    async def allow_rate_limit(*_args, **_kwargs):
        return True, 1

    async def no_sleep(*_args, **_kwargs):
        return None

    def fake_hash(raw):
        return f"hash:{raw}"

    def fake_verify(raw, hashed):
        return hashed == f"hash:{raw}"

    monkeypatch.setattr(auth_router, "rl_hit", allow_rate_limit)
    monkeypatch.setattr(auth_router.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(auth_router, "hash_password", fake_hash)
    monkeypatch.setattr(auth_router, "verify_password", fake_verify)
    monkeypatch.setattr(
        auth_router,
        "create_access_token",
        lambda subject, extra=None: (f"token:{subject}", "jti", 14400),
    )


@pytest.mark.asyncio
async def test_login_without_totp_issues_token():
    admin = make_admin()
    res = await auth_router.login(
        LoginIn(email=admin.email, password="correct-password"),
        request(),
        FakeDB(admin),
    )

    assert res.token == "token:1"
    assert res.requires_totp is False
    assert res.expires_in == 14400


@pytest.mark.asyncio
async def test_totp_enrollment_confirms_secret_and_audits():
    admin = make_admin()
    enroll = await auth_router.totp_enroll(
        TotpEnrollIn(current_password="correct-password"),
        admin,
    )
    code = generate_totp_code(enroll.secret)
    db = FakeDB(admin)

    res = await auth_router.totp_confirm(
        TotpConfirmIn(
            current_password="correct-password",
            secret=enroll.secret,
            code=code,
        ),
        db,
        admin,
        request(),
    )

    assert res.enabled is True
    assert len(res.recovery_codes) == 8
    assert admin.totp_enabled is True
    assert admin.totp_secret == enroll.secret
    assert len(admin.totp_recovery_hashes) == 8
    assert db.added[0].action == "admin.totp.enable"
    assert db.added[0].target_id == str(admin.id)


@pytest.mark.asyncio
async def test_login_with_totp_requires_step_up_then_issues_token(monkeypatch):
    secret = generate_totp_secret()
    admin = make_admin(totp_enabled=True, totp_secret=secret)
    rate_limit_keys = []

    async def count_rate_limit(key, **_kwargs):
        rate_limit_keys.append(key)
        return True, 1

    monkeypatch.setattr(auth_router, "rl_hit", count_rate_limit)

    challenge = await auth_router.login(
        LoginIn(email=admin.email, password="correct-password"),
        request(),
        FakeDB(admin),
    )
    assert challenge.requires_totp is True
    assert challenge.token is None

    code = generate_totp_code(secret)
    res = await auth_router.login(
        LoginIn(email=admin.email, password="correct-password", totp_code=code),
        request(),
        FakeDB(admin),
    )

    assert res.token == "token:1"
    assert res.requires_totp is False
    assert admin.totp_last_counter is not None
    assert any(key.startswith("ratelimit:login:ip:") for key in rate_limit_keys)
    assert f"ratelimit:login:email:{admin.email}" in rate_limit_keys


@pytest.mark.asyncio
async def test_login_with_invalid_totp_is_rejected():
    secret = generate_totp_secret()
    admin = make_admin(totp_enabled=True, totp_secret=secret)
    db = FakeDB(admin)

    with pytest.raises(HTTPException) as exc:
        await auth_router.login(
            LoginIn(email=admin.email, password="correct-password", totp_code="not-a-code"),
            request(),
            db,
        )

    assert exc.value.status_code == 401
    assert admin.totp_last_counter is None
    assert db.added == []
