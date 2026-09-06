"""GK-459: the pre-launch hold reaches the API, not only the bot process.

GK-443 made the bot silent. It did not make the **API** silent, and the API
talks to members too: every Stripe and Lava webhook, plus admin approval and
refunds in the panel, build their own `Bot` from the token and DM the member
directly. So while the bot answered «Бот сейчас в настройке», an inbound
cancellation webhook could answer «Вы можете продлить подписку через
/subscribe» — sending a member to a command that is not in the dispatcher at
all (GK-443 registers the hold router and returns, so those handlers do not
exist while it is on).

The tests are grouped by what would actually go wrong:

* a held message names a command the hold has removed;
* the hold silences a member who really *was* charged — the opposite mistake,
  and the worse one: GK-444's "charged 1500 ₽, told nothing";
* GK-446's allowlisted ids meet the held copy, so the texts they were
  allowlisted to check on a live flow are not the texts that ship;
* and with the flag off, every existing message is byte-for-byte what it was.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.routers import payments as payments_router
from app.api.routers.payments import ManualDecision, moderate_manual
from app.bot.handlers.hold import HOLD_MESSAGE
from app.services import billing_notifications
from app.services.billing_notifications import HOLD_HELP_LINE, prelaunch_hold_applies_to

MEMBER_TG_ID = 10010
ALLOWLISTED_TG_ID = 777001

#: Every command GK-443 removes from the dispatcher. While the hold is on, each
#: of these resolves to the заглушка, so a billing DM that names one is an
#: instruction to a wall — which is the whole of this defect.
DEAD_COMMANDS = ["/subscribe", "/cabinet", "/support"]


class ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, *values):
        self.values = list(values)

    async def execute(self, _query):
        if not self.values:
            raise AssertionError("FakeSession.execute called without queued result")
        return ScalarResult(self.values.pop(0))


def make_user(**overrides):
    data = {"id": 10, "tg_id": MEMBER_TG_ID}
    data.update(overrides)
    return SimpleNamespace(**data)


def make_plan(**overrides):
    data = {"id": 20, "name": "Monthly Access", "code": "1m"}
    data.update(overrides)
    return SimpleNamespace(**data)


def make_payment(**overrides):
    data = {
        "id": 123,
        "user_id": 10,
        "gift_recipient_id": None,
        "plan_id": 20,
        "provider": "lava",
        "amount": Decimal("1500.00"),
        "currency": "RUB",
        "status": "awaiting_review",
        "approved_at": None,
        "approved_by": None,
        "note": None,
        "is_gift": False,
        "is_renewal": False,
        "billing_period_end": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def make_subscription(**overrides):
    data = {
        "id": 50,
        "user_id": 10,
        "plan_id": 20,
        "provider": "lava",
        "current_period_end": datetime(2026, 9, 1, tzinfo=UTC),
        "expires_at": datetime(2026, 9, 1, tzinfo=UTC),
        "grace_ends_at": None,
        "invite_link": "https://t.me/+invite",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


@pytest.fixture
def hold(monkeypatch):
    """Set the flag the way the live host sets it, at the module the code reads.

    `prelaunch_hold_applies_to` calls `get_settings()` per message rather than
    reading a module-level snapshot, so this patches the callable, not a value.
    """

    def _set(on: bool, *, allowlist: frozenset[int] = frozenset()):
        monkeypatch.setattr(
            billing_notifications,
            "get_settings",
            lambda: SimpleNamespace(
                enable_prelaunch_hold=on,
                prelaunch_hold_allowlist_ids=allowlist,
            ),
        )

    return _set


@pytest.fixture
def sent_messages(monkeypatch):
    sent = []

    async def fake_send_message(tg_id, text, reply_markup=None):
        sent.append((tg_id, text, reply_markup))
        return True

    monkeypatch.setattr(billing_notifications, "send_message", fake_send_message)
    return sent


@pytest.fixture
def api_sent_messages(monkeypatch):
    """The API's two direct sends import `send_message` inside the function body."""
    sent = []

    async def fake_send_message(tg_id, text, reply_markup=None):
        sent.append((tg_id, text, reply_markup))
        return True

    monkeypatch.setattr("app.services.notifications.send_message", fake_send_message)
    return sent


# ── The copy belongs to the hold, not to this module ────────────────────────


def test_the_held_tail_matches_the_one_the_hold_hands_out():
    """Two constants, one sentence — and this is what stops them drifting.

    `HOLD_MESSAGE` is Grant-approved copy that may only change on his say-so.
    `HOLD_HELP_LINE` is a second copy of its closing sentence, living in the
    services layer so the API can use it without importing a bot Router. A
    member who taps a command and a member who gets a webhook DM must be sent
    to the same place in the same words.
    """
    assert HOLD_MESSAGE.endswith(HOLD_HELP_LINE)
    assert "@GKcurators" in HOLD_HELP_LINE


# ── The predicate ───────────────────────────────────────────────────────────


def test_the_hold_does_not_apply_while_the_flag_is_off(hold):
    hold(False)
    assert prelaunch_hold_applies_to(MEMBER_TG_ID) is False
    assert prelaunch_hold_applies_to(None) is False


