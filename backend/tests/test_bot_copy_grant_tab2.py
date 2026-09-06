"""GK-419: guard Grant's rewritten bot copy from his 24.07 doc, Tab 2.

Grant's copy is applied verbatim (see the project rule: never normalize his
punctuation or wording). These tests pin the phrases that actually changed, so a
later refactor cannot quietly restore the GK-413 wording.

What is left here is what Tab 2 settled and Tab 3 did not reopen: ⭐ instead of
🍄, the opening paragraph of the welcome, and the gift blurb. The bullet list,
the leaderboard ladder and «ℹ️ О сообществе» were rewritten again on 19.08 and
moved to `test_bot_copy_grant_tab3.py` — the assertions that used to live here
now contradict the shipped copy, which is the point of moving rather than
keeping them.
"""

from app.bot.handlers.gift import GIFT_INTRO
from app.bot.handlers.start import WELCOME


def test_welcome_uses_star_and_not_fly_agaric():
    # Grant: «Вместо 🍄 мухомора … В крайнем случае ⭐». The MycoTotems logo is a
    # custom emoji the bot may not be able to send (GK-420), so ⭐ ships now.
    assert "⭐" in WELCOME
    assert "🍄" not in WELCOME


def test_welcome_has_grants_new_opening():
    assert "Живое пространство работы с собой и инструменты, которые реально меняют жизнь." in WELCOME
    assert "Двадцать лет практики и сотни тысяч людей, прошедших через эту работу." in WELCOME
    # The GK-413 opening is gone.
    assert "проверенные на тысячах людей" not in WELCOME


def test_gift_intro_has_the_three_added_paragraphs():
    assert "Ссылка действует 30 дней." in GIFT_INTRO
    assert "Подписка начинается с того дня, когда получатель её активирует" in GIFT_INTRO
    assert "Подарок не продлевается автоматически." in GIFT_INTRO
    assert "Вопросы → /support" in GIFT_INTRO
    # «на 30 дней» moved out of the first paragraph into its own line.
    assert "одноразовую ссылку активации на 30 дней" not in GIFT_INTRO
