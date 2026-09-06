"""Small RFC 6238 TOTP helper for admin 2FA.

Implemented with the Python standard library to avoid adding another runtime
dependency to the shared api/bot image.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

TOTP_DIGITS = 6
TOTP_INTERVAL_SECONDS = 30
RECOVERY_CODE_COUNT = 8

_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def normalize_totp_secret(secret: str) -> str:
    normalized = "".join(secret.upper().split()).rstrip("=")
    if not normalized:
        raise ValueError("empty TOTP secret")
    try:
        _decode_secret(normalized)
    except Exception as exc:  # noqa: BLE001 - keep caller error generic
        raise ValueError("invalid TOTP secret") from exc
    return normalized


def _decode_secret(secret: str) -> bytes:
    normalized = "".join(secret.upper().split()).rstrip("=")
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    return base64.b32decode(normalized + padding, casefold=True)


def _hotp(secret: str, counter: int) -> str:
    key = _decode_secret(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{code_int % (10 ** TOTP_DIGITS):0{TOTP_DIGITS}d}"


def generate_totp_code(secret: str, *, for_time: float | None = None) -> str:
    now = time.time() if for_time is None else for_time
    return _hotp(secret, int(now // TOTP_INTERVAL_SECONDS))


def verify_totp_code(
    secret: str | None,
    code: str | None,
    *,
    for_time: float | None = None,
    window: int = 1,
    last_counter: int | None = None,
) -> int | None:
    if not secret or not code:
        return None
    cleaned = "".join(str(code).split())
    if len(cleaned) != TOTP_DIGITS or not cleaned.isdigit():
        return None

    now = time.time() if for_time is None else for_time
    current_counter = int(now // TOTP_INTERVAL_SECONDS)
    for counter in range(current_counter - window, current_counter + window + 1):
        if last_counter is not None and counter <= last_counter:
            continue
        try:
            expected = _hotp(secret, counter)
        except Exception:  # noqa: BLE001 - invalid stored secret means no match
            return None
        if hmac.compare_digest(expected, cleaned):
            return counter
    return None


def otpauth_uri(secret: str, *, issuer: str, account: str) -> str:
    label = f"{issuer}:{account}"
    return (
        "otpauth://totp/"
        f"{quote(label)}?secret={quote(secret)}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_INTERVAL_SECONDS}"
    )


def normalize_recovery_code(code: str | None) -> str:
    if not code:
        return ""
    return "".join(ch for ch in code.upper() if ch.isalnum())


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes: list[str] = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes
