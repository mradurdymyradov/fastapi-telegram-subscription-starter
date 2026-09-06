import secrets
import string
import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

settings = get_settings()
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    # passlib internally uses constant-time compare; this wrapper exists so callers
    # don't import passlib directly.
    return _pwd.verify(raw, hashed)


def create_access_token(subject: str, extra: dict | None = None) -> tuple[str, str, int]:
    """Returns (token, jti, expires_in_seconds). jti is needed for revocation."""
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=settings.jwt_expire_minutes)
    jti = uuid.uuid4().hex
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": jti,
        "iss": "membership-admin",
    }
    if extra:
        payload.update(extra)
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, jti, int((exp - now).total_seconds())


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer="membership-admin",
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except JWTError:
        return None


def generate_referral_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