def test_the_hold_applies_to_an_ordinary_member(hold):
    hold(True)
    assert prelaunch_hold_applies_to(MEMBER_TG_ID) is True


def test_an_allowlisted_id_keeps_the_real_copy(hold):
    """GK-446 exists so the finished texts can be checked on a live flow.

    Handing those ids the held copy would check the wrong thing, so the API's
    hold is per-recipient rather than per-process — the same shape as the
    dispatcher gate, which lets the allowlist past and holds everyone else.
    """
    hold(True, allowlist=frozenset({ALLOWLISTED_TG_ID}))
    assert prelaunch_hold_applies_to(ALLOWLISTED_TG_ID) is False
    assert prelaunch_hold_applies_to(MEMBER_TG_ID) is True


def test_a_message_we_cannot_attribute_lands_on_the_hold(hold):
    """Unattributable means held — the safe direction GK-443 chose everywhere."""
    hold(True, allowlist=frozenset({ALLOWLISTED_TG_ID}))
    assert prelaunch_hold_applies_to(None) is True


# ── The defect: the API pitching a command that no longer exists ────────────


@pytest.mark.asyncio
async def test_a_cancellation_webhook_no_longer_pitches_subscribe(hold, sent_messages):
    """The exact line GK-459 was filed on, through the exact call the webhook makes."""
    hold(True)

    ok = await billing_notifications.notify_subscription_cancelled(
        FakeSession(make_user(), make_plan()),
        make_subscription(),
        provider="lava",
    )

    assert ok is True
    text = sent_messages[0][1]
    assert "/subscribe" not in text
    assert "продлить подписку" not in text
    assert HOLD_HELP_LINE in text
    # The facts survive: a member still learns what was cancelled and until
    # when they keep access. Only the offer to buy again is gone.
    assert "Получена отмена подписки" in text
    assert "Доступ сохраняется до" in text
    assert "2026-09-01" in text


@pytest.mark.asyncio
async def test_a_cancellation_still_offers_renewal_with_the_hold_off(hold, sent_messages):
    hold(False)

    await billing_notifications.notify_subscription_cancelled(
        FakeSession(make_user(), make_plan()),
        make_subscription(),
        provider="lava",
    )

    text = sent_messages[0][1]
    assert "Вы можете продлить подписку через /subscribe, когда будете готовы." in text
    assert HOLD_HELP_LINE not in text


@pytest.mark.asyncio
async def test_a_failed_payment_names_the_curators_instead_of_support(hold, sent_messages):
    """One hop, not two, in the message that says «you may have been charged».

    `/support` is not a dead end under the hold — it answers the заглушка, which
    hands out @GKcurators. But this is the wrong message to spend a redirect on.
    """
    hold(True)

    await billing_notifications.notify_payment_failed(
        FakeSession(make_user(), make_plan()),
        payment=make_payment(),
        subscription=make_subscription(grace_ends_at=datetime(2026, 9, 4, tzinfo=UTC)),
        provider="lava",
    )

    text = sent_messages[0][1]
    assert "/support" not in text
    assert "@GKcurators" in text
    assert "не оплачивайте дважды" in text
    assert "2026-09-04" in text


@pytest.mark.asyncio
async def test_the_archive_notice_drops_cabinet_under_the_hold(hold, sent_messages):
    hold(True)

    await billing_notifications.notify_archive_password_updated(MEMBER_TG_ID)

    text = sent_messages[0][1]
    assert "/cabinet" not in text
    assert HOLD_HELP_LINE in text
    assert "Данные доступа к архиву обновлены" in text


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: billing_notifications.build_payment_succeeded_message(
                make_payment(), make_plan(), make_subscription(invite_link=None), hold=True
            ),
            id="initial_payment",
        ),
        pytest.param(
            lambda: billing_notifications.build_payment_succeeded_message(
                make_payment(is_gift=True, gift_recipient_id=11),
                make_plan(),
                make_subscription(invite_link=None),
                hold=True,
            ),
            id="gift_access",
        ),
        pytest.param(
            lambda: billing_notifications.build_payment_succeeded_message(
                make_payment(is_renewal=True), make_plan(), make_subscription(), hold=True
            ),
            id="renewal",
        ),
        pytest.param(
            lambda: billing_notifications.build_payment_failed_message(
                make_payment(), make_plan(), make_subscription(), provider="stripe", hold=True
            ),
            id="payment_failed",
        ),
        pytest.param(
            lambda: billing_notifications.build_subscription_cancelled_message(
                make_subscription(), make_plan(), provider="stripe", hold=True
            ),
            id="subscription_cancelled",
        ),
        pytest.param(
            lambda: billing_notifications.build_archive_password_updated_message(hold=True),
            id="archive_password",
        ),
    ],
)
def test_no_held_message_names_a_command_the_hold_has_removed(build):
    """The sweep. A new template that forgets the hold fails here, not in production."""
    text = build()
    for command in DEAD_COMMANDS:
        assert command not in text, f"held message still names {command}: {text!r}"


# ── The opposite mistake, which would be worse ──────────────────────────────


