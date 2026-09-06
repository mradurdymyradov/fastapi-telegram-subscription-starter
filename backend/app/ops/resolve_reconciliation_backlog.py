"""GK-430 — drain the reconciliation backlog that predates condition identity.

306 open items across 62 runs, 6 ever resolved. Not a backlog of 306 problems:
a backlog of a few conditions re-detected every night for two months, because
resolving an item only ever silenced that one row in that one run. GK-426 stops
the largest generator and `load_condition_history` makes a resolution stick from
now on. Neither one drains what is already there. This does.

It is deliberately a script and not an endpoint. Closing hundreds of findings in
one action is not something a panel should offer with a single button, and the
triage that justifies it — each item is historical test data, the hand-fulfilled
16.07 Lava payment, or a pre-verification USDT claim — happened on paper first.

**Dry run is the default.** Writing needs `--apply`, an `--admin-id` that exists
(the resolution is attributed to a real person, like every resolution made in
the panel), and a `--note` saying why. Filters let it be run in explainable
batches rather than one indiscriminate sweep — preferred, since a note that
covers everything explains nothing.

Usage, from inside the api container:

    python -m app.ops.resolve_reconciliation_backlog
    python -m app.ops.resolve_reconciliation_backlog \\
        --issue-type stripe_provider_terminal_with_local_access \\
        --apply --admin-id 3 --note "renewal artefact, released by GK-426"

Verify afterwards by triggering one run from the panel: the conditions you
resolved should come back as `resolved`, not as new open rows.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminUser, ReconciliationItem, ReconciliationRun, utcnow
from app.db.session import async_session

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ops.resolve_reconciliation_backlog",
        description="Resolve standing reconciliation items in explainable batches.",
    )
    parser.add_argument("--provider", choices=["stripe", "lava", "usdt"], default=None)
    parser.add_argument("--issue-type", default=None, help="exact issue_type to close")
    parser.add_argument(
        "--created-before",
        default=None,
        help="ISO date/datetime; only items created before it (e.g. 2026-08-15)",
    )
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    parser.add_argument("--admin-id", type=int, default=None, help="admin the resolution is attributed to")
    parser.add_argument("--note", default=None, help="why these are being closed")
    return parser


def _parse_created_before(raw: str | None) -> datetime | None:
    if not raw:
        return None
    value = datetime.fromisoformat(raw)
    return value if value.tzinfo else value.replace(tzinfo=utcnow().tzinfo)


async def select_open_items(
    session: AsyncSession,
    *,
    provider: str | None = None,
    issue_type: str | None = None,
    created_before: datetime | None = None,
) -> list[ReconciliationItem]:
    query = select(ReconciliationItem).where(ReconciliationItem.status == "open")
    if provider:
        query = query.where(ReconciliationItem.provider == provider)
    if issue_type:
        query = query.where(ReconciliationItem.issue_type == issue_type)
    if created_before:
        query = query.where(ReconciliationItem.created_at < created_before)
    query = query.order_by(ReconciliationItem.id.asc())
    return list((await session.execute(query)).scalars().all())


def render_breakdown(items: list[ReconciliationItem]) -> str:
    """What is about to be closed, grouped the way a human would check it."""
    if not items:
        return "Nothing matches. No open items to resolve."

    by_condition = Counter(
        (item.provider, item.issue_type, item.severity) for item in items
    )
    runs = {item.run_id for item in items}
    lines = [
        f"{len(items)} open item(s) across {len(runs)} run(s), "
        f"{len(by_condition)} distinct condition type(s):",
        "",
    ]
    width = max(len(f"{p}/{t}") for p, t, _ in by_condition)
    for (provider, issue_type, severity), count in by_condition.most_common():
        lines.append(f"  {f'{provider}/{issue_type}':<{width}}  {severity:<8}  {count:>4} row(s)")
    return "\n".join(lines)


async def resolve_items(
    session: AsyncSession,
    items: list[ReconciliationItem],
    *,
    admin_id: int,
    note: str,
    now: datetime | None = None,
) -> int:
    resolved_at = now or utcnow()
    for item in items:
        item.status = "resolved"
        item.resolve_note = note
        item.resolved_by_admin_id = admin_id
        item.resolved_at = resolved_at

    for run_id in sorted({item.run_id for item in items}):
        run = (
            await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            continue
        run.open_items_count = int(
            (
                await session.execute(
                    select(func.count(ReconciliationItem.id)).where(
                        ReconciliationItem.run_id == run_id,
                        ReconciliationItem.status == "open",
                    )
                )
            ).scalar_one()
            or 0
        )
    return len(items)


async def _run(args: argparse.Namespace) -> int:
    created_before = _parse_created_before(args.created_before)

    async with async_session() as session:
        items = await select_open_items(
            session,
            provider=args.provider,
            issue_type=args.issue_type,
            created_before=created_before,
        )
        print(render_breakdown(items))

        if not items:
            return 0

        if not args.apply:
            print(
                "\nDry run. Nothing was written. To close these, re-run with:"
                "\n  --apply --admin-id <id> --note \"<why>\""
            )
            return 0

        if args.admin_id is None or not (args.note or "").strip():
            print(
                "\nREFUSED: --apply needs both --admin-id and --note. A resolution "
                "without an owner and a reason is the state we are cleaning up.",
                file=sys.stderr,
            )
            return 2

        admin = (
            await session.execute(select(AdminUser).where(AdminUser.id == args.admin_id))
        ).scalar_one_or_none()
        if admin is None:
            print(f"\nREFUSED: no admin_users row with id={args.admin_id}.", file=sys.stderr)
            return 2

        count = await resolve_items(
            session, items, admin_id=admin.id, note=args.note.strip()
        )
        await session.commit()
        print(f"\nResolved {count} item(s) as {admin.email}.")
        print(
            "Now trigger one reconciliation run from the panel and confirm these "
            "come back resolved rather than as new open rows."
        )
        return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_run(build_arg_parser().parse_args(argv)))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
