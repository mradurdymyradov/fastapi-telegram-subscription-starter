"""GK-016 — the free-to-paid cutover command.

The acceptance criteria for GK-016 name six behaviours that have to hold before
anyone points this at 4.5k real people: the allowlist protects admins, paid
members survive, unpaid ones are eligible, dry run has no side effects, rate
limits are respected and failures recorded, and a real removal takes an explicit
human confirmation. There is one test below per behaviour, plus the parser cases,
because the export shape is the part we cannot verify by reading our own code.
"""
import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.ops import cutover
from app.ops.cutover import (
    CONFIRM_TOKEN,
    KEEP_ALLOWLISTED,
    KEEP_PAID,
    REMOVE,
    UNRESOLVED,
    CutoverPlan,
    RosterEntry,
    RosterParse,
    Verdict,
    apply_removals,
    build_plan,
    check_apply_guards,
    collect_admin_ids,
    parse_allowlist,
    parse_roster,
    render_report,
)
from app.services import channel_access
from app.services.channel_access import KickResult

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def make_user(pk: int, tg_id: int, username: str | None = None):
    return SimpleNamespace(id=pk, tg_id=tg_id, username=username)


def make_sub(user_id: int, *, expires_at=None, status="active", access_revoked_at=None):
    return SimpleNamespace(
        user_id=user_id,
        status=status,
        expires_at=expires_at if expires_at is not None else NOW + timedelta(days=20),
        current_period_end=None,
        grace_ends_at=None,
        access_revoked_at=access_revoked_at,
        provider=None,
        provider_status=None,
        source="manual",
    )


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return SimpleNamespace(all=lambda: self._rows)


class FakeSession:
    """Answers the two queries `build_plan` makes, by inspecting the model."""

    def __init__(self, users, subs):
        self.users = users
        self.subs = subs
        self.queries = 0

    async def execute(self, query):
        self.queries += 1
        entity = query.column_descriptions[0]["entity"].__name__
        if entity == "User":
            return FakeResult(list(self.users))
        return FakeResult(list(self.subs))


class RecordingBot:
    def __init__(self, admins=()):
        self._admins = admins
        self.calls = []

    async def get_chat_administrators(self, chat_id):
        self.calls.append(chat_id)
        return [SimpleNamespace(user=SimpleNamespace(id=uid)) for uid in self._admins]


# --------------------------------------------------------------------------
# Parsing — the export shape is Grant's, so the parser has to survive variety
# --------------------------------------------------------------------------


def test_plain_id_list_parses():
    parsed = parse_roster("123456789\n987654321\n\n# a comment\n")
    assert [e.tg_id for e in parsed.entries] == [123456789, 987654321]
    assert parsed.fmt == "lines"


def test_usernames_with_and_without_at_parse():
    parsed = parse_roster("@someone\nother_person\n")
    assert [e.username for e in parsed.entries] == ["someone", "other_person"]


def test_csv_with_recognised_headers_parses_both_columns():
    parsed = parse_roster("user_id,username,joined\n555666777,@Alpha,2024-01-01\n")
    assert parsed.fmt == "csv"
    assert parsed.entries[0].tg_id == 555666777
    assert parsed.entries[0].username == "alpha"


def test_json_export_is_walked_recursively():
    blob = '{"chat":{"participants":[{"id":111222333,"username":"beta"},{"user_id":"user444555666"}]}}'
    parsed = parse_roster(blob)
    assert parsed.fmt == "json"
    assert {e.tg_id for e in parsed.entries} == {111222333, 444555666}


def test_short_numbers_are_not_mistaken_for_telegram_ids():
    # A year, a count, a row number — none of these is a member.
    parsed = parse_roster("2024\n17\n")
    assert parsed.entries == ()
    assert len(parsed.skipped) == 2


def test_unparseable_json_falls_back_to_the_line_scanner_instead_of_raising():
    parsed = parse_roster('{"broken": [123456789,')
    assert [e.tg_id for e in parsed.entries] == [123456789]


def test_allowlist_accepts_ids_and_usernames_in_one_file():
    ids, names = parse_allowlist("# admins\n123456789\n@Grant\n")
    assert ids == frozenset({123456789})
    assert names == frozenset({"grant"})


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


