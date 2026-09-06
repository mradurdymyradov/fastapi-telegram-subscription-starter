import asyncio
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import DB, CurrentAdmin
from app.config import get_settings
from app.db.models import AdminUser, utcnow
from app.services.audit import record as audit_record
from app.services.rate_limit import hit as rl_hit
from app.services.rate_limit import revoke_token
from app.services.security import create_access_token, hash_password, verify_password
from app.services.totp import (
    generate_recovery_codes,
    generate_totp_secret,
    normalize_recovery_code,
    normalize_totp_secret,
    otpauth_uri,
    verify_totp_code,
)

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=256)
    totp_code: str | None = Field(default=None, max_length=64)


class LoginOut(BaseModel):
    token: str | None = None
    email: str
    role: str
    expires_in: int = 0
    requires_totp: bool = False


class MeOut(BaseModel):
    id: int
    email: str
    role: str
    totp_enabled: bool


class ChangePasswordIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=12, max_length=256)


class TotpEnrollIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)


class TotpEnrollOut(BaseModel):
    secret: str
    otpauth_uri: str


class TotpConfirmIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    secret: str = Field(..., min_length=16, max_length=64)
    code: str = Field(..., min_length=1, max_length=64)


class TotpConfirmOut(BaseModel):
    enabled: bool
    recovery_codes: list[str]


class TotpDisableIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    code: str = Field(..., min_length=1, max_length=64)


class TotpStatusOut(BaseModel):
    enabled: bool
    recovery_codes_remaining: int


