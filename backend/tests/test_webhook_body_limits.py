"""GK-404: webhook request-body size limits.

Both public payment webhooks (``/webhooks/stripe``, ``/webhooks/lava``)
authenticate the caller only *after* reading the raw body (Stripe verifies a
signature over it, Lava checks a header credential). An anonymous caller must not
be able to stream an unbounded body and exhaust memory/workers before that check
runs, so the body is capped *before* it is fully buffered, parsed, or verified.

Fully mocked — no DB/Redis/network. Tests drive the endpoint coroutines directly
with a real Starlette ``Request`` so the streaming / Content-Length code paths run
against real framework behaviour.
"""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routers import webhooks_in

SMALL_LIMIT = 64


def _make_request(
    body: bytes,
    *,
    content_length: int | str | None = "auto",
    stream_chunk: int | None = None,
    path: str = "/webhooks/stripe",
) -> Request:
    """Build a real Starlette Request that delivers ``body``.

    ``content_length="auto"`` sets the header to ``len(body)``; an int sets it
    literally; ``None`` omits it (chunked transfer-encoding style). ``stream_chunk``
    delivers the body in pieces with ``more_body`` so the streamed-size cap is
    exercised for bodies whose length is not declared up front.
    """
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if content_length == "auto":
        headers.append((b"content-length", str(len(body)).encode()))
    elif content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443),
        "scheme": "https",
    }

    pieces = (
        [body[i : i + stream_chunk] for i in range(0, len(body), stream_chunk)]
        if stream_chunk
        else [body]
    ) or [b""]
    state = {"i": 0}

    async def receive():
        i = state["i"]
        if i < len(pieces):
            state["i"] += 1
            return {"type": "http.request", "body": pieces[i], "more_body": i < len(pieces) - 1}
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class _Recorder:
    """Stand-in that records whether (and how often) it was invoked."""

    def __init__(self, result=None):
        self.calls = 0
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.result


@pytest.mark.asyncio
async def test_stripe_webhook_rejects_oversize_by_content_length(monkeypatch):
    monkeypatch.setattr(webhooks_in, "MAX_WEBHOOK_BODY_BYTES", SMALL_LIMIT, raising=False)
    parse = _Recorder(result=None)
    monkeypatch.setattr(webhooks_in.StripeProvider, "parse_webhook", parse)

    request = _make_request(b"x" * (SMALL_LIMIT + 100))

    with pytest.raises(HTTPException) as exc:
        await webhooks_in.stripe_webhook(request, db=object(), stripe_signature="sig")

    assert exc.value.status_code == 413
    assert parse.calls == 0  # rejected before any parsing / verification


@pytest.mark.asyncio
async def test_lava_webhook_rejects_oversize_by_content_length(monkeypatch):
    monkeypatch.setattr(webhooks_in, "MAX_WEBHOOK_BODY_BYTES", SMALL_LIMIT, raising=False)
    verify = _Recorder(result=True)
    parse = _Recorder(result=None)
    monkeypatch.setattr(webhooks_in.LavaProvider, "verify_webhook_auth", verify)
    monkeypatch.setattr(webhooks_in.LavaProvider, "parse_webhook", parse)

    request = _make_request(b"x" * (SMALL_LIMIT + 100), path="/webhooks/lava")

    with pytest.raises(HTTPException) as exc:
        await webhooks_in.lava_webhook(request, db=object(), x_api_key=None, authorization=None)

    assert exc.value.status_code == 413
    assert verify.calls == 0  # rejected before the credential check runs
    assert parse.calls == 0


@pytest.mark.asyncio
async def test_stripe_webhook_rejects_oversize_streamed_without_content_length(monkeypatch):
    monkeypatch.setattr(webhooks_in, "MAX_WEBHOOK_BODY_BYTES", SMALL_LIMIT, raising=False)
    parse = _Recorder(result=None)
    monkeypatch.setattr(webhooks_in.StripeProvider, "parse_webhook", parse)

    # No Content-Length (chunked transfer-encoding style), delivered in pieces.
    request = _make_request(b"x" * (SMALL_LIMIT + 100), content_length=None, stream_chunk=16)

    with pytest.raises(HTTPException) as exc:
        await webhooks_in.stripe_webhook(request, db=object(), stripe_signature="sig")

    assert exc.value.status_code == 413
    assert parse.calls == 0


@pytest.mark.asyncio
async def test_stripe_webhook_rejects_understated_content_length(monkeypatch):
    """A lying / short Content-Length must not bypass the streamed-size cap."""
    monkeypatch.setattr(webhooks_in, "MAX_WEBHOOK_BODY_BYTES", SMALL_LIMIT, raising=False)
    parse = _Recorder(result=None)
    monkeypatch.setattr(webhooks_in.StripeProvider, "parse_webhook", parse)

    request = _make_request(b"x" * (SMALL_LIMIT + 100), content_length=8, stream_chunk=16)

    with pytest.raises(HTTPException) as exc:
        await webhooks_in.stripe_webhook(request, db=object(), stripe_signature="sig")

    assert exc.value.status_code == 413
    assert parse.calls == 0


@pytest.mark.asyncio
async def test_stripe_webhook_accepts_body_within_limit(monkeypatch):
    monkeypatch.setattr(webhooks_in, "MAX_WEBHOOK_BODY_BYTES", SMALL_LIMIT, raising=False)
    parse = _Recorder(result=None)  # None → endpoint takes the "ignored" branch
    monkeypatch.setattr(webhooks_in.StripeProvider, "parse_webhook", parse)

    request = _make_request(b'{"ok": true}')

    result = await webhooks_in.stripe_webhook(request, db=object(), stripe_signature="sig")

    assert result == {"ok": True, "ignored": True}
    assert parse.calls == 1  # a within-limit body is passed through to the parser