async def _plan(entries, users, subs, **kwargs):
    session = FakeSession(users, subs)
    return await build_plan(session, RosterParse(tuple(entries)), now=NOW, **kwargs)


@pytest.mark.asyncio
async def test_admin_on_the_allowlist_is_kept_even_with_no_subscription_at_all():
    plan = await _plan([RosterEntry("1", tg_id=111)], [], [], allowlist_ids={111})
    assert plan.verdicts[0].outcome == KEEP_ALLOWLISTED


@pytest.mark.asyncio
async def test_allowlist_by_username_is_honoured_case_insensitively():
    plan = await _plan(
        [RosterEntry("@Grant", tg_id=111, username="grant")],
        [],
        [],
        allowlist_usernames={"@GRANT"},
    )
    assert plan.verdicts[0].outcome == KEEP_ALLOWLISTED


@pytest.mark.asyncio
async def test_paid_member_is_kept():
    user = make_user(7, 111)
    plan = await _plan([RosterEntry("111", tg_id=111)], [user], [make_sub(7)])
    assert plan.verdicts[0].outcome == KEEP_PAID


@pytest.mark.asyncio
async def test_member_whose_subscription_ended_is_removed():
    user = make_user(7, 111)
    expired = make_sub(7, expires_at=NOW - timedelta(days=1))
    plan = await _plan([RosterEntry("111", tg_id=111)], [user], [expired])
    assert plan.verdicts[0].outcome == REMOVE
    assert plan.verdicts[0].reason == "no subscription"


@pytest.mark.asyncio
async def test_member_who_never_used_the_bot_is_removed_and_says_so():
    plan = await _plan([RosterEntry("111", tg_id=111)], [], [])
    assert plan.verdicts[0].outcome == REMOVE
    assert plan.verdicts[0].reason == "never used the bot"


@pytest.mark.asyncio
async def test_cancelled_but_still_inside_the_paid_period_keeps_access():
    # cancel_at_period_end is not "gone" — GK-377 pays through the period.
    user = make_user(7, 111)
    sub = make_sub(7, status="cancelled", expires_at=NOW + timedelta(days=3))
    plan = await _plan([RosterEntry("111", tg_id=111)], [user], [sub])
    assert plan.verdicts[0].outcome == KEEP_PAID


@pytest.mark.asyncio
async def test_already_revoked_row_does_not_count_as_paid():
    user = make_user(7, 111)
    sub = make_sub(7, access_revoked_at=NOW - timedelta(days=2))
    plan = await _plan([RosterEntry("111", tg_id=111)], [user], [sub])
    assert plan.verdicts[0].outcome == REMOVE


@pytest.mark.asyncio
async def test_username_only_row_resolves_through_the_users_table():
    user = make_user(7, 999, username="Alpha")
    plan = await _plan([RosterEntry("@alpha", username="alpha")], [user], [make_sub(7)])
    assert plan.verdicts[0].outcome == KEEP_PAID
    assert plan.verdicts[0].tg_id == 999


@pytest.mark.asyncio
async def test_username_the_bot_has_never_seen_is_unresolved_never_removed():
    plan = await _plan([RosterEntry("@ghost", username="ghost")], [], [])
    verdict = plan.verdicts[0]
    assert verdict.outcome == UNRESOLVED
    assert verdict.tg_id is None
    assert plan.to_remove == ()


@pytest.mark.asyncio
async def test_counts_cover_every_outcome():
    users = [make_user(7, 111), make_user(8, 222)]
    plan = await _plan(
        [
            RosterEntry("111", tg_id=111),
            RosterEntry("222", tg_id=222),
            RosterEntry("333", tg_id=333),
            RosterEntry("@ghost", username="ghost"),
        ],
        users,
        [make_sub(7)],
        allowlist_ids={222},
    )
    assert plan.counts == {
        KEEP_ALLOWLISTED: 1,
        KEEP_PAID: 1,
        REMOVE: 1,
        UNRESOLVED: 1,
    }


