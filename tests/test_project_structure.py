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
        self.assertTrue((ROOT / "tunelab" / "image_inspector" / "ui.py").is_file())
        self.assertTrue((ROOT / "tunelab" / "image_inspector" / "matching.py").is_file())
        self.assertTrue((ROOT / "tunelab" / "assets" / "tunelab.png").is_file())
        self.assertTrue((ROOT / "run_tunelab.py").is_file())
        self.assertFalse((ROOT / "run_matrixcorrect.py").exists())
        self.assertFalse((ROOT / "matrixcorrect").exists())

    def test_packaging_exposes_tunelab_commands(self) -> None:
        metadata = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "tunelab"', metadata)
        self.assertIn('email = "kaiyi.jiang@thundersoft.com"', metadata)
        self.assertIn('tunelab = "tunelab.app:main"', metadata)
        self.assertIn('tunelab-ccm = "tunelab.ccm.cli:main"', metadata)
        self.assertIn('tunelab-gamma = "tunelab.gamma.ui:main"', metadata)
        self.assertNotIn("tunelab-image", metadata)
        self.assertIn('"numpy>=1.23"', metadata)
        self.assertIn('"Pillow>=9.2"', metadata)
        self.assertIn('"opencv-python>=4.7"', metadata)
        self.assertIn('"reportlab>=3.6"', metadata)
        self.assertNotIn("\nimage = [", metadata)
        self.assertNotIn("[project.optional-dependencies]", metadata)
        self.assertIn('tunelab = ["assets/*.png"]', metadata)
        self.assertNotIn('include = ["matrixcorrect*"', metadata)

    def test_source_launcher_owns_project_virtual_environment(self) -> None:
        source = (ROOT / "run_tunelab.py").read_text(encoding="utf-8")
        self.assertIn('VENV_DIR = ROOT / ".venv"', source)
        self.assertIn("venv.EnvBuilder(with_pip=True)", source)
        self.assertIn('"pip", "install", "-e"', source)
        self.assertIn("os.execv", source)
        self.assertIn("dependency_fingerprint", source)

    def test_image_inspector_has_no_standalone_entrypoint(self) -> None:
        source = (ROOT / "tunelab" / "image_inspector" / "ui.py").read_text(encoding="utf-8")
        self.assertNotIn("def main()", source)
        self.assertNotIn('if __name__ == "__main__"', source)
