"""GK-016 — turn the two free communities into paid ones, on 15.09.

Both production resources are existing free communities: ~4.5k in the channel,
~2.3k in the practice chat, joined over years. On 15 September everyone who has
not paid comes out (Grant moved the date from 11.09 on 2026-08-16, together with
the 29.08 sales start; the code never encoded either date, only the runbook did). The Bot API cannot enumerate them — `getChatMember` answers
about one id at a time and there is no "list members" call — so the roster has
to come from outside: Grant exports it himself.

What this module does with that export:

    roster file  ->  parse  ->  classify against the DB  ->  report  ->  remove

The classification is not new logic. "Paid" here means exactly what it means to
the hourly expiry job and to the member portal — `has_subscription_access`, the
same predicate, including provider grace and cancel-at-period-end. A member who
would keep the channel tomorrow keeps it on 15.09.

**Dry run is the default and cannot be skipped by accident.** Removal needs
`--apply` *and* `--confirm REMOVE-FOR-REAL` *and* an `--expect-chats` that names
**every** community a removal reaches, so a `.env` still pointing at staging — or
already repointed at production when you thought otherwise — aborts instead of
emptying the wrong one. It also refuses to run at all while the pre-launch hold
is on, because nobody has paid yet under a hold and the answer would be "remove
everyone".

`--expect-chats`, plural, is GK-470 and it is not cosmetic. Removal goes through
`kick_user`, which bans from every configured access resource — the channel *and*
the practice chat. The flag used to be `--expect-channel` and was checked against
`PRIVATE_CHANNEL_ID` alone, so an operator confirmed one room (~4.5k members) and
the command emptied two (~4.5k + ~2.3k). What is confirmed now is the set the
removal actually reaches, compared for equality in both directions: a chat that
was named and then repointed fails, and a chat that appeared without ever being
named fails too. The old flag is still parsed, and refuses with the command to
run instead — the runbooks and saved command lines still carry it.

Usage:

    python -m app.ops.cutover --roster members.csv --allowlist keep.txt
    python -m app.ops.cutover --roster members.csv --allowlist keep.txt \\
        --apply --confirm REMOVE-FOR-REAL \\
        --expect-chats -1001945266701 -1002368292795
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings, parse_tg_chat_id_list
from app.db.models import Subscription, User, utcnow
from app.services.channel_access import KickResult, configured_access_resources, kick_user
from app.services.subscription import ACCESS_HOLDING_STATUSES, has_subscription_access

logger = logging.getLogger(__name__)
settings = get_settings()

CONFIRM_TOKEN = "REMOVE-FOR-REAL"

# Verdicts. `REMOVE` is the only one that touches Telegram.
KEEP_ALLOWLISTED = "keep_allowlisted"
KEEP_PAID = "keep_paid"
REMOVE = "remove"
UNRESOLVED = "unresolved"

# Telegram ids are large; a stray "2024" in a CSV is a year, not a member.
_MIN_TG_ID = 10_000
_ID_RE = re.compile(r"\b\d{5,}\b")
_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{4,32})\b")
_PEER_RE = re.compile(r"^user(\d{5,})$")

_ID_KEYS = ("tg_id", "telegram_id", "user_id", "userid", "id", "peer_id", "from_id")
_USERNAME_KEYS = ("username", "user_name", "handle", "nickname", "login")

# ban+unban is a chat-admin action; Telegram's documented ceiling for those is
# well under one per second sustained. 1.0s keeps a 4.5k sweep inside ~75min
# and leaves headroom for the retry_after path below.
DEFAULT_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class RosterEntry:
    """One line of Grant's export, reduced to the two things we can act on."""

    raw: str
    tg_id: int | None = None
    username: str | None = None

    @property
    def label(self) -> str:
        if self.tg_id and self.username:
            return f"{self.tg_id} (@{self.username})"
        if self.tg_id:
            return str(self.tg_id)
        if self.username:
            return f"@{self.username}"
        return self.raw


@dataclass(frozen=True)
class RosterParse:
    entries: tuple[RosterEntry, ...]
    skipped: tuple[str, ...] = ()
    fmt: str = "unknown"


@dataclass(frozen=True)
class Verdict:
    entry: RosterEntry
    outcome: str
    reason: str
    tg_id: int | None = None