# --------------------------------------------------------------------------
# Admin discovery
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_administrators_are_read_from_telegram_for_every_resource(monkeypatch):
    monkeypatch.setattr(
        "app.services.channel_access.configured_access_resources",
        lambda: (
            SimpleNamespace(key="community_channel", chat_id=-100111),
            SimpleNamespace(key="practice_chat", chat_id=-100222),
        ),
    )
    bot = RecordingBot(admins=[501, 502])
    assert await collect_admin_ids(bot) == frozenset({501, 502})
    assert bot.calls == [-100111, -100222]


@pytest.mark.asyncio
async def test_a_failure_to_read_administrators_raises_rather_than_silently_kicking_them(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.channel_access.configured_access_resources",
        lambda: (SimpleNamespace(key="community_channel", chat_id=-100111),),
    )

    class Broken:
        async def get_chat_administrators(self, chat_id):
            raise RuntimeError("Forbidden")

    with pytest.raises(RuntimeError):
        await collect_admin_ids(Broken())


# --------------------------------------------------------------------------
# Removal
# --------------------------------------------------------------------------


def _removal_plan(*tg_ids):
    return CutoverPlan(
        tuple(
            Verdict(RosterEntry(str(tg_id), tg_id=tg_id), REMOVE, "no subscription", tg_id)
            for tg_id in tg_ids
        )
    )


@pytest.mark.asyncio
async def test_dry_run_touches_telegram_zero_times():
    """`build_plan` is the whole dry run — it is handed a session and no bot.

    The separation is the safety property: producing the plan cannot call the
    Bot API because it has nothing to call it with. Removal is a second,
    separately-guarded function that takes the bot.
    """
    plan = await _plan([RosterEntry("111", tg_id=111)], [], [])
    assert plan.to_remove  # there IS someone to remove
    assert "bot" not in inspect.signature(build_plan).parameters


@pytest.mark.asyncio
async def test_only_members_marked_remove_are_touched():
    kicked = []

    async def kick(bot, tg_id):
        kicked.append(tg_id)
        return KickResult(True)

    plan = CutoverPlan(
        (
            Verdict(RosterEntry("111", tg_id=111), REMOVE, "no subscription", 111),
            Verdict(RosterEntry("222", tg_id=222), KEEP_PAID, "has subscription access", 222),
            Verdict(RosterEntry("333", tg_id=333), KEEP_ALLOWLISTED, "on the allowlist", 333),
            Verdict(RosterEntry("@ghost"), UNRESOLVED, "no numeric id", None),
        )
    )
    report = await apply_removals(None, plan, kick=kick, sleep=_no_sleep, delay=0)
    assert kicked == [111]
    assert report.removed == [111]


async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_rate_limit_is_waited_out_and_retried_once():
    slept = []
    attempts = []

    async def kick(bot, tg_id):
        attempts.append(tg_id)
        if len(attempts) == 1:
            return KickResult(False, retry_after=30, error="retry_after=30")
        return KickResult(True)

    async def sleep(seconds):
        slept.append(seconds)

    report = await apply_removals(
        None, _removal_plan(111), kick=kick, sleep=sleep, delay=0
    )
    assert attempts == [111, 111]
    assert slept == [31]  # retry_after + 1, and no inter-member delay for a single target
    assert report.rate_limited == 1
    assert report.removed == [111]


@pytest.mark.asyncio
async def test_failures_are_recorded_with_the_id_so_they_can_be_retried():
    async def kick(bot, tg_id):
        if tg_id == 222:
            return KickResult(False, error="USER_NOT_PARTICIPANT")
        return KickResult(True)

    report = await apply_removals(
        None, _removal_plan(111, 222, 333), kick=kick, sleep=_no_sleep, delay=0
    )
    assert report.removed == [111, 333]
    assert report.failed == [(222, "USER_NOT_PARTICIPANT")]
    assert report.ok is False


@pytest.mark.asyncio
async def test_delay_is_applied_between_members_but_not_after_the_last_one():
    slept = []

    async def kick(bot, tg_id):
        return KickResult(True)

    async def sleep(seconds):
        slept.append(seconds)

    await apply_removals(
        None, _removal_plan(111, 222, 333), kick=kick, sleep=sleep, delay=1.5
    )
    assert slept == [1.5, 1.5]


@pytest.mark.asyncio
async def test_limit_caps_a_controlled_smoke_run():
    kicked = []

    async def kick(bot, tg_id):
        kicked.append(tg_id)
        return KickResult(True)

    await apply_removals(
        None, _removal_plan(111, 222, 333), kick=kick, sleep=_no_sleep, delay=0, limit=1
    )
    assert kicked == [111]


