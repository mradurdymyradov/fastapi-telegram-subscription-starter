"""GK-486: the accounts the bot must never charge, and cannot be talked into it.

Grant asked for Owner to get the real bot on both of his accounts. That put two
accounts past the pre-launch hold and in front of live Stripe and Lava
checkouts, and the entire safety mechanism was a sentence in a Telegram message
asking him not to press «💎 Подписка» — sent to a person whose account already
carried five payment attempts from June. This is what replaced that sentence.

Written from the failure side, because it is a money guard:

* a listed account reaches `create_checkout` anyway, through an entry point
  nobody remembered — a stale inline button, a half-finished FSM flow;
* the list is set but read as empty because of a typo, so the guard is gone and
  nothing says so until a card is charged;
* an unlisted member is blocked, and the launch quietly sells nothing;
* the guard is scoped to the pre-launch hold and evaporates on launch day;
* the guard reads a module attribute that ten other test modules replace, so it
  is present in the source and absent in every test that drives the handler.

The last one is why the runtime guard calls `get_settings()` directly, and why
`test_no_stub_can_remove_the_money_guard` exists.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Router

from app.bot.handlers import no_charge
from app.bot.handlers import subscription as subscription_handlers
from app.config import Settings

VIP_USER = 123456789
VIP_USER_SECOND = 987654321
STRANGER = 999000111


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ── the predicate ───────────────────────────────────────────────────────────


def test_a_listed_account_is_blocked():
    settings = _settings(no_charge_tg_ids=f"{VIP_USER},{VIP_USER_SECOND}")
    assert settings.charges_blocked_for(VIP_USER) is True
    assert settings.charges_blocked_for(VIP_USER_SECOND) is True


def test_an_ordinary_member_is_not_blocked():
    """The failure nobody would notice until launch: guard on, sales off."""
    settings = _settings(no_charge_tg_ids=f"{VIP_USER},{VIP_USER_SECOND}")
    assert settings.charges_blocked_for(STRANGER) is False


def test_an_event_with_no_user_is_blocked():
    """A callback Telegram only partially kept has no `from_user`. It must not
    be able to spend money on somebody's card."""
    settings = _settings(no_charge_tg_ids=str(VIP_USER))
    assert settings.charges_blocked_for(None) is True


def test_a_malformed_list_blocks_everybody():
    """The asymmetry with GK-446, and the reason the two lists are separate.

    `prelaunch_hold_allowlist_ids` reads garbage as "nobody bypasses the hold" —
    failing open there costs somebody an inconvenience. Failing open *here*
    costs a real charge on a real card, and on this host it would be silent:
    `validate_security` only raises when `is_prod`, and the live host runs
    `APP_ENV=demo`, so a typo logs one warning line and the bot boots.
    """
    settings = _settings(no_charge_tg_ids="@paulhealingod")
    assert settings.charges_blocked_for(VIP_USER) is True
    assert settings.charges_blocked_for(STRANGER) is True


def test_an_unset_list_blocks_nobody():
    """Inert by default: a deployment that never sets this behaves as before."""
    settings = _settings()
    assert settings.no_charge_tg_ids == ""
    assert settings.charges_blocked_for(VIP_USER) is False
    assert settings.charges_blocked_for(STRANGER) is False


def test_a_malformed_list_is_a_security_error():
    settings = _settings(no_charge_tg_ids="@paulhealingod")
    assert any("NO_CHARGE_TG_IDS" in err for err in settings.validate_security())


def test_a_well_formed_list_is_not_a_security_error():
    settings = _settings(no_charge_tg_ids=f"{VIP_USER},{VIP_USER_SECOND}")
    assert not any("NO_CHARGE_TG_IDS" in err for err in settings.validate_security())


def test_the_key_is_covered_by_the_config_drift_guard():
    """`NO_CHARGE_TG_ID` (singular) reads as "nobody protected" and is silent.
    GK-436's guard only flags unknown keys matching one of our prefixes."""
    from app.config_audit import OURS_PREFIXES

    assert "NO_CHARGE_TG_IDS".startswith(OURS_PREFIXES)