@pytest.mark.asyncio
async def test_a_member_who_paid_is_still_told_and_still_gets_the_invite(hold, sent_messages):
    """The hold removes the sell, not the goods.

    A provider webhook fires once. The bot's scheduled jobs could be skipped
    outright (GK-443) because they run again on the next tick; skipping this
    would produce GK-444's outcome — charged and told nothing — which is the
    failure this fix must not introduce while removing the other one.
    """
    hold(True)

    ok = await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()),
        make_payment(billing_period_end=datetime(2026, 9, 24, tzinfo=UTC)),
        make_subscription(),
    )

    assert ok is True
    tg_id, text, _markup = sent_messages[0]
    assert tg_id == MEMBER_TG_ID
    assert "Оплата получена" in text
    assert "1500.00 RUB" in text
    assert "2026-09-24" in text
    assert "https://t.me/+invite" in text


@pytest.mark.asyncio
async def test_a_paid_member_without_an_invite_link_gets_a_live_contact(hold, sent_messages):
    """No link and no /cabinet leaves only one place to send them."""
    hold(True)

    await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()),
        make_payment(),
        make_subscription(invite_link=None),
    )

    text = sent_messages[0][1]
    assert "/cabinet" not in text
    assert HOLD_HELP_LINE in text
    assert "Оплата получена" in text


@pytest.mark.asyncio
async def test_a_renewal_notice_is_the_same_under_the_hold(hold, sent_messages):
    """A renewal reports a charge that already happened and offers nothing.

    Nothing to hold, so nothing changes — recorded as a test so that a future
    "make everything hold-aware" pass has to argue with it first.
    """
    renewal = make_payment(is_renewal=True, billing_period_end=datetime(2026, 10, 1, tzinfo=UTC))

    hold(True)
    await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()), renewal, make_subscription()
    )
    hold(False)
    await billing_notifications.notify_payment_succeeded(
        FakeSession(make_user(), make_plan()), renewal, make_subscription()
    )

    assert sent_messages[0][1] == sent_messages[1][1]
    assert "Подписка продлена" in sent_messages[0][1]


@pytest.mark.asyncio
async def test_an_allowlisted_member_gets_the_real_cancellation_copy(hold, sent_messages):
    hold(True, allowlist=frozenset({ALLOWLISTED_TG_ID}))

    await billing_notifications.notify_subscription_cancelled(
        FakeSession(make_user(tg_id=ALLOWLISTED_TG_ID), make_plan()),
        make_subscription(),
        provider="stripe",
    )

    text = sent_messages[0][1]
    assert "/subscribe" in text
    assert HOLD_HELP_LINE not in text


# ── The panel's own two sends ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rejected_payment_sends_a_held_member_to_the_curators(
    hold, api_sent_messages, monkeypatch
):
    hold(True)
    audits = []

    async def fake_audit_record(db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token="tkn"))
    monkeypatch.setattr(payments_router, "audit_record", fake_audit_record)

    payment = make_payment()
    result = await moderate_manual(
        payment.id,
        ManualDecision(decision="reject", reason="скриншот не читается"),
        FakeSession(payment, make_user()),
        SimpleNamespace(id=7),
        SimpleNamespace(),
    )

    assert result["ok"] is True
    assert result["status"] == "failed"
    tg_id, text, _markup = api_sent_messages[0]
    assert tg_id == MEMBER_TG_ID
    assert "/support" not in text
    assert HOLD_HELP_LINE in text
    # The reason still reaches them — that is the point of the message.
    assert "скриншот не читается" in text
    assert audits[0]["action"] == "payment.reject"


@pytest.mark.asyncio
async def test_a_refund_notice_sends_a_held_member_to_the_curators(
    hold, api_sent_messages, monkeypatch
):
    """GK-444 wrote this one down as a warning to give the curators by hand."""
    hold(True)
    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token="tkn"))

    payment = make_payment(currency="RUB")
    result = SimpleNamespace(
        refund=SimpleNamespace(status=payments_router.REFUND_CONFIRMED, amount=Decimal("1500.00")),
        state_changed=True,
        fully_refunded=True,
    )

    delivered = await payments_router._notify_payment_refunded(
        FakeSession(make_user()), payment, result
    )

    assert delivered is True
    text = api_sent_messages[0][1]
    assert "/support" not in text
    assert HOLD_HELP_LINE in text
    assert "Полный возврат" in text
    assert "1500.00 RUB" in text


@pytest.mark.asyncio
async def test_a_refund_notice_still_names_support_with_the_hold_off(
    hold, api_sent_messages, monkeypatch
):
    hold(False)
    monkeypatch.setattr(payments_router, "settings", SimpleNamespace(bot_token="tkn"))

    result = SimpleNamespace(
        refund=SimpleNamespace(status=payments_router.REFUND_CONFIRMED, amount=Decimal("19.00")),
        state_changed=True,
        fully_refunded=False,
    )

    await payments_router._notify_payment_refunded(
        FakeSession(make_user()), make_payment(currency="USD"), result
    )

    text = api_sent_messages[0][1]
    assert "Если у вас есть вопросы — напишите /support." in text
    assert HOLD_HELP_LINE not in text
