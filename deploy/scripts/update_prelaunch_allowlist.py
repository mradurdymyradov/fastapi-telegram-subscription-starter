"""Safely append one Telegram user id to PRELAUNCH_HOLD_ALLOWLIST.

The production env file contains secrets, so this command never prints the file.
It validates the two relevant keys, changes one line atomically, creates a dated
backup, validates Compose, recreates only the bot, and rolls back automatically if
the new bot does not start with the expected GK-446 allowlist log.

Dry-run (default):
    python3 update_prelaunch_allowlist.py --env-file .env --add 6362950211

Apply after explicit production approval:
    python3 update_prelaunch_allowlist.py --env-file .env --add 6362950211 --apply
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

ALLOWLIST_KEY = b"PRELAUNCH_HOLD_ALLOWLIST"
HOLD_KEY = b"ENABLE_PRELAUNCH_HOLD"
MAX_TG_ID = 9_223_372_036_854_775_807
COMPOSE_PREFIX = ("docker", "compose", "-p", "membership_saas")


class ConfigError(RuntimeError):
    """The env file cannot be changed without guessing."""


class DeploymentError(RuntimeError):
    """The runtime check failed; the env file has been rolled back."""


@dataclass(frozen=True)
class UpdatePlan:
    original: bytes
    updated: bytes
    before_ids: tuple[int, ...]
    after_ids: tuple[int, ...]

    @property
    def changed(self) -> bool:
        return self.original != self.updated


@dataclass(frozen=True)
class ApplyResult:
    plan: UpdatePlan
    backup: Path | None


Runner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]


def positive_tg_id(raw: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        raise argparse.ArgumentTypeError("Telegram id must contain positive decimal digits only")
    value = int(raw)
    if value > MAX_TG_ID:
        raise argparse.ArgumentTypeError("Telegram id is outside the signed 64-bit range")
    return value


def _assignment(lines: list[bytes], key: bytes) -> tuple[int, re.Match[bytes]]:
    pattern = re.compile(
        rb"^(?P<prefix>[ \t]*" + re.escape(key) + rb"[ \t]*=[ \t]*)"
        rb"(?P<value>[^\r\n]*)(?P<newline>\r?\n)?$"
    )
    matches = [(index, match) for index, line in enumerate(lines) if (match := pattern.match(line))]
    if len(matches) != 1:
        label = key.decode("ascii")
        raise ConfigError(f"expected exactly one active {label}= line; found {len(matches)}")
    return matches[0]


def _parse_allowlist(raw: bytes) -> tuple[int, ...]:
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ConfigError("PRELAUNCH_HOLD_ALLOWLIST must be ASCII decimal ids") from exc
    if not value:
        return ()
    parts = value.split(",")
    if any(not re.fullmatch(r"[1-9][0-9]*", part.strip()) for part in parts):
        raise ConfigError("PRELAUNCH_HOLD_ALLOWLIST is not a canonical comma-separated id list")
    ids = tuple(int(part.strip()) for part in parts)
    if any(value > MAX_TG_ID for value in ids):
        raise ConfigError("PRELAUNCH_HOLD_ALLOWLIST contains an out-of-range id")
    if len(set(ids)) != len(ids):
        raise ConfigError("PRELAUNCH_HOLD_ALLOWLIST contains duplicate ids")
    return ids


def build_plan(env_file: Path, user_id: int) -> UpdatePlan:
    if user_id <= 0 or user_id > MAX_TG_ID:
        raise ConfigError("Telegram id must be a positive signed 64-bit integer")
    if env_file.is_symlink():
        raise ConfigError("refusing to replace a symlinked env file")
    if not env_file.is_file():
        raise ConfigError(f"env file does not exist: {env_file}")

    original = env_file.read_bytes()
    lines = original.splitlines(keepends=True)
    hold_index, hold_match = _assignment(lines, HOLD_KEY)
    del hold_index
    hold_value = hold_match.group("value").strip().lower()
    if hold_value != b"true":
        raise ConfigError(
            "ENABLE_PRELAUNCH_HOLD is not exactly true; the allowlist is inert, so no edit is needed"
        )

    index, match = _assignment(lines, ALLOWLIST_KEY)
    before_ids = _parse_allowlist(match.group("value"))
    if user_id in before_ids:
        return UpdatePlan(original, original, before_ids, before_ids)

    after_ids = (*before_ids, user_id)
    value = ",".join(str(item) for item in after_ids).encode("ascii")
    lines[index] = match.group("prefix") + value + (match.group("newline") or b"")
    updated = b"".join(lines)
    if updated.count(ALLOWLIST_KEY) < 1:
        raise ConfigError("internal guard: allowlist key disappeared from candidate env")
    return UpdatePlan(original, updated, before_ids, after_ids)


def _atomic_replace(path: Path, data: bytes, expected_current: bytes, mode: int) -> None:
    if path.read_bytes() != expected_current:
        raise ConfigError("env file changed during the operation; refusing to overwrite it")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.gk489.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, stat.S_IMODE(mode))
        if path.read_bytes() != expected_current:
            raise ConfigError("env file changed during the operation; refusing to replace it")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _default_runner(args: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _checked(runner: Runner, args: tuple[str, ...], cwd: Path, label: str) -> str:
    result = runner(args, cwd)
    if result.returncode != 0:
        raise DeploymentError(f"{label} failed with exit {result.returncode}")
    return result.stdout


def _runtime_ready(runner: Runner, cwd: Path, ids: tuple[int, ...]) -> bool:
    ps_output = _checked(
        runner,
        (*COMPOSE_PREFIX, "ps", "--status", "running", "--services", "bot"),
        cwd,
        "bot status check",
    )
    if "bot" not in {line.strip() for line in ps_output.splitlines()}:
        return False
    logs = _checked(
        runner,
        (*COMPOSE_PREFIX, "logs", "--no-color", "--since", "2m", "bot"),
        cwd,
        "bot startup log check",
    )
    marker = f"GK-446: pre-launch hold is on, but {len(ids)} Telegram id(s)"
    return marker in logs and all(str(item) in logs for item in ids)


def _restore(path: Path, plan: UpdatePlan, mode: int, runner: Runner, restart: bool) -> None:
    current = path.read_bytes()
    _atomic_replace(path, plan.original, current, mode)
    if restart:
        result = runner(
            (*COMPOSE_PREFIX, "up", "-d", "--no-deps", "--force-recreate", "bot"),
            path.parent,
        )
        if result.returncode != 0:
            raise DeploymentError(
                "new bot failed and env was restored, but the rollback bot recreate also failed"
            )


def apply_plan(
    env_file: Path,
    plan: UpdatePlan,
    *,
    runner: Runner = _default_runner,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 30,
) -> ApplyResult:
    if not plan.changed:
        _checked(
            runner,
            (*COMPOSE_PREFIX, "config", "--quiet"),
            env_file.parent,
            "docker compose config",
        )
        if _runtime_ready(runner, env_file.parent, plan.after_ids):
            return ApplyResult(plan=plan, backup=None)
        _checked(
            runner,
            (*COMPOSE_PREFIX, "up", "-d", "--no-deps", "--force-recreate", "bot"),
            env_file.parent,
            "bot recreate",
        )
        for _ in range(attempts):
            if _runtime_ready(runner, env_file.parent, plan.after_ids):
                return ApplyResult(plan=plan, backup=None)
            sleep(1)
        raise DeploymentError("bot did not report the expected GK-446 allowlist before timeout")

    mode = env_file.stat().st_mode
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = env_file.with_name(f".env.bak.prelaunch_allowlist.{stamp}")
    if backup.exists():
        raise ConfigError(f"backup path already exists: {backup}")
    shutil.copy2(env_file, backup)

    candidate_written = False
    restart_attempted = False
    try:
        _atomic_replace(env_file, plan.updated, plan.original, mode)
        candidate_written = True
        reread = build_plan(env_file, plan.after_ids[-1])
        if reread.before_ids != plan.after_ids or reread.changed:
            raise ConfigError("candidate env did not survive read-back validation")
        _checked(
            runner,
            (*COMPOSE_PREFIX, "config", "--quiet"),
            env_file.parent,
            "docker compose config",
        )
        restart_attempted = True
        _checked(
            runner,
            (*COMPOSE_PREFIX, "up", "-d", "--no-deps", "--force-recreate", "bot"),
            env_file.parent,
            "bot recreate",
        )
        for _ in range(attempts):
            if _runtime_ready(runner, env_file.parent, plan.after_ids):
                return ApplyResult(plan=plan, backup=backup)
            sleep(1)
        raise DeploymentError("bot did not report the expected GK-446 allowlist before timeout")
    except Exception as exc:
        if candidate_written:
            try:
                _restore(env_file, plan, mode, runner, restart_attempted)
            except Exception as rollback_exc:
                raise DeploymentError(
                    f"deployment failed and rollback was incomplete: {rollback_exc}"
                ) from exc
        if isinstance(exc, (ConfigError, DeploymentError)):
            raise
        raise DeploymentError(f"deployment failed and env was rolled back: {exc}") from exc


def _ids_text(ids: tuple[int, ...]) -> str:
    return ",".join(str(item) for item in ids) or "(empty)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--add", type=positive_tg_id, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        plan = build_plan(args.env_file, args.add)
        print(f"before={_ids_text(plan.before_ids)}")
        print(f"after={_ids_text(plan.after_ids)}")
        if not args.apply:
            state = "id already present" if not plan.changed else "candidate prepared"
            print(f"changed=false (dry-run; {state})")
            print("pass --apply only after explicit production approval")
            return 0
        result = apply_plan(args.env_file, plan)
        print(f"changed={'true' if plan.changed else 'false (id already present)'}")
        print(f"backup={result.backup}")
        print("bot=running; allowlist_log=verified")
        return 0
    except (ConfigError, DeploymentError) as exc:
        print(f"REFUSED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
