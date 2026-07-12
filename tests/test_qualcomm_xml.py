from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from matrixcorrect.qualcomm_xml import QualcommCCDocument


ROOT = Path(__file__).resolve().parents[1]
SOURCE_XML = ROOT / "source" / "cc13_ipe_v2.xml"


class QualcommXMLTests(unittest.TestCase):
    def test_parse_trigger_tree_and_match_cct(self) -> None:
        document = QualcommCCDocument.load(SOURCE_XML)
        self.assertEqual(len(document.regions), 5)
        self.assertEqual(
            document.control_variables,
            ("DRC Gain", "AEC Sensitivity Ratio", "LED Index", "Lux Index", "CCT"),
        )
        region, mode = document.find_region_for_cct(6500)
        self.assertEqual(mode, "exact")
        self.assertEqual(region.index, 4)
        self.assertEqual((region.cct_range.start, region.cct_range.end), (5800.0, 6500.0))
        transition_region, transition_mode = document.find_region_for_cct(2700)
        self.assertEqual(transition_mode, "transition")
        self.assertIn(transition_region.index, (0, 1))

    def test_surgical_write_changes_only_selected_c_values(self) -> None:
        document = QualcommCCDocument.load(SOURCE_XML)
        replacement = (
            (1.2, -0.1, -0.1),
            (-0.2, 1.3, -0.1),
            (0.1, -0.3, 1.2),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "optimized.xml"
            document.save_with_matrix(destination, 4, replacement)
            reloaded = QualcommCCDocument.load(destination)
            self.assertEqual(reloaded.regions[4].matrix, replacement)
            self.assertEqual(reloaded.regions[:4], document.regions[:4])
            original = SOURCE_XML.read_text(encoding="utf-8")
            updated = destination.read_text(encoding="utf-8")
            pattern = re.compile(r"(<c_tab\b[^>]*>.*?<c>)(.*?)(</c>.*?</c_tab>)", re.DOTALL)
            original_contents = [match.group(2) for match in pattern.finditer(original)]
            updated_contents = [match.group(2) for match in pattern.finditer(updated)]
            self.assertEqual(len(original_contents), 5)
            self.assertEqual(original_contents[:4], updated_contents[:4])
            self.assertNotEqual(original_contents[4], updated_contents[4])
            self.assertEqual(
                pattern.sub(r"\1MATRIX\3", original),
                pattern.sub(r"\1MATRIX\3", updated),
            )

    def test_xml_diff_contains_only_selected_matrix_change(self) -> None:
        document = QualcommCCDocument.load(SOURCE_XML)
        region = document.regions[4]
        matrix = tuple(
            tuple(value + (0.001 if column == row else -0.0005) for column, value in enumerate(values))
            for row, values in enumerate(region.matrix)
        )
        diff = document.diff_with_matrix(region.index, matrix)
        self.assertIn("--- cc13_ipe_v2.xml", diff)
        self.assertIn("+++ cc13_ipe_v2_optimized.xml", diff)
        self.assertEqual(sum(line.startswith("-") and not line.startswith("---") for line in diff.splitlines()), 1)
        self.assertEqual(sum(line.startswith("+") and not line.startswith("+++") for line in diff.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
