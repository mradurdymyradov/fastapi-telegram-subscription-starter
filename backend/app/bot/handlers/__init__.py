import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from app.bot.handlers import (
    cabinet,
    gift,
    hold,
    no_charge,
    portal,
    referral,
    start,
    subscription,
    support,
)
from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

FALLBACK_HINT = (
    "Не понял 🤔\n"
    "/start — открыть меню\n"
    "/cancel — сбросить текущий шаг"
)


async def unhandled_message(message: Message) -> None:
    await message.answer(FALLBACK_HINT)


def _private_callback(callback: CallbackQuery) -> bool:
    """GK-438: refuse callbacks fired from a button sitting in a group.

    `callback.message` can be absent (Telegram no longer has the message, or it
    is an `InaccessibleMessage`); those cannot be a live group keyboard, and
    dropping them would leave the button spinning forever (GK-421), so they pass.
    """
    chat = getattr(callback.message, "chat", None)
    return chat is None or chat.type == ChatType.PRIVATE


def _include_member_routers(parent: Router) -> None:
    """Attach the member-facing routers, in the order the flows depend on."""
    # Portal first: it owns the `?start=portal` deep link via a payload-scoped
    # CommandStart filter, so it must get first crack before start.py's catch-all
    # deep-link handler. Non-portal payloads fall through untouched.
    parent.include_router(portal.router)
    parent.include_router(start.router)
    # GK-486 BEFORE subscription, and this ordering is the whole point: the
    # router below claims the buying entry points for accounts that must never
    # be charged, so they resolve here instead of in `subscription.py`. It is
    # inert unless `NO_CHARGE_TG_IDS` names somebody. Ordering is only the
    # friendly half of that guard — the half that cannot be got wrong lives in
    # the checkout handlers themselves, see `no_charge.py`.
    parent.include_router(no_charge.router)
    parent.include_router(subscription.router)
    parent.include_router(cabinet.router)
    parent.include_router(referral.router)
    # Gift BEFORE support (GK-453). It was the other way round from GK-375 until
    # 2026-08-19, and that inversion is what Grant screenshotted: he tapped
    # «🎁 Подарить» and the bot answered «Вопрос получен. Куратор ответит вам».
    #
    # GK-375 had a reason for it — gift.py then owned `GiftFlow.waiting_recipient`,
    # a catch-all state that ate `/support`, so support had to be reachable from
    # inside it. That same commit removed the state (a gift no longer names its
    # recipient), and the ordering outlived the thing it was protecting. What it
    # protected *against* remained: support's two catch-alls claim all free text —
    # one in `SupportFlow.chatting`, one for members in no state at all — so
    # anything behind them that answers plain text is unreachable. That was
    # «🎁 Подарить» and `/gift`, and nothing else.
    #
    # gift.py now registers only exact matches (two entry points and four
    # `gift_*` callbacks), so it claims nothing support wants. Both fixes ship:
    # this order, and `MENU_BUTTON_TEXTS` excluded from the catch-alls
    # themselves, because ordering alone cannot save a button from a catch-all
    # that is registered earlier for an unrelated reason.
    parent.include_router(gift.router)
    parent.include_router(support.router)
    # Fallback LAST (GK-350): a private-chat message no handler above claimed
    # gets a hint instead of silence — stuck FSM states (BuyFlow:choosing_plan)
    # used to swallow free text, indistinguishable from a dead bot. Private
    # chats only: the bot must never chat back in the channel/practice group.
    fallback = Router(name="fallback")
    fallback.message.register(unhandled_message, F.chat.type == ChatType.PRIVATE)
    parent.include_router(fallback)


def _allowlist_filter(allowlist: frozenset[int]) -> Callable[[Message | CallbackQuery], bool]:
    """GK-446: the one predicate that decides who walks past the hold.

    `from_user` is optional on both event types (channel posts, and callbacks
    Telegram has only partially kept). `None not in allowlist` is the whole
    safety argument: an event we cannot attribute to a person is not admitted.
    """

    def _allowlisted(event: Message | CallbackQuery) -> bool:
        return getattr(getattr(event, "from_user", None), "id", None) in allowlist

    return _allowlisted


