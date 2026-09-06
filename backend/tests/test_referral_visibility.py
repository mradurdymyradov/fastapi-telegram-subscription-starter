"""GK-373: referral visibility, share encoding, and money-partner wording.

These cover the safe, non-economics parts of Grant's feedback C02/C06/C09/B04/B11/B12:
share-link encoding, the bot partner view, the read-only ledger aggregates, and
the admin relationships endpoint shaping.
"""
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api.routers.referrals import (
    PartnerStatsPage,
    RelationshipsPage,
    list_partner_stats,
    list_relationships,
)
from app.bot.handlers.cabinet import (
    _fmt_usd,
    _partner_text,
    _share_keyboard,
    _share_prompt_text,
)
from app.bot.handlers.referral import _format_board, _plural_invited
from app.bot.keyboards import cabinet_keyboard, main_menu
from app.services.referral_ledger import (
    InvitedFriend,
    PartnerEarnings,
    PartnerOverview,
    invited_friends_with_earnings,
    invited_user_count,
    paid_invited_count,
    partner_earnings_summary,
    partner_overview,
)

NOW = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)


class Result:
    def __init__(self, value):
        self.value = value

    def all(self):
        return list(self.value or [])

    def scalar_one(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return self

    def first(self):
        return self.value


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)

    async def execute(self, _query):
        return Result(self.results.pop(0))


# --- B04: share link encoding -------------------------------------------------


def test_share_keyboard_encodes_spaces_as_percent_20_not_plus():
    markup = _share_keyboard("https://t.me/membership_bot?start=ref_ABC123")
    url = markup.inline_keyboard[0][0].url
    assert url.startswith("https://t.me/share/url?")
    # The share text has spaces; they must be %20, never "+", or Telegram shows
    # literal plus signs in the prefilled message (Grant's B04 screenshot).
    assert "%20" in url
    assert "+" not in url


# --- issue #11: raw copy-link button next to the Telegram share ---------------


def test_share_keyboard_offers_raw_copy_link_button():
    raw_link = "https://t.me/membership_bot?start=ref_ABC123"
    markup = _share_keyboard(raw_link)
    flat = [button for row in markup.inline_keyboard for button in row]

    # The Telegram share deep-link is still there (only opens inside Telegram)...
    share = next(b for b in flat if b.text == "📤 Поделиться ссылкой")
    assert share.url.startswith("https://t.me/share/url?")

    # ...and a sibling "copy link" button hands over the raw referral link for any
    # other messenger via Telegram's CopyTextButton (one-tap clipboard copy).
    copy = next(b for b in flat if b.text == "🔗 Скопировать ссылку")
    assert copy.url is None
    assert copy.callback_data is None
    assert copy.copy_text is not None
    # Copied verbatim — not URL-encoded, no spaces turned into "+" (cf. GK-372).
    assert copy.copy_text.text == raw_link
    assert "+" not in copy.copy_text.text


@pytest.mark.asyncio
async def test_invite_friend_cmd_attaches_share_and_copy_buttons():
    from unittest.mock import AsyncMock

    from app.bot.handlers.cabinet import invite_friend_cmd

    message = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(referral_code="ABC123")

    await invite_friend_cmd(message, user)

    message.answer.assert_awaited_once()
    markup = message.answer.await_args.kwargs["reply_markup"]
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "📤 Поделиться ссылкой" in labels
    assert "🔗 Скопировать ссылку" in labels


def test_main_menu_matches_grant_button_doc():
    # GK-413: main menu must match Grant's approved button doc (image2, 2026-07-09).
    # Standalone 🤝 Пригласить друга / 💰 Партнёрам entries are dropped from the top
    # level in favour of 🏆 Лидерборд; those flows stay reachable via /invite,
    # /partner and the Cabinet → 💰 Партнёрам shortcut.
    rows = [[button.text for button in row] for row in main_menu().keyboard]

    assert rows == [
        ["💎 Подписка", "👤 Кабинет"],
        ["🎬 Открыть архив", "🎁 Подарить"],
        ["🏆 Лидерборд", "💬 Поддержка"],
        ["ℹ️ О сообществе"],
    ]

    flat = [text for row in rows for text in row]
    assert "🏆 Лидерборд" in flat
    assert "🤝 Пригласить друга" not in flat
    assert "💰 Партнёрам" not in flat


