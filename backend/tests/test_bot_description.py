"""GK-413 (image1) / GK-448: guard Grant's approved pre-Start bot description.

The description is applied on bot startup via setMyDescription. Telegram rejects
descriptions longer than 512 characters and does not render HTML there, so these
invariants must hold or the bot would silently fail to update its profile.

GK-448 replaced the selling copy with a pre-launch line. The tests that pinned
the old copy's shape (⭐, a /start pointer) are gone with it — they described
that text, not this field — and the ones that remain describe the field itself
plus the reason the new line exists.
"""

from app.bot.main import BOT_DESCRIPTION

GRANT_DESCRIPTION_2026_08_16 = "Официальный бот Сообщества Membership Club. Открытие скоро."


def test_bot_description_within_telegram_limit():
    # Telegram setMyDescription hard limit is 512 characters.
    assert len(BOT_DESCRIPTION) <= 512


def test_bot_description_is_grants_text_character_for_character():
    assert BOT_DESCRIPTION == GRANT_DESCRIPTION_2026_08_16


def test_bot_description_sells_nothing_before_launch():
    """The field GK-443's hold cannot reach.

    Telegram shows this before any update exists, so no handler can gate it. It
    is the one surface that could still pitch a subscription to a member of a
    community that is free until 15.09, which is why Grant took the price out.
    """
    assert "долларов" not in BOT_DESCRIPTION
    assert "$" not in BOT_DESCRIPTION
    assert "💎" not in BOT_DESCRIPTION


def test_bot_description_has_no_fly_agaric():
    # GK-419: 🍄 was replaced by ⭐; a custom emoji is impossible in this field
    # (setMyDescription takes a plain string with no parse_mode/entities). The
    # current line carries no emoji at all, but 🍄 must not return with the next one.
    assert "🍄" not in BOT_DESCRIPTION


def test_bot_description_is_plain_text():
    # Bot descriptions are plain text — no HTML tags (unlike message copy).
    assert "<b>" not in BOT_DESCRIPTION
    assert "</b>" not in BOT_DESCRIPTION