def test_the_guard_is_not_tied_to_the_pre_launch_hold():
    """The protection has to survive launch day, which is when the hold lifts
    and when nobody is watching for a regression in it."""
    settings = _settings(no_charge_tg_ids=str(VIP_USER), enable_prelaunch_hold=False)
    assert settings.charges_blocked_for(VIP_USER) is True


# ── the router ──────────────────────────────────────────────────────────────


def _event(tg_id: int | None):
    return SimpleNamespace(from_user=SimpleNamespace(id=tg_id) if tg_id else None)


def test_the_router_is_inert_when_the_feature_is_unconfigured(monkeypatch):
    """Including the no-user case. Without the `.strip()` guard in `_blocked`,
    `charges_blocked_for(None)` is True even with an empty list, and this router
    would start claiming unattributable callbacks on every deployment."""
    monkeypatch.setattr(no_charge, "get_settings", lambda: _settings())
    assert no_charge._blocked(_event(VIP_USER)) is False
    assert no_charge._blocked(_event(None)) is False


def test_the_router_claims_only_listed_accounts(monkeypatch):
    monkeypatch.setattr(
        no_charge, "get_settings", lambda: _settings(no_charge_tg_ids=str(VIP_USER))
    )
    assert no_charge._blocked(_event(VIP_USER)) is True
    assert no_charge._blocked(_event(STRANGER)) is False
    assert no_charge._blocked(_event(None)) is True


def test_every_way_into_the_buying_flow_is_claimed():
    """The enumeration is the risk: one forgotten entry point is one open door.

    Asserted against the real `_register`, driving a throwaway router, so the
    set the module actually installs is what is checked — not a list repeated
    here that could drift away from it.
    """
    probe = Router(name="probe")
    no_charge._register(probe, lambda _event: True)

    # 2 message entry points + 3 FSM states = 5 message handlers;
    # buy_start + 4 prefixes + back_to_plans = 6 callback handlers.
    assert len(probe.message.handlers) == 5
    assert len(probe.callback_query.handlers) == 6


def test_the_management_screen_is_left_alone():
    """Grant is on this list and `subscription_manage` is the screen he opens.
    A blanket filter on `subscription.router` would take it away from him."""
    source = Path(no_charge.__file__).read_text(encoding="utf-8")
    body = source.split("def _register(")[1]
    for keep in ("subscription_manage", "sub_cancel_start", "offer_doc", "sub_back"):
        assert f'"{keep}"' not in body, f"{keep} must stay reachable for staff"


# ── the runtime guard, where the money is ───────────────────────────────────


def _blocking_settings():
    return SimpleNamespace(charges_blocked_for=lambda _tg_id: True)


