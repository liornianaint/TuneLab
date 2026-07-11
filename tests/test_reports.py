from __future__ import annotations

import importlib.util
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from matrixcorrect.imatest import parse_imatest_csv
from matrixcorrect.optimizer import optimize_ccm
from matrixcorrect.qualcomm_xml import QualcommCCDocument
from matrixcorrect.report import save_analysis_html, save_analysis_pdf, save_analysis_xlsx


ROOT = Path(__file__).resolve().parents[1]


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = parse_imatest_csv(ROOT / "Source" / "D65_normal_summary.csv")
        cls.document = QualcommCCDocument.load(ROOT / "Source" / "cc13_ipe_v2.xml")
        cls.region, _ = cls.document.find_region_for_cct(6500)
        cls.result = optimize_ccm(cls.dataset, cls.region.matrix)
        cls.diff = cls.document.diff_with_matrix(cls.region.index, cls.result.optimized_matrix)

    def test_html_and_xlsx_reports_are_structurally_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path = save_analysis_html(root / "analysis.html", self.dataset, self.result, region_label=self.region.path_label(), xml_diff=self.diff)
            workbook_path = save_analysis_xlsx(root / "analysis.xlsx", self.dataset, self.result, region_label=self.region.path_label(), xml_diff=self.diff)
            document = html_path.read_text(encoding="utf-8")
            self.assertIn("Matrix Engineering Checks", document)
            self.assertIn("Before：Camera", document)
            self.assertIn("XML Diff", document)
            with zipfile.ZipFile(workbook_path) as archive:
                for name in archive.namelist():
                    if name.endswith(".xml") or name.endswith(".rels"):
                        ET.fromstring(archive.read(name))
                patch_sheet = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
                self.assertIn("IF(E2&lt;0.1,&quot;N/A&quot;,(E2-F2)/E2)", patch_sheet)
                summary_sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                self.assertIn("COUNTIF(&#x27;Patches&#x27;!E$2:E$25,&quot;&lt;=&quot;&amp;B15)/24", summary_sheet)
                self.assertIn("MatrixCorrect Engineering Report", summary_sheet)

    @unittest.skipUnless(importlib.util.find_spec("reportlab"), "reportlab not installed")
    def test_pdf_report_is_generated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = save_analysis_pdf(Path(temporary) / "analysis.pdf", self.dataset, self.result, region_label=self.region.path_label(), xml_diff=self.diff)
            self.assertTrue(path.read_bytes().startswith(b"%PDF"))
            self.assertGreater(path.stat().st_size, 10_000)
