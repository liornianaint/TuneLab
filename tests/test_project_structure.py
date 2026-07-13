from __future__ import annotations

import unittest
from pathlib import Path

from tunelab.app import TuneLabApp


ROOT = Path(__file__).resolve().parents[1]


class ProjectStructureTests(unittest.TestCase):
    def test_tunelab_namespace_and_entrypoint_names_are_current(self) -> None:
        self.assertEqual(TuneLabApp.__name__, "TuneLabApp")
        self.assertTrue((ROOT / "tunelab" / "ccm" / "optimizer.py").is_file())
        self.assertTrue((ROOT / "tunelab" / "gamma" / "optimizer.py").is_file())
        self.assertTrue((ROOT / "tunelab" / "assets" / "tunelab.png").is_file())
        self.assertTrue((ROOT / "run_tunelab.py").is_file())
        self.assertFalse((ROOT / "run_matrixcorrect.py").exists())
        self.assertFalse((ROOT / "matrixcorrect").exists())

    def test_packaging_exposes_tunelab_commands(self) -> None:
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "tunelab"', metadata)
        self.assertIn('tunelab = "tunelab.app:main"', metadata)
        self.assertIn('tunelab-ccm = "tunelab.ccm.cli:main"', metadata)
        self.assertIn('tunelab-gamma = "tunelab.gamma.ui:main"', metadata)
        self.assertIn('tunelab = ["assets/*.png"]', metadata)
        self.assertNotIn('include = ["matrixcorrect*"', metadata)
