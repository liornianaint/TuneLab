from __future__ import annotations

import tkinter as tk
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"Image dependencies unavailable: {exc}")

from tunelab.image_inspector.settings import ImageInspectorSettings
from tunelab.image_inspector.types import ImageData, ROI
from tunelab.image_inspector.ui import ImageInspectorWorkspace, WINDOW_TITLE


class ImageInspectorUISmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.load_settings = mock.patch(
            "tunelab.image_inspector.ui.load_image_inspector_settings",
            return_value=ImageInspectorSettings(),
        )
        self.save_settings = mock.patch("tunelab.image_inspector.ui.save_image_inspector_settings")
        self.load_settings.start()
        self.save_settings.start()
        self.app = ImageInspectorWorkspace(self.root)

    def wait_until(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for Image Inspector background work")

    def tearDown(self) -> None:
        if hasattr(self, "app"):
            self.app.shutdown()
        self.save_settings.stop()
        self.load_settings.stop()
        if hasattr(self, "root"):
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def test_module_window_and_empty_actions_do_not_crash(self) -> None:
        self.assertEqual(self.root.title(), WINDOW_TITLE)
        self.assertEqual(len(self.app.active_roles), 1)
        self.assertEqual(self.app.views["before"].winfo_manager(), "grid")
        self.assertFalse(self.app.views["after"].winfo_manager())
        self.root.update_idletasks()
        self.assertLessEqual(self.app.toolbar_panel.winfo_reqwidth(), 1000)
        self.assertLessEqual(self.app.toolbar_panel.winfo_reqheight(), 90)
        self.assertTrue(self.app.before_view.canvas.bind("<MouseWheel>"))
        self.assertTrue(self.app.before_view.canvas.bind("<B2-Motion>"))
        self.app.fit_images()
        self.app.one_to_one()
        self.app.zoom_in()
        self.app.zoom_out()
        self.app.clear_roi()
        with mock.patch("tunelab.image_inspector.ui.messagebox.showinfo") as info:
            self.app.accept_match()
            self.app.export_current()
        self.assertEqual(info.call_count, 2)
        menu_labels = [
            self.app.file_menu.entrycget(index, "label")
            for index in range(self.app.file_menu.index("end") + 1)
            if self.app.file_menu.type(index) != "separator"
        ]
        self.assertIn("打开图片文件夹...", menu_labels)
        self.assertNotIn("打开 Before...", menu_labels)
        self.assertNotIn("打开 After...", menu_labels)
        label_texts = []

        def collect_labels(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if child.winfo_class() == "TLabel" and "text" in child.keys():
                    label_texts.append(str(child.cget("text")))
                collect_labels(child)

        collect_labels(self.app.outer)
        self.assertFalse(any("不等同于 Sensor RAW" in text for text in label_texts))

    def test_one_to_four_image_layouts_without_toplevel(self) -> None:
        for count in range(1, 5):
            self.app._set_image_count(count)
            self.root.update_idletasks()
            visible = [role for role, view in self.app.views.items() if view.winfo_manager() == "grid"]
            self.assertEqual(visible, list(self.app.active_roles))
            self.assertEqual(len(visible), count)
            self.assertEqual(len(self.app.histogram_role_combo.cget("values")), count)
        self.assertEqual(int(self.app.views["before"].grid_info()["rowspan"]), 1)
        self.assertFalse(any(isinstance(child, tk.Toplevel) for child in self.root.winfo_children()))

    def test_folder_browser_previews_and_loads_one_to_four_images(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index in range(1, 6):
                path = folder / f"scene{index}.png"
                Image.new("RGB", (32 + index, 24 + index), (index * 20, 80, 120)).save(path)
                paths.append(path)
            (folder / "notes.txt").write_text("not an image", encoding="utf-8")
            self.app.open_folder(folder)
            self.wait_until(lambda: self.app.images["before"] is not None)
            self.assertEqual(len(self.app.folder_paths), 5)
            self.assertEqual(len(self.app.folder_tree.get_children()), 5)
            self.wait_until(lambda: bool(self.app._thumbnail_photos))

            all_items = self.app.folder_tree.get_children()
            self.app.folder_tree.selection_set(*all_items)
            self.app._on_folder_selection()
            self.assertEqual(len(self.app.folder_tree.selection()), 4)
            self.app.load_selected_images()
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.assertEqual(len(self.app.active_roles), 4)
            self.assertEqual(
                [self.app.images[role].path.resolve() for role in self.app.active_roles],
                [path.resolve() for path in paths[:4]],
            )

            for count in range(1, 5):
                self.app.load_selected_images(paths[:count])
                self.wait_until(lambda c=count: len(self.app.active_roles) == c and all(
                    self.app.images[role] is not None for role in self.app.active_roles
                ))
                self.assertEqual(len(self.app.active_roles), count)
            cached_reference = self.app._image_cache.get(paths[0])
            self.assertIsNotNone(cached_reference)
            self.app.load_selected_images([paths[0]])
            self.assertIs(self.app.images["before"], cached_reference)

    def test_canvas_coordinate_mapping_is_independent_of_render_resize(self) -> None:
        rgb = np.zeros((50, 100, 3), dtype=np.float32)
        data = ImageData(
            path=Path("mapping.png"),
            width=100,
            height=50,
            bit_depth=8,
            source_mode="RGB",
            rgb=rgb,
            display_rgb=rgb.astype(np.uint8),
        )
        self.app.images["before"] = data
        self.app.before_view.set_image(data)
        self.app.before_view.zoom = 1.0
        self.app.before_view._update_zoom_label()
        self.app.before_view.zoom_by(1.25)
        self.assertAlmostEqual(self.app.before_view.zoom, 1.25)
        self.assertEqual(self.app.before_view.zoom_var.get(), "缩放 125%")
        self.app.before_view.zoom = 2.0
        self.app.before_view.pan_x = 10.0
        self.app.before_view.pan_y = 20.0
        self.assertEqual(self.app.before_view.canvas_to_image(30.0, 40.0), (10.0, 10.0))
        self.assertEqual(self.app.before_view.image_to_canvas(10.0, 10.0), (30.0, 40.0))

    def test_reference_roi_matches_three_comparison_images(self) -> None:
        rng = np.random.default_rng(2026)
        base = rng.integers(20, 235, size=(90, 110, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index, shift in enumerate((0, 3, 7, 11), start=1):
                pixels = np.roll(base, shift=(shift, shift), axis=(0, 1))
                path = folder / f"compare{index}.png"
                Image.fromarray(pixels, mode="RGB").save(path)
                paths.append(path)
            self.app.load_selected_images(paths)
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.app._on_roi("before", ROI(30, 25, 28, 26, "纹理区域"))
            self.wait_until(lambda: all(
                self.app.match_results[role] is not None and self.app.roi_statistics[role] is not None
                for role in self.app.active_roles[1:]
            ), timeout=5.0)
            self.wait_until(lambda: all(self.app.comparisons[role] is not None for role in self.app.active_roles[1:]))
        self.assertEqual(len(self.app.active_roles), 4)
        self.assertTrue(all(self.app.match_results[role].score > 0.9 for role in self.app.active_roles[1:]))
        self.assertIn("对比图 4", self.app.compare_text.get("1.0", "end"))
        self.app.comparison_role_var.set("对比图 4")
        self.app._refresh_comparison_table()
        metrics = {
            self.app.compare_tree.set(item, "metric"): self.app.compare_tree.item(item, "values")
            for item in self.app.compare_tree.get_children()
        }
        self.assertIn("Mean R", metrics)
        self.assertIn("Lab b*", metrics)
        self.assertTrue(any(str(values[4]).startswith(("↑", "↓", "≈")) for values in metrics.values()))
        self.assertIn("compare1.png", self.app.comparison_files_var.get())
        self.assertIn("compare4.png", self.app.comparison_files_var.get())
        self.assertIn("匹配置信度", self.app.comparison_gate_var.get())
        self.assertNotEqual(self.app.reference_swatch.cget("background"), "#DDE3EC")
        self.assertNotEqual(self.app.target_swatch.cget("background"), "#DDE3EC")

    def test_histogram_tab_can_be_hidden_and_restored(self) -> None:
        self.app.show_histogram_var.set(False)
        self.app._on_histogram_visibility_changed()
        self.app.show_histogram_var.set(True)
        self.app._on_histogram_visibility_changed()
        self.assertIn(str(self.app.histogram_tab), [str(tab) for tab in self.app.notebook.tabs()])
        self.assertEqual(self.app.notebook.tab(self.app.histogram_tab, "state"), "normal")


if __name__ == "__main__":
    unittest.main()
