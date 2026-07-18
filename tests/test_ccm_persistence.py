from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tunelab.ccm.history import load_history, record_from_result, save_history
from tunelab.ccm.imatest import parse_imatest_csv
from tunelab.ccm.models import OptimizationConfig
from tunelab.ccm.optimizer import optimize_ccm
from tunelab.ccm.qualcomm_xml import QualcommCCDocument
from tunelab.ccm.settings import CcmSettings, application_data_dir, load_settings, save_settings


ROOT = Path(__file__).resolve().parents[1]


class CcmPersistenceTests(unittest.TestCase):
    def test_tunelab_data_directory_reads_legacy_settings_then_saves_current_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_directory = root / "MatrixCorrect"
            legacy_directory.mkdir()
            legacy_path = legacy_directory / "settings.json"
            legacy_path.write_text(
                json.dumps(CcmSettings(composition="post_transposed").to_dict()),
                encoding="utf-8",
            )
            data_directory = mock.patch(
                "tunelab.ccm.settings._platform_data_dir",
                side_effect=lambda name: root / name,
            )
            with data_directory:
                self.assertEqual(application_data_dir(), root / "TuneLab")
                self.assertEqual(load_settings().composition, "post_transposed")
                current_path = save_settings(CcmSettings(composition="pre"))

            self.assertEqual(current_path, root / "TuneLab" / "settings.json")
            self.assertTrue(current_path.exists())

    def test_parameter_round_trip(self) -> None:
        config = OptimizationConfig(strategy="conservative", saturation_factor=0.98, focus_patches=(1, 13, 15), focus_weight=5.0)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            save_settings(CcmSettings(optimization=config, show_motion=False), path)
            loaded = load_settings(path)
            self.assertEqual(loaded.optimization.focus_patches, (1, 13, 15))
            self.assertEqual(loaded.optimization.strategy, "conservative")
            self.assertFalse(loaded.show_motion)

    def test_settings_json_is_standard_and_every_value_is_documented(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            save_settings(CcmSettings(), path)
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
        from .materials import CC_XML, d65_dataset

        dataset = d65_dataset()
        document = QualcommCCDocument.load(CC_XML)
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