def test_leaderboard_board_has_grant_goals_and_dynamic_rows():
    # GK-413: leaderboard copy is Grant's approved wording (button doc, image8) —
    # a static goals ladder plus the live top; the example handle in the doc is
    # illustrative and must never be hardcoded.
    rows = [
        (SimpleNamespace(username="alice", first_name="A"), 5, 0),
        (SimpleNamespace(username=None, first_name="Bob"), 1, 0),
    ]
    text = _format_board(rows)

    # Static framing Grant added: intro, goals ladder, live-top header, outro.
    assert "🏆 <b>Лидерборд партнёров</b>" in text
    # GK-452: Grant's 19.08 doc rewrote the closing sentence of the intro.
    assert "награды, которых нет в подписке" in text
    assert "не купить за деньги" not in text
    assert "<b>Цели и награды</b>" in text
    # GK-419 set the thresholds (50/75/100); GK-452 rotated the rewards up a
    # rung, so the personal session with Owner is now the 100 tier.
    assert "🥉 50 приглашённых, обучение в Sacred Mushroom University, пакет Премиум" in text
    assert "🥈 75 приглашённых, участие в ретрите со скидкой 50 процентов" in text
    assert "🥇 100 приглашённых, личная сессия с Павлом, 60 минут один на один" in text
    assert "участие в ретрите бесплатно" not in text
    assert "<b>Текущий топ</b>" in text
    assert "занимайте верхние строки" in text

    # Dynamic rows use Grant's "medal @name, N приглашённых" format with correct
    # Russian plural agreement; the doc's example @handle is never hardcoded.
    assert "🥇 @alice, 5 приглашённых" in text
    assert "🥈 Bob, 1 приглашённый" in text
    assert "GRANTPROGRESS" not in text


def test_leaderboard_empty_still_shows_goals_and_invites_first():
    text = _format_board([])
    assert "<b>Цели и награды</b>" in text
    assert "Пока пусто — будьте первым!" in text


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "приглашённый"),
        (2, "приглашённых"),
        (4, "приглашённых"),
        (5, "приглашённых"),
        (11, "приглашённых"),
        (21, "приглашённый"),
        (50, "приглашённых"),
        (100, "приглашённых"),
        (111, "приглашённых"),
    ],
)
def test_plural_invited_agrees_with_russian_grammar(n, expected):
    assert _plural_invited(n) == expected


def test_cabinet_shortcut_uses_partner_dashboard_label():
    active_buttons = [
        button.text for row in cabinet_keyboard(has_active_sub=True).inline_keyboard for button in row
    ]

    assert "💰 Партнёрам" in active_buttons
    assert "🔗 Реф-ссылка" not in active_buttons


def test_fast_share_prompt_points_to_partner_dashboard():
    user = SimpleNamespace(referral_code="ABC123")
    text = _share_prompt_text(user)

    assert "🤝 Пригласить друга" in text
    assert "💰 Партнёрам" in text
    assert "https://t.me/" in text


# --- ledger aggregates (read-only) -------------------------------------------


@pytest.mark.asyncio
async def test_partner_earnings_summary_groups_by_status():
    session = FakeSession(
        [("pending", Decimal("3.80")), ("vested", Decimal("3.80")), ("paid", Decimal("7.60"))]
    )
    earnings = await partner_earnings_summary(session, referrer_id=99)
    assert earnings.pending_usd == Decimal("3.80")
    assert earnings.vested_usd == Decimal("3.80")
    assert earnings.paid_usd == Decimal("7.60")
    # cancelled rows are simply absent from the grouped result -> excluded.
    assert earnings.accrued_usd == Decimal("15.20")
    # "Доступно к выводу" — confirmed but not yet paid out (issue #9).
    assert earnings.available_usd == Decimal("3.80")


@pytest.mark.asyncio
async def test_paid_invited_count_counts_distinct_paying_invitees():
    session = FakeSession(2)
    assert await paid_invited_count(session, referrer_id=99) == 2


@pytest.mark.asyncio
async def test_paid_invited_count_zero_when_none():
    session = FakeSession(None)
    assert await paid_invited_count(session, referrer_id=99) == 0


@pytest.mark.asyncio
async def test_invited_user_count_returns_zero_when_none():
    session = FakeSession(0)
    assert await invited_user_count(session, referrer_id=99) == 0


@pytest.mark.asyncio
async def test_invited_friends_with_earnings_marks_paid_flag():
    session = FakeSession(
        [
            (10, "friend_a", "A", NOW, Decimal("3.80")),
            (11, None, None, NOW, Decimal("0")),
        ]
    )
    friends = await invited_friends_with_earnings(session, referrer_id=99)
    assert friends[0].has_paid is True
    assert friends[0].accrued_usd == Decimal("3.80")
    assert friends[1].has_paid is False


@pytest.mark.asyncio
async def test_partner_overview_combines_queries_in_order():
    session = FakeSession(
        [("pending", Decimal("3.80"))],  # earnings
        2,  # invited_user_count
        1,  # paid_invited_count
        [(10, "friend_a", "A", NOW, Decimal("3.80"))],  # friends
    )
    overview = await partner_overview(session, referrer_id=99, friends_limit=10)
    assert overview.invited_count == 2
    assert overview.paid_invited_count == 1
    assert overview.earnings.accrued_usd == Decimal("3.80")
    assert len(overview.friends) == 1