@pytest.mark.asyncio
async def test_payment_method_chosen_refuses_before_any_provider_is_called(monkeypatch):
    """`pm:` is the point of no return — all four `create_checkout` calls in
    this handler are below the guard."""
    called = []

    async def boom(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("a blocked account reached create_checkout")

    for provider in ("StripeProvider", "LavaProvider", "ManualProvider"):
        monkeypatch.setattr(
            getattr(subscription_handlers, provider), "create_checkout", boom, raising=False
        )
    monkeypatch.setattr(subscription_handlers, "get_settings", _blocking_settings)

    cb = SimpleNamespace(data="pm:stripe:20", message=AsyncMock(), answer=AsyncMock())
    state = AsyncMock()

    await subscription_handlers.payment_method_chosen(
        cb, None, state, SimpleNamespace(id=5, tg_id=VIP_USER)
    )

    assert called == []
    cb.answer.assert_awaited()          # the spinner stops (GK-421's lesson)
    state.clear.assert_awaited()        # and the flow is not left half-open
    cb.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_lava_email_step_refuses_too(monkeypatch):
    """The second `create_checkout` site, and the easy one to miss: a message
    handler inside an FSM flow rather than a button."""

    async def boom(*_args, **_kwargs):
        raise AssertionError("a blocked account reached create_checkout")

    monkeypatch.setattr(
        subscription_handlers.LavaProvider, "create_checkout", boom, raising=False
    )
    monkeypatch.setattr(subscription_handlers, "get_settings", _blocking_settings)

    message = AsyncMock()
    message.text = "vip@example.com"
    state = AsyncMock()

    await subscription_handlers.lava_email_submitted(
        message, None, state, SimpleNamespace(id=5, tg_id=VIP_USER)
    )

    message.answer.assert_awaited()
    state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_an_ordinary_member_still_reaches_the_provider(monkeypatch):
    """The guard must not become a silent kill switch for everybody."""
    from test_checkout_failure_visibility import FakeSession, buy_state, callback, plan

    reached = []

    async def record(*_args, **_kwargs):
        reached.append(True)
        raise RuntimeError("stop here — we only needed to know it got this far")

    monkeypatch.setattr(subscription_handlers.StripeProvider, "create_checkout", record)
    monkeypatch.setattr(
        subscription_handlers,
        "get_settings",
        lambda: SimpleNamespace(charges_blocked_for=lambda _tg_id: False),
    )
    monkeypatch.setattr(
        subscription_handlers,
        "settings",
        SimpleNamespace(enable_zelle=False, enable_lava_live_checkout=False),
    )
    monkeypatch.setattr(subscription_handlers, "send_ops_alert", AsyncMock(return_value=True))

    await subscription_handlers.payment_method_chosen(
        callback("pm:stripe:20"),
        FakeSession(plan()),
        buy_state(),
        SimpleNamespace(id=10, tg_id=STRANGER),
    )

    assert reached == [True]


# ── the structural guard ────────────────────────────────────────────────────


def _functions_calling(name: str) -> list[tuple[str, ast.AST]]:
    root = Path(subscription_handlers.__file__).resolve().parents[3] / "app"
    found: list[tuple[str, ast.AST]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            hit = any(
                isinstance(call, ast.Call)
                and getattr(call.func, "attr", getattr(call.func, "id", None)) == name
                for call in ast.walk(node)
            )
            if hit:
                found.append((str(path.relative_to(root.parent)), node))
    return found


def test_every_checkout_call_site_consults_the_guard():
    """The half of this that ordering cannot give you.

    `no_charge.router` claims today's entry points. It cannot claim one added
    next month by somebody who never opens that file — and GK-453 is the
    recorded case of exactly that list being wrong for months. So this asks a
    structural question instead: is there any function in `app/` that can create
    a checkout without first asking whether this account may be charged?
    """
    offenders = []
    for path, func in _functions_calling("create_checkout"):
        guards = any(
            isinstance(call, ast.Call)
            and getattr(call.func, "attr", None) == "charges_blocked_for"
            for call in ast.walk(func)
        )
        if not guards:
            offenders.append(f"{path}:{func.lineno} — {func.name}() creates a checkout unguarded")

    assert not offenders, (
        "every path to a checkout must consult Settings.charges_blocked_for:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_itself_finds_the_call_sites():
    """Guards the guard: a broken scan would pass the test above silently."""
    names = {func.name for _path, func in _functions_calling("create_checkout")}
    assert "payment_method_chosen" in names
    assert "lava_email_submitted" in names


def test_no_stub_can_remove_the_money_guard():
    """The guard must not read the module-level `settings`.

    About ten test modules replace `subscription.settings` wholesale with a
    `SimpleNamespace` holding two or three flags. Had the guard read it, every
    one of those would have silently disabled it — including the tests that
    drive `payment_method_chosen` directly.
    """
    source = Path(subscription_handlers.__file__).read_text(encoding="utf-8")
    assert "settings.charges_blocked_for" not in source
    assert source.count("get_settings().charges_blocked_for") == 2
