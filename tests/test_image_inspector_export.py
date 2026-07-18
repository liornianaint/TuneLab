from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"NumPy unavailable: {exc}")

from tunelab.image_inspector.export import (
    CSV_FIELDS,
    build_export_rows,
    build_multi_export_rows,
    export_csv,
    export_multi_csv,
)
from tunelab.image_inspector.model import analyse_roi, compare_statistics
from tunelab.image_inspector.types import ImageData, MatchResult, ROI


def make_image(path: Path, colour) -> ImageData:
    rgb = np.full((10, 12, 3), colour, dtype=np.float32)
    return ImageData(
        path=path,
        width=12,
        height=10,
        bit_depth=8,
        source_mode="RGB",
        rgb=rgb,
        display_rgb=rgb.astype(np.uint8),
    )


class ImageInspectorExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.before_image = make_image(Path("/private/输入/之前.jpg"), (100, 120, 140))
        self.after_image = make_image(Path("/private/输入/之后.jpg"), (110, 118, 150))
        self.before_roi = ROI(1, 2, 8, 6, "灰墙 中文")
        self.after_roi = ROI(2, 3, 8, 6, "灰墙 中文")
        self.before_stats = analyse_roi(self.before_image, self.before_roi)
        self.after_stats = analyse_roi(self.after_image, self.after_roi)
        self.high_match = MatchResult(
            before_roi=self.before_roi,
            after_roi=self.after_roi,
            expected_roi=self.after_roi,
            search_bounds=ROI(0, 0, 12, 10),
            score=0.964,
            confidence="高",
            reliable=True,
            method="test",
        )
        self.comparison = compare_statistics(
            self.before_stats,
            self.after_stats,
            reliable=True,
            match_score=self.high_match.score,
        )

    def test_export_rows_contain_every_declared_field_and_deltas(self) -> None:
        rows = build_export_rows(
            self.before_image,
            self.before_stats,
            after_image=self.after_image,
            after_stats=self.after_stats,
            match=self.high_match,
            comparison=self.comparison,
        )
        self.assertEqual(len(rows), 2)
        for field in CSV_FIELDS:
            self.assertIn(field, rows[1] if field.startswith("delta_") else rows[0])
        self.assertAlmostEqual(rows[1]["delta_r"], 10.0)
        self.assertAlmostEqual(rows[1]["delta_g"], -2.0)
        self.assertAlmostEqual(rows[1]["delta_b_percent"], 100.0 / 14.0)
        self.assertAlmostEqual(
            rows[1]["delta_normalized_r"],
            self.after_stats.normalized_rgb[0] - self.before_stats.normalized_rgb[0],
        )

    def test_utf8_bom_and_chinese_roi_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "分析.csv"
            export_csv(path, self.before_image, self.before_stats)
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("灰墙 中文", text)
        self.assertIn("image_role", text.splitlines()[0])

    def test_csv_can_omit_full_private_path(self) -> None:
        rows = build_export_rows(
            self.before_image,
            self.before_stats,
            include_full_path=False,
        )
        self.assertEqual(rows[0]["image_path"], "之前.jpg")
        self.assertEqual(rows[0]["image_filename"], "之前.jpg")

    def test_low_confidence_warning_is_exported(self) -> None:
        low = MatchResult(
            before_roi=self.before_roi,
            after_roi=self.after_roi,
            expected_roi=self.after_roi,
            search_bounds=ROI(0, 0, 12, 10),
            score=0.42,
            confidence="低",
            reliable=False,
            method="test",
            warning="低置信度测试提示",
        )
        comparison = compare_statistics(self.before_stats, self.after_stats, reliable=False, match_score=0.42)
        rows = build_export_rows(
            self.before_image,
            self.before_stats,
            after_image=self.after_image,
            after_stats=self.after_stats,
            match=low,
            comparison=comparison,
        )
        self.assertIn("低置信度测试提示", rows[1]["notes"])
        self.assertIn("暂不输出确定性", rows[1]["notes"])

    def test_written_csv_header_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dual.csv"
            export_csv(
                path,
                self.before_image,
                self.before_stats,
                after_image=self.after_image,
                after_stats=self.after_stats,
                match=self.high_match,
                comparison=self.comparison,
            )
            with path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
        self.assertEqual(reader.fieldnames, CSV_FIELDS)
        self.assertEqual(len(rows), 2)

    def test_four_image_export_compares_every_target_to_reference(self) -> None:
        images = [
            self.before_image,
            self.after_image,
            make_image(Path("/private/输入/第三张.jpg"), (95, 125, 145)),
            make_image(Path("/private/输入/第四张.jpg"), (120, 110, 135)),
        ]
        stats = [analyse_roi(image, self.before_roi) for image in images]
        matches = [self.high_match, self.high_match, self.high_match]
        comparisons = [
            compare_statistics(stats[0], target, reliable=True, match_score=0.964)
            for target in stats[1:]
        ]
        rows = build_multi_export_rows(
            images,
            stats,
            matches=matches,
            comparisons=comparisons,
            include_full_path=False,
        )
        self.assertEqual([row["image_role"] for row in rows], ["reference", "comparison_2", "comparison_3", "comparison_4"])
        self.assertEqual(rows[3]["image_filename"], "第四张.jpg")
        self.assertAlmostEqual(rows[1]["delta_r"], 10.0)
        self.assertNotIn("delta_r", rows[0])
        with tempfile.TemporaryDirectory() as directory:
            destination = export_multi_csv(
                Path(directory) / "四图分析.csv",
                images,
                stats,
                matches=matches,
                comparisons=comparisons,
            )
            raw = destination.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
