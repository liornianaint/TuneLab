from __future__ import annotations

import tkinter as tk
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self.assertLessEqual(self.app.toolbar_panel.winfo_reqwidth(), 1160)
        self.assertLessEqual(self.app.toolbar_panel.winfo_reqheight(), 96)
        self.assertTrue(self.app.before_view.canvas.bind("<MouseWheel>"))
        if tk.TkVersion >= 9.0:
            self.assertTrue(self.app.before_view.canvas.bind("<TouchpadScroll>"))
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
        self.assertIn("打开图片...", menu_labels)
        self.assertIn("打开图片文件夹...", menu_labels)
        self.assertIn("重新载入当前组", menu_labels)
        self.assertNotIn("打开 Before...", menu_labels)
        self.assertNotIn("打开 After...", menu_labels)
        self.assertTrue(self.app.folder_address_entry.winfo_exists())
        self.assertFalse(hasattr(self.app, "folder_tree"))
        label_texts = []

        def collect_labels(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if child.winfo_class() == "TLabel" and "text" in child.keys():
                    label_texts.append(str(child.cget("text")))
                collect_labels(child)

        collect_labels(self.app.outer)
        self.assertFalse(any("不等同于 Sensor RAW" in text for text in label_texts))
        visible_copy = "\n".join(label_texts) + self.app.compare_text.get("1.0", "end")
        visible_copy += "\n".join(
            str(self.app.compare_tree.heading(column, "text"))
            for column in self.app.compare_tree.cget("columns")
        )
        self.assertNotIn("参考图", visible_copy)
        self.assertNotIn("对比图", visible_copy)

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

    def test_folder_address_bar_loads_and_navigates_image_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index in range(1, 10):
                path = folder / f"scene{index}.png"
                Image.new("RGB", (32 + index, 24 + index), (index * 20, 80, 120)).save(path)
                paths.append(path)
            (folder / "notes.txt").write_text("not an image", encoding="utf-8")
            self.app.open_folder(folder)
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.assertEqual(len(self.app.folder_paths), 9)
            self.assertEqual(self.app.folder_path_var.get(), str(folder.resolve()))
            self.assertEqual(self.app.current_paths, [path.resolve() for path in paths[:4]])
            self.assertEqual(len(self.app.active_roles), 4)
            self.assertEqual(
                [self.app.images[role].path.resolve() for role in self.app.active_roles],
                [path.resolve() for path in paths[:4]],
            )
            self.assertIn("第 1/3 组", self.app.group_status_var.get())
            self.assertEqual(str(self.app.previous_group_button.cget("state")), "disabled")
            self.assertEqual(str(self.app.next_group_button.cget("state")), "normal")
            self.assertEqual(str(self.app.open_comparison_button.cget("state")), "normal")
            self.app.show_comparison()
            self.assertEqual(str(self.app.notebook.select()), str(self.app.compare_tab))

            self.app.show_next_group()
            self.wait_until(lambda: all(
                self.app.images[role] is not None
                and self.app.images[role].path.resolve() == paths[index + 4].resolve()
                for index, role in enumerate(self.app.active_roles)
            ))
            self.assertEqual(self.app.current_paths, [path.resolve() for path in paths[4:8]])
            self.assertIn("第 2/3 组", self.app.group_status_var.get())

            self.app.show_next_group()
            self.wait_until(lambda: self.app.images["before"] is not None)
            self.assertEqual(self.app.current_paths, [paths[8].resolve()])
            self.assertEqual(len(self.app.active_roles), 1)
            self.assertIn("第 3/3 组", self.app.group_status_var.get())
            self.assertEqual(str(self.app.next_group_button.cget("state")), "disabled")
            self.assertEqual(str(self.app.open_comparison_button.cget("state")), "disabled")

            self.app.show_previous_group()
            self.wait_until(lambda: len(self.app.active_roles) == 4 and all(
                self.app.images[role] is not None for role in self.app.active_roles
            ))
            self.assertEqual(self.app.current_paths, [path.resolve() for path in paths[4:8]])

            cached_reference = self.app._image_cache.get(paths[4])
            self.assertIsNotNone(cached_reference)
            self.app.load_selected_images([paths[4]])
            self.assertIs(self.app.images["before"], cached_reference)

    def test_direct_open_loads_up_to_four_images_without_folder_preview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index in range(3):
                path = folder / f"direct{index + 1}.png"
                Image.new("RGB", (80, 60), (40 + index * 30, 90, 130)).save(path)
                paths.append(path)
            with mock.patch(
                "tunelab.image_inspector.ui.filedialog.askopenfilenames",
                return_value=tuple(str(path) for path in paths),
            ):
                self.app.open_images()
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))

        self.assertEqual(len(self.app.active_roles), 3)
        self.assertFalse(self.app.folder_group_mode)
        self.assertEqual(self.app.current_paths, [path.resolve() for path in paths])
        self.assertEqual(str(self.app.previous_group_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.next_group_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.open_comparison_button.cget("state")), "normal")
        self.assertEqual(
            [self.app.images[role].path.name for role in self.app.active_roles],
            [path.name for path in paths],
        )

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
        self.app.before_view.zoom = 1.0
        self.app.before_view._on_mousewheel(SimpleNamespace(delta=120, x=50, y=25))
        self.assertGreater(self.app.before_view.zoom, 1.0)
        if tk.TkVersion >= 9.0:
            self.app.before_view.zoom = 1.0
            self.app.before_view._on_touchpad_scroll(SimpleNamespace(delta=120, x=50, y=25))
            self.assertGreater(self.app.before_view.zoom, 1.0)
        self.app.before_view.zoom = 2.0
        self.app.before_view.pan_x = 10.0
        self.app.before_view.pan_y = 20.0
        self.assertEqual(self.app.before_view.canvas_to_image(30.0, 40.0), (10.0, 10.0))
        self.assertEqual(self.app.before_view.image_to_canvas(10.0, 10.0), (30.0, 40.0))

    def test_wheel_zoom_is_linked_and_image_info_is_inside_each_canvas(self) -> None:
        rgb = np.zeros((60, 80, 3), dtype=np.uint8)
        first = ImageData(
            path=Path("first.png"),
            width=80,
            height=60,
            bit_depth=8,
            source_mode="RGB",
            rgb=rgb,
            display_rgb=rgb,
        )
        second = ImageData(
            path=Path("second.png"),
            width=80,
            height=60,
            bit_depth=8,
            source_mode="RGB",
            rgb=rgb,
            display_rgb=rgb,
        )
        self.app._set_image_count(2)
        self.app.views["before"].set_image(first)
        self.app.views["after"].set_image(second)
        for role in self.app.active_roles:
            view = self.app.views[role]
            view.zoom = 1.0
            view.pan_x = 0.0
            view.pan_y = 0.0
        self.app.views["before"]._on_mousewheel(SimpleNamespace(delta=120, x=40, y=30))
        self.assertAlmostEqual(self.app.views["before"].zoom, 1.15)
        self.assertAlmostEqual(self.app.views["after"].zoom, 1.15)

        self.root.deiconify()
        self.root.update_idletasks()
        for role in self.app.active_roles:
            view = self.app.views[role]
            view._render()
            self.assertEqual(int(view.canvas.grid_info()["row"]), 0)
            self.assertTrue(view.canvas.find_withtag("viewer-chrome"))
            self.assertFalse(any(child.winfo_class() == "TLabel" for child in view.winfo_children()))

    def test_right_drag_moves_existing_render_immediately_and_coalesces_refresh(self) -> None:
        rgb = np.zeros((100, 100, 3), dtype=np.uint8)
        data = ImageData(
            path=Path("pan.png"),
            width=100,
            height=100,
            bit_depth=8,
            source_mode="RGB",
            rgb=rgb,
            display_rgb=rgb,
        )
        view = self.app.before_view
        view.image_data = data
        view.zoom = 2.0
        view.pan_x = -100.0
        view.pan_y = -100.0
        if view._render_after_id is not None:
            view.after_cancel(view._render_after_id)
            view._render_after_id = None
        item = view.canvas.create_rectangle(0, 0, 10, 10, tags=("rendered-image",))
        view._on_pan_press(SimpleNamespace(x=0, y=0))
        view._on_pan_drag(SimpleNamespace(x=10, y=5))
        self.assertEqual([round(value) for value in view.canvas.coords(item)], [10, 5, 20, 15])
        first_render = view._render_after_id
        self.assertIsNotNone(first_render)
        view._on_pan_drag(SimpleNamespace(x=12, y=7))
        self.assertEqual(view._render_after_id, first_render)
        self.assertEqual([round(value) for value in view.canvas.coords(item)], [12, 7, 22, 17])

    def test_fit_view_hides_scrollbars_between_multiple_images(self) -> None:
        rgb = np.zeros((50, 100, 3), dtype=np.float32)
        data = ImageData(
            path=Path("fit.png"),
            width=100,
            height=50,
            bit_depth=8,
            source_mode="RGB",
            rgb=rgb,
            display_rgb=rgb.astype(np.uint8),
        )
        view = self.app.before_view
        view.image_data = data
        with mock.patch.object(view.canvas, "winfo_width", return_value=500), mock.patch.object(
            view.canvas, "winfo_height", return_value=300
        ):
            view.zoom = 1.0
            view._update_scrollbars()
            self.assertFalse(view.horizontal.winfo_manager())
            self.assertFalse(view.vertical.winfo_manager())
            view.zoom = 10.0
            view._update_scrollbars()
            self.assertEqual(view.horizontal.winfo_manager(), "grid")
            self.assertEqual(view.vertical.winfo_manager(), "grid")

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
        self.assertIn("图像 4", self.app.compare_text.get("1.0", "end"))
        self.app.comparison_role_var.set("图像 4")
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