@pytest.mark.asyncio
async def test_interruption_returns_a_failed_partial_report():
    kicked = []

    async def kick(bot, tg_id):
        if tg_id == 222:
            raise asyncio.CancelledError
        kicked.append(tg_id)
        return KickResult(True)

    report = await apply_removals(
        None, _removal_plan(111, 222, 333), kick=kick, sleep=_no_sleep, delay=0
    )

    assert kicked == [111]
    assert report.removed == [111]
    assert report.failed == []
    assert report.stopped_early == (
        "interrupted after 1 of 3 confirmed member result(s); 2 result(s) remain unconfirmed"
    )
    assert report.ok is False


# --------------------------------------------------------------------------
# The guards on a real removal
# --------------------------------------------------------------------------


CHANNEL = -100111
PRACTICE = -100222
# In `kick_user`'s own order, not sorted: sorted negative chat ids come out
# backwards from the resource table an operator is reading.
TARGETS = (CHANNEL, PRACTICE)


def _args(**overrides):
    data = {
        "apply": True,
        "confirm": CONFIRM_TOKEN,
        "expect_chats": [str(CHANNEL), str(PRACTICE)],
        "expect_channel": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _resource(title, chat_id, link=None):
    return SimpleNamespace(title=title, chat_id=chat_id, fallback_invite_link=link)


def test_dry_run_needs_no_confirmation_at_all():
    args = _args(apply=False, confirm="", expect_chats="")
    assert check_apply_guards(args, hold_on=True, targets=()) is None


def test_removal_without_the_exact_token_is_refused():
    assert "--confirm" in check_apply_guards(
        _args(confirm="yes"), hold_on=False, targets=TARGETS
    )


def test_removal_is_refused_while_the_prelaunch_hold_is_on():
    # Under the hold nobody can have paid, so every member would classify as
    # REMOVE. This is the guard that stops the worst possible run.
    refusal = check_apply_guards(_args(), hold_on=True, targets=TARGETS)
    assert "pre-launch hold" in refusal


def test_removal_is_refused_when_no_chats_were_named():
    refusal = check_apply_guards(_args(expect_chats=[]), hold_on=False, targets=TARGETS)
    assert "--expect-chats" in refusal
    # It says which ids to use, so the operator does not go and look them up in
    # a `.env` — looking them up is how the wrong one gets pasted.
    assert f"{CHANNEL} {PRACTICE}" in refusal


def test_naming_only_the_channel_is_refused_because_removal_reaches_two():
    """GK-470, the defect this whole section exists for: `--expect-channel` used
    to be satisfied by the channel alone while `kick_user` emptied the practice
    chat as well — one room confirmed, two emptied."""
    refusal = check_apply_guards(
        _args(expect_chats=[str(CHANNEL)]), hold_on=False, targets=TARGETS
    )
    assert "refusing to act on the wrong community" in refusal
    assert f"would also remove from [{PRACTICE}]" in refusal


def test_naming_a_chat_no_removal_reaches_is_refused():
    """The other direction of the same equality: a stale id in the command line
    means the operator's picture of the targets is wrong, even though every real
    target happens to be named."""
    refusal = check_apply_guards(
        _args(expect_chats=[str(CHANNEL), str(PRACTICE), "-100999"]),
        hold_on=False,
        targets=TARGETS,
    )
    assert "[-100999], which no removal reaches" in refusal


def test_a_repointed_chat_is_refused():
    # The 10.08 class of incident: acting on whichever chat `.env` happens to name.
    refusal = check_apply_guards(_args(), hold_on=False, targets=(CHANNEL, -100999))
    assert "refusing to act on the wrong community" in refusal


def test_the_comma_joined_form_is_accepted_too():
    """`--expect-chats=-100111,-100222` is the shape EXPIRY_REMOVALS_EXPECT_CHATS
    uses, and somebody who knows that one will type it here."""
    assert (
        check_apply_guards(
            _args(expect_chats=[f"{CHANNEL},{PRACTICE}"]), hold_on=False, targets=TARGETS
        )
        is None
    )


def test_the_old_single_chat_flag_is_refused_with_the_command_to_run_instead():
    """The runbook, the readiness doc and whatever is in somebody's shell history
    all still say `--expect-channel`. Dying in argparse would be a puzzle at the
    worst moment; this answers it."""
    refusal = check_apply_guards(_args(expect_channel=CHANNEL), hold_on=False, targets=TARGETS)
    assert "GK-470" in refusal
    assert f"--expect-chats {CHANNEL} {PRACTICE}" in refusal


def test_removal_is_refused_when_nothing_is_configured_to_remove_from():
    refusal = check_apply_guards(_args(), hold_on=False, targets=())
    assert "no Telegram resource is configured" in refusal


def test_an_unparseable_expectation_is_refused_rather_than_ignored():
    refusal = check_apply_guards(
        _args(expect_chats=["@membership_owner"]), hold_on=False, targets=TARGETS
    )
    assert "not a list of chat ids" in refusal


def test_all_guards_satisfied_allows_the_run():
    assert check_apply_guards(_args(), hold_on=False, targets=TARGETS) is None


def test_the_targets_are_the_ones_kick_user_itself_loops():
    """Not a second list assembled from PRIVATE_CHANNEL_ID and PRACTICE_CHAT_ID.
    A third community added to `configured_access_resources` has to become a chat
    this command demands be named, not one it silently starts emptying."""
    assert cutover.configured_access_resources is channel_access.configured_access_resources


def test_a_resource_with_no_chat_id_is_not_a_target():
    """`_kick_user_from_resource` refuses it as a permanent failure, so nobody is
    removed there — and asking an operator to confirm an id that does not exist
    would make the confirmation unanswerable."""
    resources = (
        _resource("Community channel", CHANNEL),
        _resource("Practice chat", 0, link="https://t.me/+abc"),
    )
    assert cutover.target_chat_ids(resources) == (CHANNEL,)


def test_the_run_names_every_room_before_anyone_is_taken_out_of_it():
    text = cutover.render_targets(
        (_resource("Community channel", CHANNEL), _resource("Practice chat", PRACTICE))
    )
    assert "2 Telegram resource(s)" in text
    assert "Community channel" in text and str(CHANNEL) in text
    assert "Practice chat" in text and str(PRACTICE) in text


def test_the_run_says_so_when_there_is_nowhere_to_remove_from():
    assert "no Telegram resource is configured" in cutover.render_targets(())


def test_the_parser_accepts_the_plural_flag_and_defaults_it_empty():
    args = cutover.build_arg_parser().parse_args(["--roster", "members.csv"])
    assert args.expect_chats == []
    assert args.expect_channel is None


def test_the_parser_takes_two_negative_ids_the_way_the_run_prints_them():
    """The trap this shape exists to avoid: a single comma-joined value starting
    with a minus is not a number, so argparse reads it as an unknown option and
    exits 2 before any of the guards above ever run. Space-separated negative
    ids parse; so does the `=` form. Both are what the command prints."""
    parser = cutover.build_arg_parser()
    spaced = parser.parse_args(
        ["--roster", "m.csv", "--expect-chats", str(CHANNEL), str(PRACTICE)]
    )
    assert spaced.expect_chats == [str(CHANNEL), str(PRACTICE)]

    joined = parser.parse_args(["--roster", "m.csv", f"--expect-chats={CHANNEL},{PRACTICE}"])
    assert joined.expect_chats == [f"{CHANNEL},{PRACTICE}"]


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def test_report_names_every_row_and_its_reason():
    plan = CutoverPlan(
        (
            Verdict(RosterEntry("111", tg_id=111, username="alpha"), REMOVE, "no subscription", 111),
            Verdict(RosterEntry("@ghost", username="ghost"), UNRESOLVED, "no numeric id", None),
        )
    )
    csv_text = render_report(plan)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "outcome,tg_id,username,reason,raw"
    assert lines[1].startswith("remove,111,alpha,no subscription")
    assert lines[2].startswith("unresolved,,ghost,no numeric id")


def test_cli_defaults_to_a_dry_run():
    args = cutover.build_arg_parser().parse_args(["--roster", "members.csv"])
    assert args.apply is False
    assert args.confirm == ""
