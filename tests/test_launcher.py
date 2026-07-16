from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import run_tunelab


class ProjectLauncherTests(unittest.TestCase):
    def test_project_python_is_cross_platform_virtualenv_interpreter(self) -> None:
        root = Path("project") / ".venv"
        expected = root / "Scripts" / "python.exe" if os.name == "nt" else root / "bin" / "python"
        self.assertEqual(run_tunelab.project_python(root), expected)

    def test_dependency_fingerprint_changes_with_project_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / "pyproject.toml"
            metadata.write_text('[project]\nname = "tunelab"\n', encoding="utf-8")
            first = run_tunelab.dependency_fingerprint(root)
            metadata.write_text('[project]\nname = "tunelab"\nversion = "2"\n', encoding="utf-8")
            second = run_tunelab.dependency_fingerprint(root)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