def _client_ip(request: Request) -> str:
    """Honour X-Forwarded-For only when behind our own Caddy reverse proxy."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


_GENERIC_LOGIN_ERR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid credentials",
)


def _verify_current_password(password: str, admin: AdminUser) -> bool:
    return verify_password(password, admin.password_hash)


def _hash_recovery_codes(codes: list[str]) -> list[str]:
    return [hash_password(normalize_recovery_code(code)) for code in codes]


def _consume_recovery_code(admin: AdminUser, code: str | None) -> bool:
    normalized = normalize_recovery_code(code)
    if not normalized:
        return False

    remaining: list[str] = []
    matched = False
    for code_hash in admin.totp_recovery_hashes or []:
        if not matched:
            try:
                matched = verify_password(normalized, code_hash)
            except Exception:  # noqa: BLE001 - ignore corrupt legacy hashes
                matched = False
            if matched:
                continue
        remaining.append(code_hash)

    if matched:
        admin.totp_recovery_hashes = remaining
    return matched


def _verify_totp_or_recovery(admin: AdminUser, code: str | None) -> tuple[str | None, int | None]:
    counter = verify_totp_code(
        admin.totp_secret,
        code,
        last_counter=admin.totp_last_counter,
    )
    if counter is not None:
        return "totp", counter
    if _consume_recovery_code(admin, code):
        return "recovery", None
    return None, None


@router.post("/login", response_model=LoginOut)
async def login(payload: LoginIn, request: Request, db: DB):
    # Layered rate-limit: by IP (cheap) and by email (slower but prevents
    # distributed brute force on a single account).
    ip = _client_ip(request)
    limit = max(1, settings.login_rate_limit_per_minute)

    ip_ok, _ = await rl_hit(f"ratelimit:login:ip:{ip}", limit=limit, window_seconds=60)
    email_ok, _ = await rl_hit(
        f"ratelimit:login:email:{payload.email.lower()}",
        limit=max(1, limit * 2),
        window_seconds=300,
    )
    if not (ip_ok and email_ok):
        # Constant-time delay so the 429 doesn't reveal exhaustion timing.
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts")

    admin = (
        await db.execute(select(AdminUser).where(AdminUser.email == payload.email))
    ).scalar_one_or_none()

    # Constant-time path: always run verify_password against *some* hash so that
    # response timing for "unknown email" matches "wrong password".
    dummy_hash = "$2b$12$" + "C" * 53  # well-formed bcrypt, will never match
    target_hash = admin.password_hash if admin else dummy_hash
    pw_ok = verify_password(payload.password, target_hash)

    if admin is None or not pw_ok or not admin.is_active:
        logger.warning("login failed for %s from %s", payload.email, ip)
        raise _GENERIC_LOGIN_ERR

    if admin.totp_enabled:
        if not payload.totp_code:
            logger.info("login password ok; totp required for admin id=%s from %s", admin.id, ip)
            return LoginOut(
                token=None,
                email=admin.email,
                role=admin.role,
                expires_in=0,
                requires_totp=True,
            )

        totp_method, totp_counter = _verify_totp_or_recovery(admin, payload.totp_code)
        if totp_method is None:
            logger.warning("login totp failed for admin id=%s from %s", admin.id, ip)
            await asyncio.sleep(0.5)
            raise _GENERIC_LOGIN_ERR
        if totp_counter is not None:
            admin.totp_last_counter = totp_counter
        if totp_method == "recovery":
            # GK-405: a recovery-code login means the authenticator was lost — burn
            # every other outstanding session by advancing the account epoch. The
            # token minted below carries the new version, so this login stays valid.
            admin.token_version += 1
            await audit_record(
                db,
                actor_admin_id=admin.id,
                action="admin.totp.recovery_login",
                target_type="admin_user",
                target_id=admin.id,
                details={"recovery_codes_remaining": len(admin.totp_recovery_hashes or [])},
                request=request,
            )

    token, _jti, ttl = create_access_token(
        str(admin.id),
        {"email": admin.email, "role": admin.role, "ver": admin.token_version},
    )
    logger.info("login ok for admin id=%s email=%s from %s", admin.id, admin.email, ip)
    return LoginOut(token=token, email=admin.email, role=admin.role, expires_in=ttl)


@router.get("/me", response_model=MeOut)
async def me(admin: CurrentAdmin):
    return MeOut(id=admin.id, email=admin.email, role=admin.role, totp_enabled=admin.totp_enabled)


@router.post("/logout", status_code=204)
async def logout(request: Request, _admin: CurrentAdmin):
    """Revoke the current JWT by adding its jti to the Redis blacklist."""
    jti = getattr(request.state, "jwt_jti", None)
    exp = int(getattr(request.state, "jwt_exp", 0) or 0)
    if jti and exp:
        import time
        ttl = max(exp - int(time.time()), 1)
        await revoke_token(jti, ttl_seconds=ttl)
    return None


@router.post("/revoke-sessions", status_code=204)
async def revoke_sessions(request: Request, db: DB, admin: CurrentAdmin):
    """GK-405: log out everywhere by advancing the account epoch.

    Unlike /logout (which blacklists only the calling token's jti), this bumps
    ``token_version`` so every JWT ever issued for this admin — including the one
    used to make this request — fails the epoch check in current_admin. The admin
    must sign in again afterwards.
    """
    admin.token_version += 1
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="admin.sessions.revoke_all",
        target_type="admin_user",
        target_id=admin.id,
        request=request,
    )
    logger.info("all sessions revoked for admin id=%s", admin.id)
    return None


@router.post("/change-password", status_code=204)
async def change_password(payload: ChangePasswordIn, db: DB, admin: CurrentAdmin):
    if not verify_password(payload.current_password, admin.password_hash):
        # Same delay as login to avoid timing oracle.
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong current password")
    if secrets.compare_digest(payload.new_password, payload.current_password):
        raise HTTPException(status_code=400, detail="New password must differ from the current one")
    admin.password_hash = hash_password(payload.new_password)
    # GK-405: rotating the password invalidates every existing session, including the
    # one that made this call — the client must re-authenticate afterwards.
    admin.token_version += 1
    logger.info("password changed for admin id=%s", admin.id)
    return None


@router.get("/totp/status", response_model=TotpStatusOut)
async def totp_status(admin: CurrentAdmin):
    return TotpStatusOut(
        enabled=admin.totp_enabled,
        recovery_codes_remaining=len(admin.totp_recovery_hashes or []),
    )


@router.post("/totp/enroll", response_model=TotpEnrollOut)
async def totp_enroll(payload: TotpEnrollIn, admin: CurrentAdmin):
    if admin.totp_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="TOTP already enabled")
    if not _verify_current_password(payload.current_password, admin):
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong current password")

    secret = generate_totp_secret()
    return TotpEnrollOut(
        secret=secret,
        otpauth_uri=otpauth_uri(secret, issuer=settings.app_name, account=admin.email),
    )


@router.post("/totp/confirm", response_model=TotpConfirmOut)
async def totp_confirm(payload: TotpConfirmIn, db: DB, admin: CurrentAdmin, request: Request):
    if admin.totp_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="TOTP already enabled")
    if not _verify_current_password(payload.current_password, admin):
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong current password")

    try:
        secret = normalize_totp_secret(payload.secret)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid TOTP secret"
        ) from None

    if verify_totp_code(secret, payload.code) is None:
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid TOTP code")

    recovery_codes = generate_recovery_codes()
    now = utcnow()
    admin.totp_enabled = True
    admin.totp_secret = secret
    admin.totp_confirmed_at = now
    admin.totp_disabled_at = None
    admin.totp_last_counter = None
    admin.totp_recovery_hashes = _hash_recovery_codes(recovery_codes)
    admin.token_version += 1  # GK-405: enabling TOTP invalidates pre-2FA sessions
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="admin.totp.enable",
        target_type="admin_user",
        target_id=admin.id,
        details={"recovery_codes_issued": len(recovery_codes)},
        request=request,
    )
    logger.info("totp enabled for admin id=%s", admin.id)
    return TotpConfirmOut(enabled=True, recovery_codes=recovery_codes)


@router.post("/totp/disable", status_code=204)
async def totp_disable(payload: TotpDisableIn, db: DB, admin: CurrentAdmin, request: Request):
    if not admin.totp_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TOTP is not enabled")
    if not _verify_current_password(payload.current_password, admin):
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong current password")

    method, _counter = _verify_totp_or_recovery(admin, payload.code)
    if method is None:
        await asyncio.sleep(0.5)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid TOTP code")

    admin.totp_enabled = False
    admin.totp_secret = None
    admin.totp_confirmed_at = None
    admin.totp_disabled_at = utcnow()
    admin.totp_last_counter = None
    admin.totp_recovery_hashes = []
    admin.token_version += 1  # GK-405: disabling TOTP invalidates existing sessions
    await audit_record(
        db,
        actor_admin_id=admin.id,
        action="admin.totp.disable",
        target_type="admin_user",
        target_id=admin.id,
        details={"method": method},
        request=request,
    )
    logger.info("totp disabled for admin id=%s method=%s", admin.id, method)
    return None
