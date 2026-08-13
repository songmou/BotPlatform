"""Tests for canonical application paths."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PathsTest(unittest.TestCase):
    def test_data_directory_can_be_isolated_with_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "benchmark-data"
            environment = dict(os.environ)
            environment["BOTPLATFORM_DATA_DIR"] = str(override)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from src.core.paths import DATA_DIR, SYSTEM_DATA_DIR; "
                        "print(DATA_DIR); print(SYSTEM_DATA_DIR)"
                    ),
                ],
                cwd=Path(__file__).resolve().parent.parent,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            lines = completed.stdout.splitlines()
            resolved = override.resolve()
            self.assertEqual(lines, [str(resolved), str(resolved / "system")])


if __name__ == "__main__":
    unittest.main()