@dataclass(frozen=True)
class CutoverPlan:
    verdicts: tuple[Verdict, ...]
    skipped: tuple[str, ...] = ()
    fmt: str = "unknown"

    def of(self, outcome: str) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if v.outcome == outcome)

    @property
    def to_remove(self) -> tuple[Verdict, ...]:
        return self.of(REMOVE)

    @property
    def counts(self) -> dict[str, int]:
        counts = {
            KEEP_ALLOWLISTED: 0,
            KEEP_PAID: 0,
            REMOVE: 0,
            UNRESOLVED: 0,
        }
        for verdict in self.verdicts:
            counts[verdict.outcome] = counts.get(verdict.outcome, 0) + 1
        return counts


@dataclass
class RemovalReport:
    removed: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    rate_limited: int = 0
    stopped_early: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and self.stopped_early is None


# --------------------------------------------------------------------------
# Parsing
#
# The export format is Grant's, not ours, and the one thing here that cannot be
# unit-tested into correctness is a guess about its shape. So none is made: the
# parser tries JSON, then delimited text, then bare lines, and pulls ids and
# @usernames out of whatever it finds. A shape we did not anticipate degrades to
# the line scanner rather than to an exception.
# --------------------------------------------------------------------------


def parse_roster(text: str) -> RosterParse:
    stripped = text.strip()
    if not stripped:
        return RosterParse((), (), "empty")

    if stripped[0] in "[{":
        try:
            return _parse_json(json.loads(stripped))
        except (json.JSONDecodeError, ValueError):
            logger.warning("roster looks like JSON but did not parse; falling back to text")

    first_line = stripped.splitlines()[0]
    if any(sep in first_line for sep in (",", ";", "\t")):
        parsed = _parse_delimited(stripped)
        if parsed is not None:
            return parsed

    return _parse_lines(stripped)


def _parse_json(data: object) -> RosterParse:
    entries: list[RosterEntry] = []
    seen: set[tuple[int | None, str | None]] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            entry = _entry_from_mapping(node)
            if entry is not None:
                key = (entry.tg_id, entry.username)
                if key not in seen:
                    seen.add(key)
                    entries.append(entry)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return RosterParse(tuple(entries), (), "json")


def _entry_from_mapping(row: dict) -> RosterEntry | None:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}

    tg_id: int | None = None
    for key in _ID_KEYS:
        if key in lowered:
            tg_id = _coerce_id(lowered[key])
            if tg_id is not None:
                break

    username: str | None = None
    for key in _USERNAME_KEYS:
        if key in lowered:
            username = _normalize_username(lowered[key])
            if username:
                break

    if tg_id is None and not username:
        return None
    return RosterEntry(raw=json.dumps(row, ensure_ascii=False, sort_keys=True)[:200], tg_id=tg_id, username=username)


def _parse_delimited(text: str) -> RosterParse | None:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return None

    headers = {(name or "").strip().lower() for name in reader.fieldnames}
    if not (headers & set(_ID_KEYS) or headers & set(_USERNAME_KEYS)):
        # A delimited file whose header names nothing we recognise is more
        # safely handled by the line scanner than by column guesswork.
        return None

    entries: list[RosterEntry] = []
    skipped: list[str] = []
    for row in reader:
        clean = {k: v for k, v in row.items() if k is not None}
        entry = _entry_from_mapping(clean)
        if entry is None:
            joined = ",".join(str(v) for v in clean.values() if v)
            if joined.strip():
                skipped.append(joined[:200])
            continue
        entries.append(entry)
    return RosterParse(tuple(entries), tuple(skipped), "csv")


def _parse_lines(text: str) -> RosterParse:
    entries: list[RosterEntry] = []
    skipped: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        peer = _PEER_RE.match(raw)
        tg_id = int(peer.group(1)) if peer else None
        if tg_id is None:
            id_match = _ID_RE.search(raw)
            tg_id = _coerce_id(id_match.group(0)) if id_match else None
        name_match = _USERNAME_RE.search(raw if raw.startswith("@") else f" {raw}")
        username = _normalize_username(name_match.group(1)) if name_match else None
        if username is None and tg_id is None and _looks_like_bare_username(raw):
            username = _normalize_username(raw)
        if tg_id is None and not username:
            skipped.append(raw[:200])
            continue
        entries.append(RosterEntry(raw=raw[:200], tg_id=tg_id, username=username))
    return RosterParse(tuple(entries), tuple(skipped), "lines")


def _looks_like_bare_username(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value))


