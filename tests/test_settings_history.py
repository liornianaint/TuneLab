from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from matrixcorrect.history import load_history, record_from_result, save_history
from matrixcorrect.imatest import parse_imatest_csv
from matrixcorrect.models import OptimizationConfig
from matrixcorrect.optimizer import optimize_ccm
from matrixcorrect.qualcomm_xml import QualcommCCDocument
from matrixcorrect.settings import AppSettings, load_settings, save_settings


ROOT = Path(__file__).resolve().parents[1]


class SettingsHistoryTests(unittest.TestCase):
    def test_parameter_round_trip(self) -> None:
        config = OptimizationConfig(strategy="conservative", saturation_factor=0.98, focus_patches=(1, 13, 15), focus_weight=5.0)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            save_settings(AppSettings(optimization=config, show_motion=False), path)
            loaded = load_settings(path)
            self.assertEqual(loaded.optimization.focus_patches, (1, 13, 15))
            self.assertEqual(loaded.optimization.strategy, "conservative")
            self.assertFalse(loaded.show_motion)

    def test_settings_json_is_standard_and_every_value_is_documented(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            save_settings(AppSettings(), path)
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)

        self.assertEqual(payload["version"], 2)
        self.assertIn("values", payload)
        self.assertIn("descriptions", payload)
        self.assertNotIn("//", raw)
        self.assertNotIn("/*", raw)
        optimization_values = payload["values"]["optimization"]
        optimization_descriptions = payload["descriptions"]["optimization"]
        self.assertEqual(set(optimization_values), set(optimization_descriptions))
        self.assertEqual(
            set(payload["descriptions"]["application"]),
            {"composition", "show_motion", "last_report_format"},
        )
        self.assertIn("不会重新运行优化", payload["descriptions"]["application"]["show_motion"]["impact"])
        for description in (
            list(optimization_descriptions.values())
            + list(payload["descriptions"]["application"].values())
        ):
            self.assertEqual(
                set(description),
                {"purpose", "default", "recommended_range", "impact"},
            )
            self.assertTrue(description["purpose"])
            self.assertTrue(description["recommended_range"])
            self.assertTrue(description["impact"])

    def test_version_one_settings_remain_readable(self) -> None:
        legacy = {
            "version": 1,
            "optimization": OptimizationConfig(strategy="aggressive").to_dict(),
            "composition": "post_transposed",
            "show_motion": False,
            "last_report_format": "xlsx",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = load_settings(path)
        self.assertEqual(loaded.optimization.strategy, "aggressive")
        self.assertEqual(loaded.composition, "post_transposed")
        self.assertFalse(loaded.show_motion)
        self.assertEqual(loaded.last_report_format, "xlsx")

    def test_history_round_trip(self) -> None:
        dataset = parse_imatest_csv(ROOT / "Source" / "D65_normal_summary.csv")
        document = QualcommCCDocument.load(ROOT / "Source" / "cc13_ipe_v2.xml")
        region, _ = document.find_region_for_cct(6500)
        result = optimize_ccm(dataset, region.matrix)
        record = record_from_result(result, dataset_name=dataset.source_path.name, region_label=region.path_label())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.json"
            save_history([record], path)
            loaded = load_history(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].optimized_matrix, record.optimized_matrix)
            self.assertEqual(loaded[0].matrix_status, "PASS")
