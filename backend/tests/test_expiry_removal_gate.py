"""GK-484: the gate in front of arming the hourly expiry removals.

Three rows were due on the live host on 2026-08-23 — two of them the client's
manager, one an ordinary member whose gift had run out — and the only thing
stopping the hourly job from banning all three out of the production channel and
DMing them a subscription pitch was a flag somebody was about to turn off on
launch morning. `app.ops.expiry_removal_gate` is what has to be run first.

What these tests hold, because each one is a way the gate could quietly stop
being a gate:

* it asks the **job's own selector**, not a second copy of the predicate that
  agrees with it today;
* the exit code carries the answer, so the runbook step can be a command whose
  failure is visible rather than a paragraph somebody reads at 3am;
* it writes nothing without `--expire`, an admin and a reason, and it can only
  ever write onto a row it just listed;
* an expiry recorded here does **not** claim a Telegram removal that never
  happened — `access_revoked_at` stays empty, which is the field the 15.09
  cutover and the panel both read as "this member is gone".
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.bot import tasks
from app.ops import expiry_removal_gate as gate
from app.services import subscription as subscription_service

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

CHANNEL = -1001945266701
PRACTICE = -1002368292795


def make_sub(**overrides):
    """`sub#4`'s shape: a gift whose window closed while removals were disarmed."""
    data = {
        "id": 4,
        "user_id": 4,
        "status": "active",
        "source": "gift",
        "is_comp": False,
        "provider": None,
        "provider_subscription_id": None,
        "provider_status": None,
        "current_period_end": None,
        "grace_ends_at": None,
        "expires_at": NOW - timedelta(days=11),
        "access_revoked_at": None,
        "user": SimpleNamespace(tg_id=7436224582, username="HypnoGeorgi"),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def gate_settings(**overrides):
    data = {
        "enable_prelaunch_hold": False,
        "enable_expiry_removals": False,
        "expiry_removals_expect_chats": "",
        "expiry_removals_expect_chat_ids": frozenset(),
        "private_channel_id": CHANNEL,
        "practice_chat_id": PRACTICE,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class FakeSession:
    """Enough session for the gate: a due set per call, and one admin lookup."""

    def __init__(self, admin=None):
        self.admin = admin
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.admin)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def run_gate(monkeypatch, argv, due_sets, *, admin=None, settings=None):
    """Run the CLI over a scripted sequence of due sets. Returns (code, stdout)."""
    session = FakeSession(admin=admin)
    calls = iter(due_sets)

    async def fake_expire_subscriptions(_session):
        return next(calls)

    monkeypatch.setattr(gate, "async_session", lambda: session)
    monkeypatch.setattr(gate, "expire_subscriptions", fake_expire_subscriptions)
    monkeypatch.setattr(gate, "get_settings", lambda: settings or gate_settings())
    monkeypatch.setattr(
        gate,
        "configured_access_resources",
        lambda: (
            SimpleNamespace(title="Community channel", chat_id=CHANNEL),
            SimpleNamespace(title="Practice chat", chat_id=PRACTICE),
        ),
    )
    return gate.main(argv), session


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_the_gate_asks_the_job_its_own_question():
    """Not a copy of the predicate that happens to agree. The whole value of the
    gate is that a row it says nothing about is a row the job cannot reach."""
    assert gate.expire_subscriptions is subscription_service.expire_subscriptions


def test_zero_rows_exits_clear(monkeypatch, capsys):
    code, session = run_gate(monkeypatch, [], [[]])

    assert code == gate.EXIT_CLEAR
    out = capsys.readouterr().out
    assert "GATE CLEAR" in out
    assert session.commits == 0


def test_a_due_row_makes_the_gate_red(monkeypatch, capsys):
    code, session = run_gate(monkeypatch, [], [[make_sub()]])

    assert code == gate.EXIT_ROWS_DUE
    out = capsys.readouterr().out
    assert "GATE RED" in out
    assert "Do not set ENABLE_EXPIRY_REMOVALS" in out
    assert session.commits == 0


def test_the_report_names_the_person_and_the_rooms(monkeypatch, capsys):
    """An operator deciding whether to arm is deciding about people, so the
    output has to say which ones and where they would be removed from."""
    run_gate(monkeypatch, [], [[make_sub()]])

    out = capsys.readouterr().out
    assert "sub#4" in out
    assert "@HypnoGeorgi" in out
    assert "7436224582" in out
    assert str(CHANNEL) in out and str(PRACTICE) in out


def test_the_report_quotes_the_message_that_would_actually_be_sent(monkeypatch, capsys):
    run_gate(monkeypatch, [], [[make_sub()]])

    assert tasks.EXPIRY_REMOVAL_DM in capsys.readouterr().out


def test_the_dm_is_the_job_s_own_string():
    """If this ever becomes two strings, the gate starts showing an operator a
    sentence the bot no longer sends."""
    assert gate.EXPIRY_REMOVAL_DM is tasks.EXPIRY_REMOVAL_DM


def test_it_says_whether_the_job_is_armed_right_now(monkeypatch, capsys):
    run_gate(
        monkeypatch,
        [],
        [[make_sub()]],
        settings=gate_settings(
            enable_expiry_removals=True,
            expiry_removals_expect_chats=f"{CHANNEL},{PRACTICE}",
            expiry_removals_expect_chat_ids=frozenset({CHANNEL, PRACTICE}),
        ),
    )

    assert "WOULD REMOVE the rows below" in capsys.readouterr().out


def test_the_hold_is_reported_ahead_of_the_arming_flags(monkeypatch, capsys):
    """`kick_expired_job` returns on the hold before it looks at the arming
    flags, so a fully-armed job under a hold removes nobody. Reporting the
    arming state alone would print WOULD REMOVE at a moment nothing can."""
    run_gate(
        monkeypatch,
        [],
        [[make_sub()]],
        settings=gate_settings(
            enable_prelaunch_hold=True,
            enable_expiry_removals=True,
            expiry_removals_expect_chats=f"{CHANNEL},{PRACTICE}",
            expiry_removals_expect_chat_ids=frozenset({CHANNEL, PRACTICE}),
        ),
    )

    out = capsys.readouterr().out
    assert "ENABLE_PRELAUNCH_HOLD is on" in out
    assert "WOULD REMOVE" not in out


def test_an_unarmed_job_is_reported_as_refusing(monkeypatch, capsys):
    run_gate(monkeypatch, [], [[make_sub()]])

    assert "ENABLE_EXPIRY_REMOVALS is not set" in capsys.readouterr().out


def test_a_database_that_cannot_be_read_is_not_a_clear_result(monkeypatch, capsys):
    """The failure mode that would matter most: an unreadable due set exits
    non-zero anyway, which is also what "rows are due" exits — so it has to say
    which one it is rather than let an operator read the code as either."""
    session = FakeSession()

    async def boom(_session):
        raise RuntimeError("password authentication failed")

    monkeypatch.setattr(gate, "async_session", lambda: session)
    monkeypatch.setattr(gate, "expire_subscriptions", boom)
    monkeypatch.setattr(gate, "get_settings", gate_settings)
    monkeypatch.setattr(gate, "configured_access_resources", lambda: ())

    assert gate.main([]) == gate.EXIT_REFUSED
    captured = capsys.readouterr()
    assert "GATE ERROR" in captured.err
    assert "GATE CLEAR" not in captured.out


# --------------------------------------------------------------------------
# The one write it can make
# --------------------------------------------------------------------------


def test_expiring_a_row_records_the_end_without_claiming_a_removal():
    sub = make_sub()

    written = gate.expire_rows([sub], [4], note="gift ended 18.08", now=NOW)

    assert [s.id for s in written] == [4]
    assert sub.status == "expired"
    # The field that means "removed from Telegram". Nobody was.
    assert sub.access_revoked_at is None


def test_it_will_not_expire_a_row_the_gate_did_not_list():
    with pytest.raises(gate.GateRefusal) as refusal:
        gate.expire_rows([make_sub()], [9], note="typo", now=NOW)

    assert "not due for removal: 9" in str(refusal.value)


def test_it_will_not_expire_a_team_row():
    """Unreachable through the due set — `expire_subscriptions` excludes comp
    rows — and checked anyway, because this is the one command that writes a
    status onto a row nobody opened in a browser first."""
    with pytest.raises(gate.GateRefusal) as refusal:
        gate.expire_rows([make_sub(is_comp=True)], [4], note="wrong tool", now=NOW)

    assert "panel" in str(refusal.value)


def test_it_will_not_expire_a_subscription_that_is_still_running():
    live = make_sub(expires_at=NOW + timedelta(days=5))

    with pytest.raises(gate.GateRefusal):
        gate.expire_rows([live], [4], note="no", now=NOW)

    assert live.status == "active"


def test_a_write_needs_an_admin_and_a_reason(monkeypatch, capsys):
    code, session = run_gate(monkeypatch, ["--expire", "4"], [[make_sub()]])

    assert code == gate.EXIT_REFUSED
    assert session.commits == 0
    assert "REFUSED" in capsys.readouterr().err


def test_a_write_needs_an_admin_that_exists(monkeypatch, capsys):
    code, session = run_gate(
        monkeypatch,
        ["--expire", "4", "--admin-id", "99", "--note", "why"],
        [[make_sub()]],
        admin=None,
    )

    assert code == gate.EXIT_REFUSED
    assert session.commits == 0
    assert "no admin_users row" in capsys.readouterr().err


def test_the_gate_reruns_itself_after_writing(monkeypatch, capsys):
    """The zero the task asks to be pasted in has to come from the query again,
    not from subtracting what we just wrote."""
    sub = make_sub()
    code, session = run_gate(
        monkeypatch,
        ["--expire", "4", "--admin-id", "3", "--note", "gift ended 18.08"],
        [[sub], []],
        admin=SimpleNamespace(id=3, email="grant@example.com"),
    )

    assert code == gate.EXIT_CLEAR
    assert sub.status == "expired"
    assert session.commits == 1
    out = capsys.readouterr().out
    assert "GATE CLEAR" in out
    assert "grant@example.com" in out


def test_a_write_that_leaves_rows_behind_still_exits_red(monkeypatch, capsys):
    """Two of the three rows are the team's and are cleared in the panel, not
    here. Clearing the third must not report the gate as open."""
    grant_row = make_sub(id=2, user_id=3, source="lava", user=SimpleNamespace(tg_id=556081290, username="GRANTPROGRESS"))
    code, _ = run_gate(
        monkeypatch,
        ["--expire", "4", "--admin-id", "3", "--note", "gift ended 18.08"],
        [[make_sub(), grant_row], [grant_row]],
        admin=SimpleNamespace(id=3, email="grant@example.com"),
    )

    assert code == gate.EXIT_ROWS_DUE
    assert "GATE RED" in capsys.readouterr().out


def test_the_write_is_audited(monkeypatch):
    _, session = run_gate(
        monkeypatch,
        ["--expire", "4", "--admin-id", "3", "--note", "gift ended 18.08"],
        [[make_sub()], []],
        admin=SimpleNamespace(id=3, email="grant@example.com"),
    )

    assert len(session.added) == 1
    entry = session.added[0]
    assert entry.action == "subscription.expired_without_removal"
    assert entry.actor_admin_id == 3
    assert entry.details["note"] == "gift ended 18.08"
