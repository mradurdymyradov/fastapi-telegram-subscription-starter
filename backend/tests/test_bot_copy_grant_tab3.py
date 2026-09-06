"""GK-452: guard Grant's rewritten bot copy from his 19.08 doc, Tab 3.

Three screens changed: the `/start` welcome, the partner leaderboard, and
«ℹ️ О сообществе». Grant's copy is applied verbatim — the standing project rule
is never to normalize his punctuation or wording — so these tests pin the
sentences *and the bold spans* he wrote, including the ones that read like
slips. `test_bot_copy_grant_tab2.py` still guards what Tab 3 left alone.
"""

from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.referral import _LB_GOALS, _LB_INTRO, _format_board
from app.bot.handlers.start import WELCOME, about


def test_welcome_bullets_are_a_bold_headline_plus_an_explanation():
    bullets = [line for line in WELCOME.splitlines() if line.startswith("✅")]

    assert len(bullets) == 7
    assert bullets[0] == (
        "✅ <b>Безлимитные проработки в чате практики.</b> В любой момент вы "
        "можете попросить о помощи, и опытный гипнотерапевт возьмёт вас в "
        "работу. Не через месяц по записи и без дополнительных оплат. Одной "
        "проработки бывает достаточно, чтобы жизнь развернулась"
    )
    # Grant: «жирным идут только названия пунктов» — every bullet opens with its
    # own bold headline and continues in plain text.
    assert all(line.startswith("✅ <b>") and "</b> " in line for line in bullets)
    # GK-477: Grant replaced one sentence of his own Tab 3 copy on 21.08.
    assert "не за отдельные деньги" not in WELCOME
    # The GK-419 one-liners the explanations replaced are gone.
    assert "Такого доступа нет больше нигде" not in WELCOME
    assert "✅ Полный архив Гипно-Коучинга" not in WELCOME
    assert "с которыми работает Павел" not in WELCOME


def test_welcome_bolds_the_bullet_headlines_and_keeps_the_two_it_already_had():
    # Grant's «жирным идут только названия пунктов» names the bold *he added* —
    # the seven bullet headlines. The title and «💎 Подписка» were already bold
    # under GK-419 and his doc does not restate them, so they carry over. Nine.
    assert WELCOME.startswith("<b>Закрытое Сообщество Павла Дмитриева | Membership Club</b> ⭐\n\n")
    assert WELCOME.endswith("Нажмите <b>💎 Подписка</b>, чтобы войти 👇")
    assert WELCOME.count("<b>") == 9
    assert WELCOME.count("</b>") == 9


def test_welcome_gains_the_life_is_already_happening_paragraph():
    assert (
        "Внутри уже идёт жизнь. Пока вы читаете это, кто-то там делает свой следующий шаг."
        in WELCOME
    )


def test_welcome_referral_line_is_shortened():
    assert "🔹 Друзьям скидка 20% на первый месяц" in WELCOME
    assert "Приглашайте друзей, для них скидка" not in WELCOME


def test_leaderboard_intro_keeps_grants_missing_full_stop():
    # «Лучших ждут награды, которых нет в подписке» — no full stop in his doc.
    # Adding one would be exactly the normalization GK-413 had to be redone for.
    assert _LB_INTRO.endswith("Лучших ждут награды, которых нет в подписке")
    assert "не купить за деньги" not in _LB_INTRO


def test_leaderboard_rewards_moved_up_a_rung():
    assert _LB_GOALS == (
        "<b>Цели и награды</b>\n"
        "🥉 50 приглашённых, обучение в Sacred Mushroom University, пакет Премиум\n"
        "🥈 75 приглашённых, участие в ретрите со скидкой 50 процентов\n"
        "🥇 100 приглашённых, личная сессия с Павлом, 60 минут один на один"
    )


def test_leaderboard_keeps_everything_below_the_ladder():
    # Grant closed Tab 3's leaderboard block with «….дальше все как есть сейчас»,
    # so the live top and the closing line are unchanged.
    text = _format_board([])

    assert "🏆 <b>Лидерборд партнёров</b>" in text
    assert "<b>Текущий топ</b>" in text
    assert "Приглашайте друзей по вашей партнёрской ссылке и занимайте верхние строки." in text


@pytest.mark.asyncio
async def test_about_gains_the_membership_title_and_the_outcome_paragraph():
    message = AsyncMock()

    await about(message)

    text = message.answer.await_args.args[0]
    assert text.startswith("<b>Закрытое Сообщество Павла Дмитриева | Membership Club ⭐</b>\n\n")
    assert "К чему это приводит. Уходит то, что держало годами." in text
    assert "Для тех, кто идёт глубже, это становится профессией и делом жизни." in text
    assert (
        "Главное, что здесь есть: вы идёте не в одиночку, и путь, который в "
        "одиночку занимает годы, здесь проходится быстрее." in text
    )
    assert text.endswith("Вопросы → /support")
    # Dropped on 19.08.
    assert "Никого никуда не тянут." not in text
    assert "Главное, что здесь есть, вы идёте не в одиночку." not in text
