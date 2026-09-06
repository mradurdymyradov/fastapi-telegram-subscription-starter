"""GK-437: read the restore canary's verdict and decide whether to shout.

The canary itself is a shell script in the backup image (`deploy/backup/verify.sh`):
it decrypts the newest dump, restores it into a scratch database, compares it
table by table against live, and writes one row into `backup_verifications`.

This module is the other half — the part that notices when those rows stop
arriving. That distinction is the whole design. A canary that reports failures
is only half a canary; the failure mode that actually happened on 2026-08-09
was silence, and silence is indistinguishable from success unless something
independent is watching the clock. The bot process does the watching, because
it is the one component whose liveness is already proven every 60 seconds.

Three states are failures, and they are deliberately not collapsed into one:

  never   — no verification has ever run. The canary was never deployed, or
            has never once succeeded in reaching the database.
  stale   — verifications exist but the newest is older than the window. The
            canary container is dead, wedged, or was never restarted.
  failed  — the newest verification ran and said no. This is the good case, in
            the sense that the machinery worked; the backup is the problem.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BackupVerification

# The canary runs daily. 36 hours lets one run be skipped by a deploy or a
# slow restore without paging anybody, and catches a second consecutive miss.
DEFAULT_MAX_AGE_HOURS = 36


@dataclass(frozen=True)
class BackupHealth:
    """What we can honestly say about the newest backup right now."""

    state: str  # "ok" | "never" | "stale" | "failed"
    verified_at: datetime | None
    age_hours: float | None
    dump_file: str | None
    detail: str | None

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    @property
    def headline(self) -> str:
        """One line, safe to put on a dashboard card."""
        if self.state == "never":
            return "Восстановление ни разу не проверено"
        if self.state == "stale":
            hours = int(self.age_hours or 0)
            return f"Проверка восстановления устарела: последняя {hours} ч назад"
        if self.state == "failed":
            return "Последняя проверка восстановления ПРОВАЛИЛАСЬ"
        hours = int(self.age_hours or 0)
        return f"Бэкап восстанавливался {hours} ч назад"


async def latest_verification(session: AsyncSession) -> BackupVerification | None:
    """The newest row, successful or not.

    Deliberately not filtered to `ok=true`: a run that failed must be able to
    push the state to "failed", and filtering it out would let an old success
    keep the dashboard green while every run since has been failing.
    """
    return (
        await session.execute(
            select(BackupVerification).order_by(BackupVerification.verified_at.desc()).limit(1)
        )
    ).scalars().first()


def assess(
    row: BackupVerification | None,
    *,
    now: datetime | None = None,
    max_age_hours: int = DEFAULT_MAX_AGE_HOURS,
) -> BackupHealth:
    reference = now or datetime.now(UTC)

    if row is None:
        return BackupHealth("never", None, None, None, None)

    verified_at = row.verified_at
    if verified_at is not None and verified_at.tzinfo is None:
        # Defensive: a naive timestamp would raise on subtraction. Rows written
        # by psql carry a tz, but a hand-inserted one might not.
        verified_at = verified_at.replace(tzinfo=UTC)

    age_hours = (
        (reference - verified_at).total_seconds() / 3600.0 if verified_at is not None else None
    )

    # Order matters: a stale row that also says ok=false is reported as stale,
    # because "nothing has run since" is the more actionable fact — chasing the
    # old failure is pointless if the canary is not running at all.
    if age_hours is not None and age_hours > max_age_hours:
        state = "stale"
    elif not row.ok:
        state = "failed"
    else:
        state = "ok"

    return BackupHealth(
        state=state,
        verified_at=verified_at,
        age_hours=age_hours,
        dump_file=row.dump_file,
        detail=row.detail,
    )


def alert_text(health: BackupHealth, *, max_age_hours: int = DEFAULT_MAX_AGE_HOURS) -> str:
    """The Telegram message. Only ever built for a non-ok state."""
    if health.state == "never":
        return (
            "🚨 Бэкапы: восстановление ни разу не проверялось\n\n"
            "Ни одной записи о проверке. Либо канарейка не запущена, либо она "
            "ни разу не смогла записать результат.\n"
            "Это значит: у нас есть файлы бэкапов и нет доказательства, что "
            "хоть один из них открывается.\n\n"
            "Проверить: docker compose -p membership_saas logs backup-verify"
        )

    when = health.verified_at.strftime("%Y-%m-%d %H:%M UTC") if health.verified_at else "?"

    if health.state == "stale":
        hours = int(health.age_hours or 0)
        return (
            "🚨 Бэкапы: проверка восстановления остановилась\n\n"
            f"Последняя проверка: {when} ({hours} ч назад, допустимо {max_age_hours} ч).\n"
            "Сама по себе тишина ничего не говорит о бэкапах — она говорит, что "
            "проверять их перестали. Пока это так, «бэкап есть» — предположение.\n\n"
            "Проверить: docker compose -p membership_saas logs backup-verify"
        )

    return (
        "🚨 Бэкапы: ПОСЛЕДНИЙ БЭКАП НЕ ВОССТАНАВЛИВАЕТСЯ\n\n"
        f"Проверка: {when}\n"
        f"Файл: {health.dump_file or '?'}\n"
        f"Причина: {(health.detail or 'не указана')[:600]}\n\n"
        "Пока это не исправлено, у нас нет бэкапа — есть файлы, которые не "
        "разворачиваются. Руководство: docs/runbooks/backup_restore.md"
    )


def next_alert_key(health: BackupHealth) -> str:
    """Rate-limit key.

    Keyed by state, not by row: a canary that fails every night should say so
    once a day, not once per run, and a transition from stale to failed is
    news worth delivering immediately rather than swallowing into the window.
    """
    return f"backup_verification:{health.state}"


__all__ = [
    "DEFAULT_MAX_AGE_HOURS",
    "BackupHealth",
    "alert_text",
    "assess",
    "latest_verification",
    "next_alert_key",
]
