"""GK-437: prove the newest backup is restorable, and notice when nobody is checking.

GK-427 made backups real. It could not tell you whether any of them opened. On
2026-08-09 the age private key on file failed its own checksum: every dump taken
until then was permanently unreadable, and the nightly job reported success the
whole time. "The backup job succeeded" is not the claim that matters.

The canary itself is a shell script (`deploy/backup/verify.sh`) — these tests
cover the half that runs in Python: reading the verdict, deciding whether it
still counts, and shouting when it does not. The case worth being pedantic
about is the one that actually bit us: **silence**. A canary that never runs
leaves exactly the same database state as a system where nothing has gone wrong
yet, so the tests below insist that absence is loud.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import tasks as bot_tasks
from app.services.backup_verification import (
    DEFAULT_MAX_AGE_HOURS,
    alert_text,
    assess,
    next_alert_key,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def row(**overrides):
    data = {
        "id": 1,
        "verified_at": NOW - timedelta(hours=6),
        "dump_file": "membership_saas-20260810T030000Z.sql.gz.age",
        "ok": True,
        "tables_checked": 31,
        "mismatches": 2,
        "detail": "restored 31 tables; 2 with counts moved on since the dump",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


# ---------------------------------------------------------------------------
# what the verdict means
# ---------------------------------------------------------------------------


def test_a_fresh_successful_restore_is_the_only_ok_state():
    health = assess(row(), now=NOW)
    assert health.state == "ok"
    assert health.ok is True
    assert health.age_hours == pytest.approx(6.0)


def test_no_verification_at_all_is_a_failure_not_a_blank():
    """The state we were actually in until this task shipped.

    Zero rows is not "no news". It means nothing has ever proved a dump opens,
    which is materially worse than a failed check, because a failed check at
    least tells you the machinery runs.
    """
    health = assess(None, now=NOW)
    assert health.state == "never"
    assert health.ok is False


def test_a_verification_that_stopped_happening_is_a_failure():
    health = assess(row(verified_at=NOW - timedelta(hours=40)), now=NOW)
    assert health.state == "stale"
    assert health.ok is False


def test_the_age_window_is_a_boundary_not_a_suggestion():
    just_inside = assess(
        row(verified_at=NOW - timedelta(hours=DEFAULT_MAX_AGE_HOURS - 1)), now=NOW
    )
    just_outside = assess(
        row(verified_at=NOW - timedelta(hours=DEFAULT_MAX_AGE_HOURS + 1)), now=NOW
    )
    assert just_inside.state == "ok"
    assert just_outside.state == "stale"


def test_a_failed_restore_is_reported_as_failed():
    health = assess(
        row(ok=False, detail="DECRYPTION FAILED with the configured identity"), now=NOW
    )
    assert health.state == "failed"
    assert "DECRYPTION FAILED" in (health.detail or "")


def test_an_old_success_cannot_mask_a_run_that_has_since_stopped():
    """Ordering matters: stale wins over failed.

    If the newest row is both old and a failure, the actionable fact is that
    nothing has run since — fixing the old failure is pointless while the
    canary is dead.
    """
    health = assess(row(ok=False, verified_at=NOW - timedelta(days=5)), now=NOW)
    assert health.state == "stale"


def test_a_naive_timestamp_does_not_crash_the_check():
    """A hand-inserted row (an operator running the SQL directly) may lack a
    timezone. Raising here would take out the dashboard and the daily job at
    once — the two things that exist to report problems."""
    health = assess(row(verified_at=datetime(2026, 8, 10, 6, 0)), now=NOW)
    assert health.state == "ok"
    assert health.age_hours == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# what gets said
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state_row", [None, row(ok=False), row(verified_at=NOW - timedelta(days=3))])
def test_every_failure_state_produces_a_message_that_names_the_problem(state_row):
    health = assess(state_row, now=NOW)
    text = alert_text(health)
    assert len(text) > 80
    # No placeholder leakage, and the message must not read like a success.
    assert "{" not in text
    assert "🚨" in text


def test_the_failed_message_says_we_do_not_have_a_backup():
    """Not "a backup check failed" — the honest statement is that the files we
    are holding do not currently constitute a backup."""
    health = assess(row(ok=False, detail="restore into scratch failed: syntax error"), now=NOW)
    text = alert_text(health)
    assert "не восстанавливается" in text.lower() or "нет бэкапа" in text.lower()
    assert "syntax error" in text


def test_the_stale_message_does_not_claim_the_backup_is_broken():
    """Staleness says the checking stopped, which is not the same claim. Saying
    "backups are broken" when we simply stopped looking is how alerts get
    ignored."""
    health = assess(row(verified_at=NOW - timedelta(days=3)), now=NOW)
    text = alert_text(health)
    assert "остановилась" in text or "устарела" in text


def test_each_state_alerts_under_its_own_key():
    """Rate-limit keys are per state so a stale→failed transition is delivered
    rather than swallowed by the window an earlier alert opened."""
    keys = {
        next_alert_key(assess(None, now=NOW)),
        next_alert_key(assess(row(ok=False), now=NOW)),
        next_alert_key(assess(row(verified_at=NOW - timedelta(days=3)), now=NOW)),
    }
    assert len(keys) == 3


def test_the_dashboard_headline_never_reads_green_for_a_bad_state():
    for bad in (None, row(ok=False), row(verified_at=NOW - timedelta(days=3))):
        assert "восстанавливался" not in assess(bad, now=NOW).headline


# ---------------------------------------------------------------------------
# the job that watches the clock
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self, value):
        self._value = value

    async def execute(self, _query):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: self._value))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _patch_session(monkeypatch, value):
    monkeypatch.setattr(bot_tasks, "async_session", lambda: FakeSession(value))


def fresh_row(**overrides):
    """A row the *job* will still consider current.

    `row()` is anchored to the frozen `NOW`, which is correct for `assess(now=NOW)`
    and wrong for every test that calls `backup_verification_job()` — the job reads
    the real clock. A fixture pinned to a fixed date therefore ages past the 36-hour
    window on its own and the test silently starts exercising the `stale` branch
    instead of the one it names. That is what happened on 2026-08-11: two tests here
    began failing at 18:00 UTC with no code change behind it.
    """
    overrides.setdefault("verified_at", datetime.now(UTC) - timedelta(hours=6))
    return row(**overrides)


@pytest.mark.asyncio
async def test_the_job_is_silent_while_backups_are_provably_restorable(monkeypatch):
    """A daily "all good" trains people to ignore the channel, which is how the
    original failure survived thirteen days."""
    _patch_session(monkeypatch, fresh_row())
    alert = AsyncMock()
    monkeypatch.setattr(bot_tasks, "send_ops_alert", alert)

    await bot_tasks.backup_verification_job()

    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_dead_canary_is_reported_by_something_outside_the_canary(monkeypatch):
    """The failure mode the script itself can never report.

    A container that never starts writes no rows and logs nothing anybody
    reads. The bot is the right watcher because its own liveness is already
    proven every sixty seconds.
    """
    _patch_session(monkeypatch, None)
    alert = AsyncMock()
    monkeypatch.setattr(bot_tasks, "send_ops_alert", alert)

    await bot_tasks.backup_verification_job()

    alert.assert_awaited_once()
    assert alert.await_args.kwargs["severity"] == "error"
    assert alert.await_args.kwargs["key"] == "backup_verification:never"


@pytest.mark.asyncio
async def test_a_failed_restore_reaches_telegram_with_the_reason(monkeypatch):
    _patch_session(
        monkeypatch,
        fresh_row(
            ok=False,
            detail="DECRYPTION FAILED with the configured identity: no identity matched",
        ),
    )
    alert = AsyncMock()
    monkeypatch.setattr(bot_tasks, "send_ops_alert", alert)

    await bot_tasks.backup_verification_job()

    alert.assert_awaited_once()
    assert "DECRYPTION FAILED" in alert.await_args.args[0]


@pytest.mark.asyncio
async def test_the_reminder_repeats_under_a_day_so_a_restart_cannot_skip_it(monkeypatch):
    """Same reasoning as the manual-cancellation digest: short enough that a
    bot restart cannot drop a whole cycle, long enough that a restart loop
    cannot turn it into 48 messages."""
    _patch_session(monkeypatch, None)
    alert = AsyncMock()
    monkeypatch.setattr(bot_tasks, "send_ops_alert", alert)

    await bot_tasks.backup_verification_job()

    window = alert.await_args.kwargs["rate_limit_seconds"]
    assert 12 * 3600 <= window < 24 * 3600


@pytest.mark.asyncio
async def test_the_job_honours_the_configured_window(monkeypatch):
    """The window is a setting, so it has to be read at run time rather than
    frozen at import — otherwise raising it on the server changes nothing."""
    _patch_session(monkeypatch, row(verified_at=datetime.now(UTC) - timedelta(hours=40)))
    alert = AsyncMock()
    monkeypatch.setattr(bot_tasks, "send_ops_alert", alert)
    monkeypatch.setattr(
        bot_tasks, "get_settings", lambda: SimpleNamespace(backup_verification_max_age_hours=100)
    )

    await bot_tasks.backup_verification_job()

    alert.assert_not_awaited()


def test_the_job_is_actually_scheduled():
    """A job nobody registered is a function with tests and no effect."""
    import inspect

    from app.bot import main as bot_main

    source = inspect.getsource(bot_main._build_scheduler)
    assert "backup_verification_job" in source


# ---------------------------------------------------------------------------
# the canary script's own guard rails
# ---------------------------------------------------------------------------
#
# verify.sh is not importable, so these read it. They are cheap and they cover
# the two mistakes that would be catastrophic rather than merely wrong.


def _verify_sh() -> str:
    from pathlib import Path

    for candidate in (
        Path("/repo/deploy/backup/verify.sh"),
        Path(__file__).resolve().parents[2] / "deploy" / "backup" / "verify.sh",
    ):
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    pytest.skip("deploy/ is not in this build context")


def test_the_canary_refuses_to_restore_over_the_live_database():
    """It drops the target database first. If BACKUP_VERIFY_DB were ever set to
    POSTGRES_DB, that single line would destroy production — so the script must
    check before it does anything else."""
    source = _verify_sh()
    assert 'if [ "$VERIFY_DB" = "$POSTGRES_DB" ]' in source
    guard = source.index('if [ "$VERIFY_DB" = "$POSTGRES_DB" ]')
    assert guard < source.index("DROP DATABASE")


def test_the_canary_records_its_failures_rather_than_only_exiting():
    """A failure that leaves no row is indistinguishable from a canary that
    never ran — and the bot would then report the wrong problem."""
    source = _verify_sh()
    fail_body = source[source.index("fail() {") : source.index("fail() {") + 300]
    assert "record false" in fail_body


def test_a_dump_that_restores_but_is_ancient_still_fails():
    """"Restorable" is worth nothing if it is last week's data."""
    source = _verify_sh()
    assert "MAX_AGE_HOURS" in source
    assert "backups have stopped" in source


def test_the_canary_does_not_compare_the_table_it_writes():
    """Self-poisoning, found by the negative pass and not by reading the code.

    Every run appends a row to `backup_verifications` in live and none to the
    restore. Comparing that table means the second run fails because the first
    one happened — a check that breaks itself on a schedule. The exclusion is
    the fix; this test is here so nobody removes it as an oddity.
    """
    source = _verify_sh()
    assert "tablename <> 'backup_verifications'" in source


def test_an_empty_table_in_the_restore_is_a_failure_and_drift_is_not():
    """The dump is a point in time and live moves on, so unequal counts are
    expected. A table that is populated in live and empty in the restore is
    not — that is data that would not come back."""
    source = _verify_sh()
    assert 'if [ "$n_live" -gt 0 ] && [ "$n_restored" -eq 0 ]' in source
    assert "restore is missing data in" in source
