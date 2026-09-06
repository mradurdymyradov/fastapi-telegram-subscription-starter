"""URL safety checks used before issuing outgoing HTTP requests.

Outbound webhooks (integrations) are admin-controlled but an admin can be
phished or compromised; without a guard, that admin can pivot to internal
infrastructure (cloud metadata, hermes-agent on the same host, postgres, redis).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"https"}
# In dev we also allow http for localhost testing. is_https_only is checked
# at the call-site against settings.is_prod.


class UnsafeURLError(ValueError):
    pass


def _host_is_global(host: str) -> bool:
    """True iff `host` resolves only to public IP addresses.

    Resolves both IPv4 and IPv6. If ANY resolved IP is non-global (loopback,
    link-local, RFC1918, multicast, reserved), refuse.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
        # Defence-in-depth: explicitly reject AWS/GCP metadata addresses even
        # though is_global already covers 169.254/16.
        if str(ip) in {"169.254.169.254", "fd00:ec2::254"}:
            return False
    return True


def assert_safe_outbound_url(raw_url: str, *, require_https: bool = True) -> None:
    """Raise UnsafeURLError if the URL points to internal infrastructure.

    Use this for admin-configured webhooks, link previews, or anything where
    user/admin input can dictate where the server makes an outbound call.
    """
    if not raw_url or not isinstance(raw_url, str):
        raise UnsafeURLError("URL is empty")
    if len(raw_url) > 2048:
        raise UnsafeURLError("URL is too long")
    try:
        parsed = urlparse(raw_url.strip())
    except Exception as e:
        raise UnsafeURLError(f"Malformed URL: {e}") from e

    scheme = (parsed.scheme or "").lower()
    if require_https:
        if scheme not in _ALLOWED_SCHEMES:
            raise UnsafeURLError(f"Only HTTPS is allowed (got {scheme!r})")
    else:
        if scheme not in {"http", "https"}:
            raise UnsafeURLError(f"Only HTTP(S) is allowed (got {scheme!r})")

    host = (parsed.hostname or "").strip()
    if not host:
        raise UnsafeURLError("URL has no host")

    # Reject obvious shortcuts before DNS.
    lowered = host.lower()
    if lowered in {"localhost", "ip6-localhost", "ip6-loopback"}:
        raise UnsafeURLError("Refusing to call localhost")
    if lowered.endswith(".local") or lowered.endswith(".internal"):
        raise UnsafeURLError("Refusing to call internal-only TLD")

    # Reject Docker-side compose hostnames we know about on this host.
    if lowered in {"db", "redis", "api", "bot", "admin", "caddy", "hermes-agent1", "hermes-agent2"}:
        raise UnsafeURLError("Refusing to call internal service name")

    # No userinfo (foo:bar@host) — Lava/AmoCRM-style URLs don't need it and it
    # can be used to confuse parsers.
    if parsed.username or parsed.password:
        raise UnsafeURLError("URL must not contain userinfo")

    if not _host_is_global(host):
        raise UnsafeURLError("URL resolves to non-global address")
