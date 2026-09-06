"""GK-484 — the gate that stands in front of arming the hourly expiry removals.

`kick_expired_job` is the loudest thing the bot does to a member: it ban+unbans
them out of **every** configured Telegram resource and then DMs them a
subscription pitch, hourly and unattended. GK-460 put two flags in front of it,
both off in code, so nothing fires by accident any more. That changed *when*
this matters, not whether — the hour `ENABLE_EXPIRY_REMOVALS` is set, every row
that is already due is processed. Three rows were due on 2026-08-23 and two of
them were the client's own manager.

So arming needs a gate, and this is it:

    docker exec -i membership_saas-api-1 python -m app.ops.expiry_removal_gate

Run it immediately before the `.env` edit rather than the evening before. The
set comes from `expire_subscriptions` — the job's own selector, imported rather
than re-expressed — so the gate and the job can never describe different sets,
which is the failure this file exists to prevent. **The exit code is the
answer:** 0 means nobody is due and it is safe to arm, 1 means somebody would be
removed within the hour, 2 means the command refused. Nothing is written unless
`--expire` is passed.

Clearing a row is a statement about a person, and there are exactly two of them:

* **The client's own team.** Mark it in the panel — Subscriptions → «Отметить» —
  which writes `is_comp` and an audit row naming the admin who decided. This
  command deliberately has **no** flag for that. GK-483 is explicit that the team
  list is Grant's to supply and must not be inferred from who happens to be in
  the data, and a script that could set the flag is a way to infer it at 3am.

* **A subscription that genuinely ended during the hold.** Record that here:

      python -m app.ops.expiry_removal_gate --expire 4 --admin-id 3 \\
          --note "gift ended 18.08; meets the 15.09 cutover with everyone else"

  It writes `status='expired'` and nothing else. In particular
  `access_revoked_at` stays NULL, because nobody was removed from Telegram — the
  member keeps the chat they are still sitting in and meets the 15.09 cutover
  with every other non-payer, which is the entire point of not letting the
  hourly job reach them three weeks early. That makes this the **second** writer
  of `status='expired'`; `mark_access_revoke_result` is the first and writes it
  only after a removal actually succeeded. The two are distinguishable in the
  data by exactly the field that means "removed": this one leaves it empty.

Only a row the gate itself returned can be expired, and the ids are the
operator's to name. "Which of these is an ex-member and which is staff nobody
has flagged yet" is not a question the data answers.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from sqlalchemy import select

# The refusal string is imported from the job rather than paraphrased for the
# same reason the due set is: an operator reading this is deciding whether to
# arm, and a gate that describes the arming rules in its own words is a second
# copy of them.
from app.bot.tasks import EXPIRY_REMOVAL_DM, _expiry_removals_refusal
from app.config import get_settings
from app.db.models import AdminUser, utcnow
from app.db.session import async_session
from app.services.audit import record as audit_record
from app.services.channel_access import configured_access_resources
from app.services.subscription import expire_subscriptions, subscription_access_ends_at

logger = logging.getLogger(__name__)

EXIT_CLEAR = 0
EXIT_ROWS_DUE = 1
EXIT_REFUSED = 2


class GateRefusal(Exception):
    """A write this command will not perform, with the reason to print."""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ops.expiry_removal_gate",
        description=(
            "Show every subscription the hourly removal job would act on, and "
            "refuse arming while any remain. Read-only unless --expire is given."
        ),
    )
    parser.add_argument(
        "--expire",
        type=int,
        nargs="+",
        metavar="SUB_ID",
        default=None,
        help="record these due rows as expired (they must be in the list above)",
    )
    parser.add_argument("--admin-id", type=int, default=None, help="admin the write is attributed to")
    parser.add_argument("--note", default=None, help="why this subscription is being closed")
    return parser


def _age(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "unknown"
    days = (now - then).days
    return f"{then:%Y-%m-%d} ({days} days ago)"


def render_arming_state(settings, resources) -> str:
    """What the job would do with its flags as they stand right now."""
    expect = (getattr(settings, "expiry_removals_expect_chats", "") or "").strip()
    reach = (
        ", ".join(f"{r.title} ({r.chat_id or 'invite-link only'})" for r in resources)
        or "nothing — no Telegram resource is configured"
    )
    # In the job's own order. `kick_expired_job` returns on the hold *before* it
    # reaches the arming check, so reporting the refusal alone would tell an
    # operator "WOULD REMOVE" during a hold that stops it dead (GK-443).
    if settings.enable_prelaunch_hold:
        verdict = "skips the sweep entirely — ENABLE_PRELAUNCH_HOLD is on (GK-443)"
    else:
        refusal = _expiry_removals_refusal(settings)
        verdict = f"refuses — {refusal}" if refusal else "WOULD REMOVE the rows below"
    return "\n".join(
        [
            "Arming state, read from this container's settings:",
            f"  ENABLE_PRELAUNCH_HOLD        = {bool(settings.enable_prelaunch_hold)}",
            f"  ENABLE_EXPIRY_REMOVALS       = {bool(settings.enable_expiry_removals)}",
            f"  EXPIRY_REMOVALS_EXPECT_CHATS = {expect or '(unset)'}",
            f"  a removal would reach: {reach}",
            f"  the job right now: {verdict}",
        ]
    )


def render_due(rows, *, now: datetime) -> str:
    """The due set, spelled out as what would happen to which person."""
    if not rows:
        return "\nDue for removal: none.\n"

    lines = [f"\nDue for removal: {len(rows)} row(s).\n"]
    for sub in rows:
        user = getattr(sub, "user", None)
        username = getattr(user, "username", None)
        handle = f"@{username}" if username else "(no username)"
        lines += [
            f"  sub#{sub.id}  user#{getattr(sub, 'user_id', '?')}  "
            f"{handle} tg_id={getattr(user, 'tg_id', '?')}",
            f"          status={sub.status}  source={sub.source}  "
            f"is_comp={bool(getattr(sub, 'is_comp', False))}",
            f"          access ended {_age(subscription_access_ends_at(sub), now)}",
            "",
        ]
    lines += [
        "  Each of them would be banned+unbanned out of every resource named "
        "above, then sent:",
        f"    «{EXPIRY_REMOVAL_DM}»",
        "",
    ]
    return "\n".join(lines)


def render_resolutions() -> str:
    return "\n".join(
        [
            "Two ways to clear a row. Which one applies is a statement about the",
            "person, not about the data, so this command will not choose:",
            "",
            "  * The client's own team (Grant, Owner, the curators) — mark it in the",
            "    panel: Subscriptions → «Отметить». That writes `is_comp` and an audit",
            "    row naming who decided. GK-483 is explicit that this list is Grant's",
            "    to supply, so there is deliberately no flag here that sets it.",
            "",
            "  * A subscription that genuinely ended during the hold — record it:",
            "      --expire <sub id> --admin-id <admin id> --note \"<why>\"",
            "    That writes `status='expired'` only. Nobody is removed from Telegram",
            "    and `access_revoked_at` stays empty, so the member keeps the chat they",
            "    are in and meets the 15.09 cutover with every other non-payer.",
            "",
        ]
    )


def render_verdict(rows) -> str:
    if not rows:
        return (
            "GATE CLEAR — no subscription is due. Arming ENABLE_EXPIRY_REMOVALS now\n"
            "removes nobody. Paste this output into GK-484 and take the arming step in\n"
            "docs/runbooks/launch_batch_deploy.md."
        )
    return (
        f"GATE RED — {len(rows)} row(s) would be removed within the hour of arming.\n"
        "Do not set ENABLE_EXPIRY_REMOVALS until this prints zero."
    )


def expire_rows(rows, ids, *, note: str, now: datetime | None = None) -> list:
    """Mark named due rows as expired. Returns the rows written.

    Every guard here answers the same question — is this row one the gate itself
    just returned — because that is what keeps a status write out of the reach of
    a mistyped id. `is_comp` cannot appear in that set (the job's query excludes
    it) and is checked anyway: this is the one command that writes a status onto
    a row nobody looked at in a browser first.
    """
    now = now or utcnow()
    by_id = {sub.id: sub for sub in rows}
    unknown = [sub_id for sub_id in ids if sub_id not in by_id]
    if unknown:
        due = ", ".join(str(i) for i in sorted(by_id)) or "none"
        raise GateRefusal(
            f"not due for removal: {', '.join(str(i) for i in unknown)}. "
            f"Only a row this gate returned can be expired here (due: {due}). "
            "A row that is not due is not this command's to touch."
        )

    written = []
    for sub_id in ids:
        sub = by_id[sub_id]
        if getattr(sub, "is_comp", False):
            raise GateRefusal(
                f"sub#{sub_id} is flagged as the client's team. Clearing that is the "
                "panel's «Снять», not an expiry."
            )
        ends_at = subscription_access_ends_at(sub)
        if ends_at is None or ends_at > now:
            raise GateRefusal(
                f"sub#{sub_id}'s access window has not closed ({ends_at}). Refusing "
                "to write an expiry onto a live subscription."
            )
        sub.status = "expired"
        # Deliberately NOT setting access_revoked_at: that field means "removed
        # from Telegram", and nobody was. See the module docstring.
        written.append(sub)
        logger.info("GK-484 gate: sub#%s -> expired (%s)", sub_id, note)
    return written


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    resources = configured_access_resources()

    # Printed before the database is touched, so a host that cannot reach
    # Postgres still tells the operator what the flags say.
    print(render_arming_state(settings, resources))

    async with async_session() as session:
        try:
            rows = await expire_subscriptions(session)
        except Exception:
            # Never let a database failure read as "clear". It cannot: the
            # exception would exit 1, which is also "rows are due" — so say
            # which one this is, in the sentence the operator is looking for.
            logger.exception("GK-484 gate: could not read the due set")
            print(
                "\nGATE ERROR — the due set could not be read, so this is NOT a "
                "clear result. Do not arm removals on it.",
                file=sys.stderr,
            )
            return EXIT_REFUSED

        now = utcnow()
        print(render_due(rows, now=now))

        if not args.expire:
            if rows:
                print(render_resolutions())
            await session.rollback()
            print(render_verdict(rows))
            return EXIT_CLEAR if not rows else EXIT_ROWS_DUE

        if args.admin_id is None or not (args.note or "").strip():
            print(
                "REFUSED: --expire needs --admin-id and --note. A subscription closed "
                "by nobody, for no recorded reason, is the state this task is cleaning up.",
                file=sys.stderr,
            )
            return EXIT_REFUSED

        admin = (
            await session.execute(select(AdminUser).where(AdminUser.id == args.admin_id))
        ).scalar_one_or_none()
        if admin is None:
            print(f"REFUSED: no admin_users row with id={args.admin_id}.", file=sys.stderr)
            return EXIT_REFUSED

        note = args.note.strip()
        try:
            written = expire_rows(rows, args.expire, note=note, now=now)
        except GateRefusal as refusal:
            await session.rollback()
            print(f"REFUSED: {refusal}", file=sys.stderr)
            return EXIT_REFUSED

        for sub in written:
            ended_at = subscription_access_ends_at(sub)
            await audit_record(
                session,
                actor_admin_id=admin.id,
                action="subscription.expired_without_removal",
                target_type="subscription",
                target_id=sub.id,
                details={
                    "user_id": sub.user_id,
                    "source": sub.source,
                    "access_ended_at": ended_at.isoformat() if ended_at else None,
                    "note": note[:500],
                    "tool": "app.ops.expiry_removal_gate",
                    "telegram_removal": "none — status only (GK-484)",
                },
            )
        await session.commit()
        print(
            f"Wrote status='expired' on {len(written)} row(s) as {admin.email}: "
            + ", ".join(f"sub#{sub.id}" for sub in written)
        )

        # Re-run the gate rather than assume: this is the line that gets pasted
        # into GK-484, and it has to come from the query, not from arithmetic.
        rows = await expire_subscriptions(session)
        print(render_due(rows, now=utcnow()))
        if rows:
            print(render_resolutions())
        print(render_verdict(rows))
        return EXIT_CLEAR if not rows else EXIT_ROWS_DUE


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(build_arg_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