def _coerce_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if abs(value) >= _MIN_TG_ID else None
    if isinstance(value, str):
        text = value.strip()
        peer = _PEER_RE.match(text)
        if peer:
            return int(peer.group(1))
        try:
            number = int(text)
        except ValueError:
            return None
        return number if abs(number) >= _MIN_TG_ID else None
    return None


def _normalize_username(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip().lstrip("@").strip()
    if not name or not re.fullmatch(r"[A-Za-z0-9_]{4,32}", name):
        return None
    return name.lower()


def parse_allowlist(text: str) -> tuple[frozenset[int], frozenset[str]]:
    """Ids and usernames that are never removed, whatever the DB says.

    Admins and owners belong here even when they have never paid, and so does
    anyone Grant names as an exception. Parsed with the same scanner as the
    roster so a copied line from one file works in the other.
    """
    parsed = parse_roster(text) if text.strip() else RosterParse(())
    ids = {entry.tg_id for entry in parsed.entries if entry.tg_id is not None}
    names = {entry.username for entry in parsed.entries if entry.username}
    return frozenset(ids), frozenset(n for n in names if n)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


async def build_plan(
    session: AsyncSession,
    parsed: RosterParse,
    *,
    allowlist_ids: Iterable[int] = (),
    allowlist_usernames: Iterable[str] = (),
    now: datetime | None = None,
) -> CutoverPlan:
    """Decide, per roster entry, keep or remove — using the live access predicate."""
    now = now or utcnow()
    allow_ids = frozenset(allowlist_ids)
    allow_names = frozenset(name.lower().lstrip("@") for name in allowlist_usernames)

    entries = parsed.entries
    by_id, by_username = await _load_users(session, entries)
    paid_user_ids = await _paid_user_ids(
        session,
        {user.id for user in (*by_id.values(), *by_username.values())},
        now,
    )

    verdicts: list[Verdict] = []
    for entry in entries:
        user = None
        if entry.tg_id is not None:
            user = by_id.get(entry.tg_id)
        if user is None and entry.username:
            user = by_username.get(entry.username)

        tg_id = entry.tg_id if entry.tg_id is not None else (user.tg_id if user else None)

        if (tg_id is not None and tg_id in allow_ids) or (
            entry.username and entry.username in allow_names
        ):
            verdicts.append(Verdict(entry, KEEP_ALLOWLISTED, "on the allowlist", tg_id))
            continue

        if user is not None and user.id in paid_user_ids:
            verdicts.append(Verdict(entry, KEEP_PAID, "has subscription access", tg_id))
            continue

        if tg_id is None:
            # A username the bot has never seen cannot be banned — the Bot API
            # takes a numeric id. Reported, never silently dropped.
            verdicts.append(
                Verdict(entry, UNRESOLVED, "username not known to the bot; no numeric id", None)
            )
            continue

        reason = "no subscription" if user is not None else "never used the bot"
        verdicts.append(Verdict(entry, REMOVE, reason, tg_id))

    return CutoverPlan(tuple(verdicts), parsed.skipped, parsed.fmt)


async def _load_users(
    session: AsyncSession, entries: Iterable[RosterEntry]
) -> tuple[dict[int, User], dict[str, User]]:
    ids = {entry.tg_id for entry in entries if entry.tg_id is not None}
    names = {entry.username for entry in entries if entry.username}

    by_id: dict[int, User] = {}
    by_username: dict[str, User] = {}

    for chunk in _chunked(sorted(ids), 1000):
        result = await session.execute(select(User).where(User.tg_id.in_(chunk)))
        for user in result.scalars().all():
            by_id[user.tg_id] = user

    for chunk in _chunked(sorted(n for n in names if n), 1000):
        result = await session.execute(
            select(User).where(func.lower(User.username).in_(chunk))
        )
        for user in result.scalars().all():
            if user.username:
                by_username[user.username.lower()] = user

    return by_id, by_username


async def _paid_user_ids(
    session: AsyncSession, user_ids: set[int], now: datetime
) -> frozenset[int]:
    """User ids holding access right now, by the same rule the expiry job uses."""
    if not user_ids:
        return frozenset()

    paid: set[int] = set()
    for chunk in _chunked(sorted(user_ids), 1000):
        result = await session.execute(
            select(Subscription).where(
                Subscription.user_id.in_(chunk),
                Subscription.status.in_(ACCESS_HOLDING_STATUSES),
                Subscription.access_revoked_at.is_(None),
            )
        )
        for sub in result.scalars().all():
            if has_subscription_access(sub, now):
                paid.add(sub.user_id)
    return frozenset(paid)


def _chunked(values: list, size: int) -> Iterable[list]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


async def collect_admin_ids(bot) -> frozenset[int]:
    """Chat administrators of every configured resource — read-only.

    Admins keep their place whether or not they ever paid, and asking Telegram
    beats maintaining a hand-written list that goes stale the first time Grant
    promotes someone.
    """
    from app.services.channel_access import configured_access_resources

    ids: set[int] = set()
    for resource in configured_access_resources():
        if not resource.chat_id:
            continue
        try:
            for member in await bot.get_chat_administrators(chat_id=resource.chat_id):
                user = getattr(member, "user", None)
                if user is not None and getattr(user, "id", None):
                    ids.add(user.id)
        except Exception as exc:  # noqa: BLE001 — a missing admin list must not kick anyone
            logger.warning("could not read administrators of %s: %s", resource.key, exc)
            raise
    return frozenset(ids)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def render_report(plan: CutoverPlan) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["outcome", "tg_id", "username", "reason", "raw"])
    for verdict in plan.verdicts:
        writer.writerow(
            [
                verdict.outcome,
                verdict.tg_id if verdict.tg_id is not None else "",
                verdict.entry.username or "",
                verdict.reason,
                verdict.entry.raw,
            ]
        )
    return buffer.getvalue()