def _prelaunch_allowlist_gate(
    allowlist: frozenset[int],
    include: Callable[[Router], None] = _include_member_routers,
) -> Router:
    """GK-446: a router only the named Telegram user ids may enter.

    Grant asked to check the finished texts on a live bot before launch, and
    GK-443's hold is precisely what hides them: with it on, the greeting, the
    plans, the checkout and the portal link do not exist in the dispatcher. This
    gate gives those handlers back to a named few and to nobody else.

    Be honest about what it costs. GK-443's guarantee was structural — "no
    checkout is created by any path" was a fact about what was *registered*,
    which no later edit could weaken. Once this gate exists that guarantee holds
    only for everyone outside the list, and for them it now rests on a filter
    rather than on absence. So the filter sits at the root of its own router,
    where aiogram evaluates it before any child, and it is written to fail
    closed: an event with no user, or a user whose id is not in the set, does
    not enter, falls through to `hold.router` behind it, and meets the заглушка.
    An empty allowlist is not passed here at all — `setup_handlers()` skips the
    gate entirely, so the default deployment keeps GK-443 word for word.

    `include` is a seam, and it exists for one reason: the handler routers are
    module-level singletons that attach to a parent once per process, so a test
    that built this gate for real would collide with the dispatcher another test
    module has already assembled. Injecting stub children lets the gate itself —
    both filters, the real predicate, the ordering — be exercised instead of
    described. Production never passes it.
    """
    gate = Router(name="prelaunch_allowlist")
    allowlisted = _allowlist_filter(allowlist)
    gate.message.filter(allowlisted)
    gate.callback_query.filter(allowlisted)
    include(gate)
    return gate


def setup_handlers() -> Router:
    root = Router(name="root")
    # GK-438: the bot is an administrator in the production channel and practice
    # chat, which switches Telegram's privacy mode off — it therefore *sees*
    # every message posted there. Nothing member-facing may answer in a group.
    # On 2026-08-10 a `/start` sent inside the live practice chat made the bot
    # reply with `main_menu()`, and a non-selective ReplyKeyboardMarkup posted to
    # a group is shown to *all* its members: every "🎬 Открыть архив" tap then
    # went to the group and the bot pitched a paid subscription in public, three
    # weeks before paid access opens. Root-level filters are evaluated before any
    # child router, so this single guard covers every handler below — including
    # ones added later — and the stale inline keyboards already sitting in that
    # chat's history stop working too.
    root.message.filter(F.chat.type == ChatType.PRIVATE)
    root.callback_query.filter(_private_callback)
    # GK-443: while the pre-launch hold is on, this is the entire bot. The
    # return is the mechanism, not a shortcut — nothing below is registered, so
    # the handlers that create checkouts, issue portal links and open support
    # flows are absent from the dispatcher rather than merely shadowed. The
    # private-chat filters above still apply: the hold answers in DMs and stays
    # silent in the practice chat, exactly like everything else.
    #
    # GK-446 qualifies that in one direction only. With a non-empty allowlist
    # those handlers come back for the named ids, behind the gate's root filter,
    # so "absent from the dispatcher" becomes true of everyone *except* them.
    # With the allowlist empty — the default, and the shipped state — the
    # paragraph above stands word for word.
    if settings.enable_prelaunch_hold:
        # GK-446: a named few get the real bot so the finished texts can be
        # checked live. The gate goes FIRST and the hold stays LAST as the
        # catch-all, which is the safe direction: anything the gate does not
        # claim — including every event it cannot attribute to a user — lands on
        # the заглушка rather than on a checkout.
        allowlist = settings.prelaunch_hold_allowlist_ids
        if allowlist:
            logger.warning(
                "GK-446: pre-launch hold is on, but %d Telegram id(s) bypass it and "
                "get the full bot including live checkouts: %s",
                len(allowlist),
                ", ".join(str(uid) for uid in sorted(allowlist)),
            )
            root.include_router(_prelaunch_allowlist_gate(allowlist))
        root.include_router(hold.router)
        return root
    _include_member_routers(root)
    return root
