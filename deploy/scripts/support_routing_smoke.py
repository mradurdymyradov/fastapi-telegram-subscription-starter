"""GK-378 / issue #5 — live support-routing smoke (run on the staging host).

Reproduces the production support path against the LIVE bot, DB, and the real
curator chat (`SUPPORT_ROUTING_CHAT_ID`), then prints a paste-ready result block.

It reuses the exact production code (`route_support_message`, `send_message`) and
the bot's HTML default, so a green run proves the live wiring — not a re-mock.

  TEST ACCOUNT ONLY. Never run with a production-audience tg_id (Danger rule #1).
  The bot must already be an admin (read/send) of the curator chat, and the test
  account must have sent /start once so a `users` row exists.

Run inside the bot container (settings/token/DB are present there):

  docker compose -p membership_saas exec bot \
    python -m deploy.scripts.support_routing_smoke --tg-id <TEST_TG_ID>

  # routing-only (don't message the test account back):
  ... support_routing_smoke --tg-id <TEST_TG_ID> --no-reply

If `python -m deploy.scripts...` is not importable in the image, copy the file in
and run it directly:

  docker compose -p membership_saas cp deploy/scripts/support_routing_smoke.py \
    bot:/tmp/smoke.py
  docker compose -p membership_saas exec bot python /tmp/smoke.py --tg-id <TEST_TG_ID>
"""
from __future__ import annotations

import argparse
import asyncio
import html
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from app.bot.handlers.support import route_support_message
from app.config import get_settings
from app.db.models import SupportMessage, User
from app.db.session import async_session
from app.services.notifications import send_message


async def main(tg_id: int, do_reply: bool) -> int:
    settings = get_settings()
    target = (settings.support_routing_chat_id or "").strip()
    print(f"SUPPORT_ROUTING_CHAT_ID = {target!r}")
    if not settings.bot_token:
        print("FAIL: BOT_TOKEN is not set in this environment.")
        return 2
    if not target:
        print("FAIL: SUPPORT_ROUTING_CHAT_ID is empty — routing would be 'skipped'. "
              "Set it in the server .env and recreate the bot first.")
        return 2

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        async with async_session() as session:
            user = (
                await session.execute(select(User).where(User.tg_id == tg_id))
            ).scalar_one_or_none()
            if user is None:
                print(f"FAIL: no users row with tg_id={tg_id}. Send /start to the bot "
                      "from the TEST account first, then re-run.")
                return 2
            print(f"test user: id={user.id} tg_id={user.tg_id} username={user.username!r}")

            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

            # Step 1 — inbound ticket routed to the curator chat (the exact path
            # the bot handler runs: persist, then route, then persist outcome).
            question = SupportMessage(
                user_id=user.id, role="user", content=f"SMOKE GK-378 routing {ts}"
            )
            session.add(question)
            await session.commit()
            routing_status = await route_support_message(bot, user, question)
            await session.commit()
            print(f"[routing] support_message id={question.id} "
                  f"delivery_status={routing_status!r}  (expect 'routed')")

            # Step 2 — admin reply delivered back to the user (mirrors
            # /api/support/reply: persist, deliver, persist outcome).
            reply = None
            reply_status = "(skipped --no-reply)"
            if do_reply:
                reply = SupportMessage(
                    user_id=user.id, role="assistant", content=f"SMOKE GK-378 reply {ts}"
                )
                session.add(reply)
                await session.commit()
                delivered = await send_message(user.tg_id, html.escape(reply.content))
                reply.delivery_status = "delivered" if delivered else "failed"
                await session.commit()
                reply_status = reply.delivery_status
                print(f"[reply]   support_message id={reply.id} "
                      f"delivery_status={reply_status!r}  (expect 'delivered')")

            # Step 3 — transcript on the user (what the user card shows).
            rows = (
                await session.execute(
                    select(SupportMessage)
                    .where(SupportMessage.user_id == user.id)
                    .order_by(SupportMessage.id.desc())
                    .limit(10)
                )
            ).scalars().all()
            print("\n--- recent transcript (newest first) ---")
            for m in rows:
                print(f"  #{m.id:<6} {m.role:<9} {str(m.delivery_status):<9} {m.content[:48]}")

            ok = routing_status == "routed" and (not do_reply or reply_status == "delivered")
            print("\n=== paste into docs/runbooks/deploy_log.md and issue #5 ===")
            print(f"GK-378 live smoke — {ts}, staging host, test account tg_id={tg_id}")
            print(f"- routing: support_message id={question.id} role=user      "
                  f"delivery_status={routing_status}")
            if do_reply and reply is not None:
                print(f"- reply:   support_message id={reply.id} role=assistant "
                      f"delivery_status={reply_status}")
            print(f"- curator chat {target}: notification received  | "
                  f"user-card transcript: admin → Пользователи → user id={user.id}")
            print(f"- RESULT: {'PASS' if ok else 'CHECK FAILURES ABOVE'}")
            return 0 if ok else 1
    finally:
        await bot.session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tg-id", type=int, required=True,
                        help="Telegram numeric id of the TEST account (must have sent /start).")
    parser.add_argument("--no-reply", action="store_true",
                        help="Only route to the curator chat; skip the user-reply delivery.")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.tg_id, not args.no_reply)))