def render_summary(plan: CutoverPlan) -> str:
    counts = plan.counts
    lines = [
        f"roster format          : {plan.fmt}",
        f"rows understood        : {len(plan.verdicts)}",
        f"rows skipped           : {len(plan.skipped)}",
        "",
        f"  keep — allowlisted   : {counts[KEEP_ALLOWLISTED]}",
        f"  keep — paid          : {counts[KEEP_PAID]}",
        f"  REMOVE               : {counts[REMOVE]}",
        f"  unresolved (no id)   : {counts[UNRESOLVED]}",
    ]
    if plan.skipped:
        lines += ["", "first skipped rows:"]
        lines += [f"  {row}" for row in plan.skipped[:5]]
    return "\n".join(lines)


def target_chat_ids(resources) -> tuple[int, ...]:
    """The chat ids a ban can actually land on, in the order it lands (GK-470).

    Read off `configured_access_resources` — the list `kick_user` itself loops —
    rather than assembled here from `PRIVATE_CHANNEL_ID` and `PRACTICE_CHAT_ID`,
    so a third community added later becomes a chat this command starts
    *demanding be named* instead of one it silently starts emptying.

    A resource configured by invite link alone has no chat id, and
    `_kick_user_from_resource` refuses it as a permanent failure, so it is not a
    target and must not be something the operator is asked to confirm.
    """
    return tuple(resource.chat_id for resource in resources if resource.chat_id)


