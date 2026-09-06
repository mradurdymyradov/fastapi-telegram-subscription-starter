import ast
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.observability import alerts


@pytest.mark.asyncio
async def test_send_ops_alert_accepts_username_chat_target(monkeypatch):
    sent = []
    closed = []

    class FakeSession:
        async def close(self):
            closed.append(True)

    class FakeBot:
        def __init__(self, token):
            self.token = token
            self.session = FakeSession()

        async def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(
            app_env="demo",
            bot_token="123456:test-token",
            alert_chat_id="@alert_membership_community",
        ),
    )
    monkeypatch.setattr("aiogram.Bot", FakeBot)

    ok = await alerts.send_ops_alert("smoke", severity="info")

    assert ok is True
    assert sent[0]["chat_id"] == "@alert_membership_community"
    assert "membership_saas ops" in sent[0]["text"]
    assert closed == [True]


@pytest.mark.asyncio
async def test_send_ops_alert_escapes_html_in_message_body(monkeypatch):
    sent = []

    class FakeSession:
        async def close(self):
            return None

    class FakeBot:
        def __init__(self, token):
            self.session = FakeSession()

        async def send_message(self, **kwargs):
            sent.append(kwargs)

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(
            app_env="demo",
            bot_token="123456:test-token",
            alert_chat_id="@alerts",
        ),
    )
    monkeypatch.setattr("aiogram.Bot", FakeBot)

    ok = await alerts.send_ops_alert("failure: <class 'ValueError'> & retry")

    assert ok is True
    assert "failure: &lt;class &#x27;ValueError&#x27;&gt; &amp; retry" in sent[0]["text"]
    assert "<class" not in sent[0]["text"]
    assert sent[0]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_send_ops_alert_retries_parse_error_as_plain_text(monkeypatch):
    sent = []
    closed = []

    class FakeSession:
        async def close(self):
            closed.append(True)

    class FakeBot:
        def __init__(self, token):
            self.session = FakeSession()

        async def send_message(self, **kwargs):
            sent.append(kwargs)
            if len(sent) == 1:
                raise TelegramBadRequest(
                    method=None,
                    message="Bad Request: can't parse entities: unsupported start tag class",
                )

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(
            app_env="demo",
            bot_token="123456:test-token",
            alert_chat_id="@alerts",
        ),
    )
    monkeypatch.setattr("aiogram.Bot", FakeBot)

    ok = await alerts.send_ops_alert("failure: <class 'ValueError'>")

    assert ok is True
    assert len(sent) == 2
    assert sent[0]["parse_mode"] == "HTML"
    assert sent[1]["parse_mode"] is None
    assert sent[1]["text"].endswith("failure: <class 'ValueError'>")
    assert closed == [True]


@pytest.mark.asyncio
async def test_send_ops_alert_missing_config_warns(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(app_env="demo", bot_token="", alert_chat_id=""),
    )
    caplog.set_level(logging.WARNING)

    ok = await alerts.send_ops_alert("smoke", severity="warn")

    assert ok is False
    assert "BOT_TOKEN,ALERT_CHAT_ID" in caplog.text


@pytest.mark.asyncio
async def test_health_reports_missing_ops_alert_config_in_launch_env(monkeypatch):
    from app.api import main as api_main

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _query):
            return None

    async def fresh_heartbeat():
        return 10

    monkeypatch.setattr(
        api_main,
        "settings",
        SimpleNamespace(
            app_env="demo",
            bot_token="",
            alert_chat_id="",
            bot_heartbeat_max_age_seconds=180,
            is_dev=False,
        ),
    )
    monkeypatch.setattr(api_main, "async_session", lambda: FakeSession())
    monkeypatch.setattr(api_main, "seconds_since_last_heartbeat", fresh_heartbeat)

    response = await api_main.health()
    body = json.loads(response.body)

    assert response.status_code == 503
    assert body["status"] == "degraded"
    assert body["ops_alerts"] == {
        "configured": False,
        "missing": ["BOT_TOKEN", "ALERT_CHAT_ID"],
        "bot_token_configured": False,
        "chat_configured": False,
        "chat_target_type": None,
        "required": True,
    }


# ── The contract, enforced at the call sites ────────────────────────────────


