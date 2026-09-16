from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).parents[1] / "scripts" / "update_prelaunch_allowlist.py"
SPEC = importlib.util.spec_from_file_location("update_prelaunch_allowlist", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def env_bytes(allowlist: str, *, hold: str = "true", newline: bytes = b"\n") -> bytes:
    return newline.join(
        [
            b"BOT_TOKEN=do-not-print-this-secret",
            f"ENABLE_PRELAUNCH_HOLD={hold}".encode(),
            f"PRELAUNCH_HOLD_ALLOWLIST={allowlist}".encode(),
            b"STRIPE_SECRET_KEY=also-do-not-print",
            b"",
        ]
    )


class FakeCompose:
    def __init__(self, ids: tuple[int, ...], *, fail: str | None = None) -> None:
        self.ids = ids
        self.fail = fail
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
        del cwd
        self.calls.append(args)
        action = "config" if "config" in args else "up" if "up" in args else "ps" if "ps" in args else "logs"
        if self.fail == action:
            return subprocess.CompletedProcess(args, 1, "", "deliberate failure")
        if action == "ps":
            return subprocess.CompletedProcess(args, 0, "bot\n", "")
        if action == "logs":
            marker = f"GK-446: pre-launch hold is on, but {len(self.ids)} Telegram id(s)"
            ids = ", ".join(str(item) for item in self.ids)
            return subprocess.CompletedProcess(args, 0, f"{marker}: {ids}\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class UpdatePrelaunchAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / ".env"

    def write(self, content: bytes) -> None:
        self.path.write_bytes(content)
        os.chmod(self.path, 0o640)

    def test_plan_changes_only_the_allowlist_line_and_preserves_crlf(self) -> None:
        original = env_bytes("11,22", newline=b"\r\n")
        self.write(original)

        plan = module.build_plan(self.path, 33)

        self.assertEqual(plan.before_ids, (11, 22))
        self.assertEqual(plan.after_ids, (11, 22, 33))
        self.assertEqual(
            plan.updated,
            original.replace(
                b"PRELAUNCH_HOLD_ALLOWLIST=11,22",
                b"PRELAUNCH_HOLD_ALLOWLIST=11,22,33",
            ),
        )
        self.assertEqual(self.path.read_bytes(), original)

    def test_existing_id_is_idempotent(self) -> None:
        original = env_bytes("11,22")
        self.write(original)
        plan = module.build_plan(self.path, 22)
        runner = FakeCompose(plan.after_ids)
        result = module.apply_plan(self.path, plan, runner=runner)
        self.assertFalse(plan.changed)
        self.assertIsNone(result.backup)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(any("config" in call for call in runner.calls))
        self.assertFalse(any("up" in call for call in runner.calls))

    def test_refuses_when_hold_is_not_true(self) -> None:
        self.write(env_bytes("11,22", hold="false"))
        with self.assertRaisesRegex(module.ConfigError, "allowlist is inert"):
            module.build_plan(self.path, 33)

    def test_refuses_duplicate_key_or_malformed_list(self) -> None:
        self.write(env_bytes("11,22") + b"PRELAUNCH_HOLD_ALLOWLIST=44\n")
        with self.assertRaisesRegex(module.ConfigError, "exactly one active"):
            module.build_plan(self.path, 33)
        self.write(env_bytes("11,@person"))
        with self.assertRaisesRegex(module.ConfigError, "canonical comma-separated"):
            module.build_plan(self.path, 33)

    def test_apply_is_atomic_preserves_secrets_and_verifies_runtime(self) -> None:
        original = env_bytes("11,22")
        self.write(original)
        plan = module.build_plan(self.path, 33)
        runner = FakeCompose(plan.after_ids)

        result = module.apply_plan(self.path, plan, runner=runner, sleep=lambda _: None)

        self.assertEqual(self.path.read_bytes(), plan.updated)
        self.assertIsNotNone(result.backup)
        assert result.backup
        self.assertEqual(result.backup.read_bytes(), original)
        if os.name != "nt":
            self.assertEqual(stat_mode(self.path), 0o640)
        self.assertIn(b"BOT_TOKEN=do-not-print-this-secret", self.path.read_bytes())
        self.assertTrue(any("config" in call for call in runner.calls))
        self.assertTrue(any("up" in call for call in runner.calls))

    def test_compose_failure_restores_original_without_restarting(self) -> None:
        original = env_bytes("11,22")
        self.write(original)
        plan = module.build_plan(self.path, 33)
        runner = FakeCompose(plan.after_ids, fail="config")

        with self.assertRaisesRegex(module.DeploymentError, "docker compose config"):
            module.apply_plan(self.path, plan, runner=runner, sleep=lambda _: None)

        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(any("up" in call for call in runner.calls))

    def test_concurrent_change_is_never_overwritten_by_rollback(self) -> None:
        original = env_bytes("11,22")
        concurrent = env_bytes("44")
        self.write(original)
        plan = module.build_plan(self.path, 33)
        atomic_replace = module._atomic_replace

        def race(path: Path, data: bytes, expected: bytes, mode: int) -> None:
            path.write_bytes(concurrent)
            atomic_replace(path, data, expected, mode)

        with mock.patch.object(module, "_atomic_replace", side_effect=race):
            with self.assertRaisesRegex(module.ConfigError, "changed during the operation"):
                module.apply_plan(self.path, plan, runner=FakeCompose(plan.after_ids))

        self.assertEqual(self.path.read_bytes(), concurrent)

    def test_runtime_failure_restores_original_and_recreates_old_bot(self) -> None:
        original = env_bytes("11,22")
        self.write(original)
        plan = module.build_plan(self.path, 33)
        runner = FakeCompose(plan.after_ids, fail="logs")

        with self.assertRaisesRegex(module.DeploymentError, "startup log check"):
            module.apply_plan(self.path, plan, runner=runner, sleep=lambda _: None, attempts=1)

        self.assertEqual(self.path.read_bytes(), original)
        up_calls = [call for call in runner.calls if "up" in call]
        self.assertEqual(len(up_calls), 2)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