def render_targets(resources) -> str:
    """Name every room before anybody is taken out of it.

    The summary below this says how many members. This says where they go from,
    which was the part an operator could previously only learn by reading
    `channel_access.py`.
    """
    if not resources:
        return "removal reaches        : nothing — no Telegram resource is configured"
    lines = [
        f"removal reaches        : {len(resources)} Telegram resource(s) — "
        "every one of them, for every member removed:"
    ]
    for resource in resources:
        where = (
            str(resource.chat_id)
            if resource.chat_id
            else "(no chat id configured — nobody can be removed there)"
        )
        lines.append(f"  {resource.title:<21}{where}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------


async def apply_removals(
    bot,
    plan: CutoverPlan,
    *,
    kick: Callable[[object, int], Awaitable[KickResult]] = kick_user,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    delay: float = DEFAULT_DELAY_SECONDS,
    limit: int | None = None,
    on_progress: Callable[[int, int, Verdict, KickResult], None] | None = None,
) -> RemovalReport:
    """Remove everyone the plan marked REMOVE, slowly, remembering failures.

    A `retry_after` is honoured once — Telegram means it, and racing past it
    only earns a longer one. Anything still failing is recorded with its id so
    the operator can feed the failures back in as a second, much smaller roster.
    """
    targets = plan.to_remove
    if limit is not None:
        targets = targets[:limit]

    report = RemovalReport()
    total = len(targets)
    try:
        for index, verdict in enumerate(targets, start=1):
            if verdict.tg_id is None:  # pragma: no cover — build_plan cannot produce this
                report.failed.append((0, f"{verdict.entry.label}: no numeric id"))
                continue

            result = await kick(bot, verdict.tg_id)
            if not result.success and result.retry_after:
                report.rate_limited += 1
                await sleep(result.retry_after + 1)
                result = await kick(bot, verdict.tg_id)

            if result.success:
                report.removed.append(verdict.tg_id)
            else:
                report.failed.append((verdict.tg_id, result.error or "unknown error"))

            if on_progress is not None:
                on_progress(index, total, verdict, result)

            if index < total and delay:
                await sleep(delay)
    except (KeyboardInterrupt, asyncio.CancelledError):
        completed = len(report.removed) + len(report.failed)
        report.stopped_early = (
            f"interrupted after {completed} of {total} confirmed member result(s); "
            f"{total - completed} result(s) remain unconfirmed"
        )

    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.ops.cutover",
        description="GK-016 free-to-paid cutover: classify an exported member roster, "
        "report who would lose access, and — only on explicit confirmation — remove them.",
    )
    parser.add_argument("--roster", required=True, type=Path, help="member export from Telegram")
    parser.add_argument("--allowlist", type=Path, help="ids/usernames never removed")
    parser.add_argument("--report", type=Path, help="where to write the per-member CSV verdict")
    parser.add_argument(
        "--expect-chats",
        nargs="+",
        default=[],
        metavar="ID",
        # Space-separated rather than one comma-joined value, because every id
        # here starts with a minus: argparse reads `-1001945266701` as a negative
        # number and accepts it, but `-1001945266701,-1002368292795` is not a
        # number, so it reads that as an unknown option and dies with "expected
        # one argument". The one shape an operator would naturally type is the
        # one shape that fails, so it is not the shape this asks for.
        help="ids of every community a removal reaches, space-separated — required with --apply",
    )
    parser.add_argument(
        "--expect-channel",
        type=int,
        # Kept so the pre-GK-470 command line from the runbook reaches the guard
        # and gets told what to run, rather than dying in argparse with
        # "unrecognized arguments" on the morning of the cutover.
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--fetch-admins",
        action="store_true",
        help="ask Telegram for chat administrators and add them to the allowlist",
    )
    parser.add_argument("--apply", action="store_true", help="actually remove (default: dry run)")
    parser.add_argument("--confirm", default="", help=f"must be exactly {CONFIRM_TOKEN}")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--limit", type=int, help="stop after N removals (for a controlled smoke)")
    return parser


def check_apply_guards(args, *, hold_on: bool, targets: tuple[int, ...]) -> str | None:
    """Every reason to refuse a real removal, in one place. None == allowed.

    `targets` is what a removal *reaches*, in the order `kick_user` reaches them
    — not what the operator hopes it reaches, and not sorted, because sorted
    negative chat ids come out in an order that matches neither the resource
    table printed above nor anything a human holds in their head. The comparison
    against `--expect-chats` is set equality, so it fails in both directions: a
    chat named and then repointed, and a chat that appeared without ever being
    named (GK-470).
    """
    if not args.apply:
        return None
    if args.confirm != CONFIRM_TOKEN:
        return f"--apply requires --confirm {CONFIRM_TOKEN}"
    if hold_on:
        return (
            "the pre-launch hold is on (ENABLE_PRELAUNCH_HOLD=true) — nobody can have "
            "paid yet, so this would remove the entire community"
        )
    if not targets:
        return (
            "no Telegram resource is configured, so there is nothing to remove anyone "
            "from (PRIVATE_CHANNEL_ID / PRACTICE_CHAT_ID are unset)"
        )
    reached = frozenset(targets)
    named = " ".join(str(chat_id) for chat_id in targets)
    if getattr(args, "expect_channel", None) is not None:
        return (
            f"--expect-channel names one chat, but a removal reaches {len(targets)}: "
            f"{list(targets)}. Confirming one while emptying all of them is GK-470 — "
            f"use --expect-chats {named} instead"
        )
    raw = args.expect_chats
    if not isinstance(raw, str):
        # argparse hands over a list; a comma-joined string is also accepted so
        # that `--expect-chats=-100111,-100222` works and matches the shape of
        # EXPIRY_REMOVALS_EXPECT_CHATS (GK-460).
        raw = ",".join(str(part) for part in raw)
    try:
        expected = parse_tg_chat_id_list(raw)
    except ValueError as e:
        return f"--expect-chats is not a list of chat ids: {e}"
    if not expected:
        return (
            "--apply requires --expect-chats <ids> naming every community a removal "
            f"reaches — set it to {named} to confirm those are the ones you mean"
        )
    if expected != reached:
        missing = [chat_id for chat_id in targets if chat_id not in expected]
        extra = sorted(expected - reached)
        detail = []
        if missing:
            detail.append(f"would also remove from {missing}, which nobody named")
        if extra:
            detail.append(f"named {extra}, which no removal reaches")
        return (
            f"--expect-chats does not match the {len(targets)} configured "
            f"resource(s) {list(targets)} — "
            + "; ".join(detail)
            + " — refusing to act on the wrong community"
        )
    return None


