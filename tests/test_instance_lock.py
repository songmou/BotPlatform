from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.core.infrastructure.instance_lock import AlreadyRunning, SingleInstanceLock


class SingleInstanceLockTests(unittest.TestCase):
    def test_second_process_reports_owner_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            with SingleInstanceLock(lock_path):
                command = [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        "from pathlib import Path; "
                        "from src.core.infrastructure.instance_lock import AlreadyRunning, SingleInstanceLock; "
                        "\ntry:\n"
                        "  with SingleInstanceLock(Path(sys.argv[1])): pass\n"
                        "except AlreadyRunning as exc:\n"
                        "  print(str(exc)); raise SystemExit(7)\n"
                    ),
                    str(lock_path),
                ]
                result = subprocess.run(
                    command,
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )

            self.assertEqual(result.returncode, 7)
            self.assertIn("机器人已启动，请勿重复运行", result.stdout)
            self.assertIn("PID=", result.stdout)
            self.assertIn("启动时间=", result.stdout)

    def test_released_and_stale_lock_files_can_be_acquired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            lock_path.write_text(
                json.dumps({"pid": 999999, "started_at": "2026-01-01T00:00:00+08:00"}),
                encoding="utf-8",
            )

            with SingleInstanceLock(lock_path):
                current = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertNotEqual(current["pid"], 999999)

            with SingleInstanceLock(lock_path):
                pass

    def test_lock_is_released_after_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with SingleInstanceLock(lock_path):
                    raise RuntimeError("boom")

            with SingleInstanceLock(lock_path):
                pass

    def test_same_process_cannot_acquire_lock_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "bot.lock"
            with SingleInstanceLock(lock_path):
                with self.assertRaises(AlreadyRunning):
                    with SingleInstanceLock(lock_path):
                        pass