def _send_ops_alert_call_bodies() -> list[tuple[str, int, str]]:
    """Every `send_ops_alert(...)` argument list in `app/`, as source text.

    A paren-balance scan rather than an AST walk on purpose: the thing being
    checked is what a human typed into the literal, and `ast` would hand back
    only the already-joined constants — losing the f-string parts, which is
    exactly where the markup was.

    Comment-only lines are dropped, so a comment may name the thing it is
    warning about. Without this the rule cannot be explained at the site it
    applies to, which is where an explanation is worth most.
    """
    root = Path(__file__).resolve().parents[1] / "app"
    found: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"send_ops_alert\(", source):
            start = match.end()
            depth, index = 1, start
            while index < len(source) and depth:
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
                index += 1
            body = "\n".join(
                line
                for line in source[start : index - 1].splitlines()
                if not line.lstrip().startswith("#")
            )
            line_no = source.count("\n", 0, match.start()) + 1
            found.append((str(path.relative_to(root.parent)), line_no, body))
    return found


def test_no_ops_alert_ships_html_markup_in_its_body():
    """GK-451: the body is plain text, and this is what keeps it that way.

    `send_ops_alert` escapes the whole body, so a `<code>` wrapper at a call
    site arrives as the literal characters `&lt;code&gt;` and an `html.escape`
    at a call site escapes twice — `&` becomes `&amp;`. Both were live: three
    callers had wrappers, one of them double-escaping, and two more were being
    written on other branches of the same release against the old assumption.

    A unit test on `send_ops_alert` itself cannot catch that; the defect is in
    the callers. So this test reads them. It is deliberately the kind of test
    that fails when somebody adds a *new* alert with markup, because that is the
    failure mode — the function was fine, the contract around it was not.
    """
    offenders = []
    for path, line, body in _send_ops_alert_call_bodies():
        if re.search(r"</?(?:b|i|u|s|a|code|pre|tg-spoiler|blockquote)\b", body):
            offenders.append(f"{path}:{line} — HTML tag in an alert body")
        if "html.escape" in body:
            offenders.append(f"{path}:{line} — html.escape at the call site (escaped twice)")

    assert not offenders, "ops alert bodies must be plain text:\n  " + "\n  ".join(offenders)


def _functions_that_send_ops_alerts() -> list[tuple[str, ast.AST]]:
    """Every function in `app/` whose body contains a `send_ops_alert(...)` call.

    AST here, where the scan above uses text, because this question is about
    program structure rather than about what a human typed: which function does
    the call live in, and what else happens inside it.

    `send_ops_alert`'s own definition is not matched — it contains no call to
    itself, and the `html.escape` inside it is the one that is supposed to be
    there.
    """
    root = Path(__file__).resolve().parents[1] / "app"
    found: list[tuple[str, ast.AST]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            sends = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "send_ops_alert"
                for call in ast.walk(node)
            )
            if sends:
                found.append((str(path.relative_to(root.parent)), node))
    return found


def test_nothing_escapes_an_alert_before_send_ops_alert_does():
    """The half of the contract the text scan structurally cannot see.

    `test_no_ops_alert_ships_html_markup_in_its_body` reads what is inside the
    call parens. The real GK-455 defect was three lines above them:

        payment_id = html.escape(str(payment.id))    # <- here
        ...
        await send_ops_alert(f"payment_id={payment_id}...")

    which double-escapes — Telegram's error text carries `&`, and the alert
    explaining why a paying member got no invite arrives as `&amp;amp;`. Nothing
    in the argument list looks wrong, because nothing in it is.

    So this asks a structural question instead: does any function that sends an
    ops alert escape anything at all? Inside such a function there is no correct
    use of `html.escape` — `send_ops_alert` escapes the whole body itself. If one
    ever legitimately needs to build user-facing HTML *and* page ops, that is two
    functions.
    """
    offenders = []
    for path, func in _functions_that_send_ops_alerts():
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "escape"
                and isinstance(node.value, ast.Name)
                and node.value.id == "html"
            ):
                offenders.append(
                    f"{path}:{node.lineno} — html.escape inside {func.name}(), "
                    "which sends an ops alert (escaped twice)"
                )

    assert not offenders, "ops alerts are escaped once, by the sender:\n  " + "\n  ".join(
        sorted(set(offenders))
    )


def test_the_scan_itself_finds_the_call_sites():
    """Guards the guard: a broken scan would pass the tests above silently."""
    calls = _send_ops_alert_call_bodies()
    senders = _functions_that_send_ops_alerts()

    assert len(calls) >= 10
    assert any("errors.py" in path for path, _, _ in calls)
    assert len(senders) >= 10
    assert any(func.name == "handle_bot_error" for _path, func in senders)