async def _run(args) -> int:
    from app.db.session import async_session

    roster_text = args.roster.read_text(encoding="utf-8-sig")
    parsed = parse_roster(roster_text)
    if not parsed.entries:
        print(f"No members found in {args.roster}. Nothing to do.", file=sys.stderr)
        return 2

    allow_ids: set[int] = set()
    allow_names: set[str] = set()
    if args.allowlist:
        ids, names = parse_allowlist(args.allowlist.read_text(encoding="utf-8-sig"))
        allow_ids |= set(ids)
        allow_names |= set(names)

    bot = None
    if args.fetch_admins or args.apply:
        from aiogram import Bot

        bot = Bot(token=settings.bot_token)

    try:
        if args.fetch_admins:
            admins = await collect_admin_ids(bot)
            print(f"administrators fetched from Telegram: {len(admins)}")
            allow_ids |= set(admins)

        async with async_session() as session:
            plan = await build_plan(
                session,
                parsed,
                allowlist_ids=allow_ids,
                allowlist_usernames=allow_names,
            )

        resources = configured_access_resources()
        targets_to_confirm = target_chat_ids(resources)

        print(render_targets(resources))
        print()
        print(render_summary(plan))

        report_path = args.report or args.roster.with_suffix(".verdicts.csv")
        report_path.write_text(render_report(plan), encoding="utf-8")
        print(f"\nper-member verdicts written to {report_path}")

        refusal = check_apply_guards(
            args, hold_on=settings.enable_prelaunch_hold, targets=targets_to_confirm
        )
        if refusal is not None:
            print(f"\nREFUSED: {refusal}", file=sys.stderr)
            return 3

        named = " ".join(str(chat_id) for chat_id in targets_to_confirm)
        if not args.apply:
            print("\nDry run. Nothing was removed.")
            if targets_to_confirm:
                print("Review the CSV, then re-run with:")
                print(f"  --apply --confirm {CONFIRM_TOKEN} --expect-chats {named}")
            else:
                print(
                    "There is no command to re-run with: no Telegram resource is "
                    "configured, so nobody can be removed from anywhere."
                )
            return 0

        targets = len(plan.to_remove) if args.limit is None else min(args.limit, len(plan.to_remove))
        # Said again, immediately above the first ban, because everything between
        # this and the summary is scrollback by the time the run starts.
        print(f"\n{render_targets(resources)}")
        print(f"\nRemoving {targets} member(s) at {args.delay}s intervals. Ctrl-C stops.")

        def progress(index: int, total: int, verdict: Verdict, result: KickResult) -> None:
            state = "ok" if result.success else f"FAILED ({result.error})"
            print(f"  [{index}/{total}] {verdict.entry.label}: {state}", flush=True)

        report = await apply_removals(
            bot, plan, delay=args.delay, limit=args.limit, on_progress=progress
        )

        print(f"\nremoved      : {len(report.removed)}")
        print(f"rate-limited : {report.rate_limited}")
        print(f"failed       : {len(report.failed)}")
        if report.stopped_early:
            print(f"stopped early: {report.stopped_early}")
        if report.failed:
            failures_path = report_path.with_suffix(".failures.txt")
            failures_path.write_text(
                "\n".join(f"{tg_id}  # {error}" for tg_id, error in report.failed) + "\n",
                encoding="utf-8",
            )
            print(f"failures written to {failures_path} — re-run with it as --roster to retry")
        return 0 if report.ok else 1
    finally:
        if bot is not None:
            await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_arg_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