# --- B12 / criteria 5 & 6: money wording, no overpromise ----------------------


def test_partner_text_uses_money_program_rules_not_bonus_days():
    user = SimpleNamespace(referral_code="ABC123")
    overview = PartnerOverview(
        invited_count=2,
        earnings=PartnerEarnings(pending_usd=Decimal("3.80")),
        friends=[
            InvitedFriend(10, "friend_a", "A", NOW, Decimal("0")),
        ],
    )
    text = _partner_text(user, overview)
    # money-partner program wording present...
    assert "20%" in text
    assert "12 месяцев" in text
    assert "$100" in text
    # ...current numbers are labelled as ledger data, not a promised total...
    assert "по данным реестра" in text
    # ...and the legacy bonus-days framing is gone.
    assert "бонус" not in text.lower()


def test_partner_text_shows_paid_count_and_accrued_vs_available_split():
    # issue #9: numbers must be visible the moment a friend pays, and the
    # confirmed "Доступно к выводу" balance is split from the accrued total.
    user = SimpleNamespace(referral_code="ABC123")
    overview = PartnerOverview(
        invited_count=3,
        paid_invited_count=2,
        earnings=PartnerEarnings(
            pending_usd=Decimal("3.80"),
            vested_usd=Decimal("7.60"),
            paid_usd=Decimal("0"),
        ),
        friends=[],
    )
    text = _partner_text(user, overview)
    assert "Приглашено: <b>3</b>" in text
    assert "Оплатило: <b>2</b>" in text
    # accrued total includes the still-pending row ($3.80 + $7.60 = $11.40)
    assert "Накоплено всего" in text
    assert "$11.40" in text
    # available to withdraw is the vested-only balance
    assert "Доступно к выводу: <b>$7.60</b>" in text
    # pending is surfaced as a separate, clearly-labelled line
    assert "ожидает подтверждения" in text
    assert "$3.80" in text


def test_fmt_usd_two_decimals():
    assert _fmt_usd(Decimal("3.8")) == "$3.80"
    assert _fmt_usd(None) == "$0.00"


# --- B11 / C02 / C06: admin relationships endpoint ----------------------------


@pytest.mark.asyncio
async def test_relationships_endpoint_shapes_a_to_b_with_commission_summary():
    referee = SimpleNamespace(id=10, username="invitee", first_name="B", joined_at=NOW)
    referrer = SimpleNamespace(id=99, username="inviter", first_name="A")
    attribution = SimpleNamespace(
        source="telegram_deeplink", attributed_at=NOW, review_status="clear"
    )
    db = FakeSession(
        1,  # total count
        [(referee, referrer, attribution, 1, Decimal("3.80"), Decimal("0"), Decimal("0"))],
    )

    page = await list_relationships(db, SimpleNamespace(), limit=100, offset=0)

    assert isinstance(page, RelationshipsPage)
    assert page.total == 1
    row = page.items[0]
    assert row.referrer_id == 99 and row.referrer_username == "inviter"
    assert row.referee_id == 10 and row.referee_username == "invitee"
    assert row.source == "telegram_deeplink"
    assert row.commission_count == 1
    assert row.accrued_usd == 3.80


@pytest.mark.asyncio
async def test_partners_endpoint_shapes_per_partner_stats():
    referrer = SimpleNamespace(id=99, username="inviter", first_name="A")
    db = FakeSession(
        1,  # total partners
        [
            (
                referrer,
                3,  # invited_count
                2,  # paid_count
                Decimal("11.40"),  # accrued (incl. pending)
                Decimal("3.80"),  # pending
                Decimal("7.60"),  # available / vested
                Decimal("0"),  # paid
            )
        ],
    )

    page = await list_partner_stats(db, SimpleNamespace(), limit=100, offset=0)

    assert isinstance(page, PartnerStatsPage)
    assert page.total == 1
    row = page.items[0]
    assert row.referrer_id == 99 and row.referrer_username == "inviter"
    assert row.invited_count == 3
    assert row.paid_count == 2
    assert row.accrued_usd == 11.40
    assert row.available_usd == 7.60
    assert row.pending_usd == 3.80


@pytest.mark.asyncio
async def test_relationships_endpoint_handles_missing_attribution():
    referee = SimpleNamespace(id=12, username=None, first_name=None, joined_at=NOW)
    referrer = SimpleNamespace(id=99, username="inviter", first_name="A")
    db = FakeSession(
        1,
        [(referee, referrer, None, 0, Decimal("0"), Decimal("0"), Decimal("0"))],
    )

    page = await list_relationships(db, SimpleNamespace(), limit=100, offset=0)
    row = page.items[0]
    assert row.source is None
    assert row.attributed_at is None
    assert row.review_status is None
    assert row.accrued_usd == 0.0
