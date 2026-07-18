from __future__ import annotations

import tkinter as tk
import tempfile
import time
import unittest
import gc
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    import numpy as np
    from PIL import Image
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"Image dependencies unavailable: {exc}")

from tunelab.image_inspector.settings import ImageInspectorSettings
from tunelab.image_inspector.browser import ThumbnailData
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

    def install_synthetic_images(self, count: int) -> None:
        self.app._set_image_count(count)
        for index, role in enumerate(self.app.active_roles):
            pixels = np.full((32, 48, 3), (40 + index * 20, 80, 120), dtype=np.uint8)
            image_data = ImageData(
                path=Path(f"synthetic-{index + 1}.png"),
                width=48,
                height=32,
                bit_depth=8,
                source_mode="RGB",
                rgb=pixels,
                display_rgb=pixels,
                histogram=np.ones((3, 256), dtype=np.int64),
                luminance_histogram=np.ones(256, dtype=np.int64),
            )
            self.app.images[role] = image_data
            self.app.views[role].set_image(image_data)
        self.app._refresh_information_sidebar()

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
        self.assertTrue(self.app.folder_thumbnail_strip.winfo_exists())
        self.assertFalse(hasattr(self.app, "folder_tree"))
        self.assertFalse(hasattr(self.app, "open_comparison_button"))
        self.assertEqual(str(self.app.sidebar_toggle_button.cget("text")), "收起信息")
        self.assertEqual(str(self.app.sidebar_header_toggle_button.cget("text")), "›")
        self.assertEqual(str(self.app.sidebar_header_toggle_button.cget("style")), "Icon.TButton")
        self.assertEqual(self.app.toolbar_panel.grid_slaves(row=1), [])
        self.assertFalse(self.app.progress.winfo_manager())
        self.assertEqual(self.app.folder_path_var.get(), "")
        self.assertEqual(self.app.group_status_var.get(), "")
        self.assertFalse(self.app.metrics_grid.winfo_manager())
        self.app.before_view._render()
        self.assertFalse(self.app.before_view.canvas.find_withtag("viewer-chrome"))
        self.assertTrue(all(not variable.get() for variable in self.app.metric_vars.values()))
        label_texts = []
        widget_texts = []

        def collect_labels(widget: tk.Misc) -> None:
            for child in widget.winfo_children():
                if "text" in child.keys():
                    widget_texts.append(str(child.cget("text")))
                if child.winfo_class() == "TLabel" and "text" in child.keys():
                    label_texts.append(str(child.cget("text")))
                collect_labels(child)

        collect_labels(self.app.outer)
        self.assertFalse(any("不等同于 Sensor RAW" in text for text in label_texts))
        visible_copy = "\n".join(label_texts)
        visible_copy += "\n".join(
            str(self.app.compare_tree.heading(column, "text"))
            for column in self.app.compare_tree.cget("columns")
        )
        self.assertNotIn("参考图", visible_copy)
        self.assertNotIn("对比图", visible_copy)
        self.assertNotIn("详细数据", visible_copy)
        self.assertNotIn("结论", visible_copy)
        self.assertNotIn("macOS 式图库选择", visible_copy)
        self.assertNotIn("Mean RGB 是 0–255", visible_copy)
        self.assertNotIn("将当前选区视为中性区域", widget_texts)
        self.assertFalse(hasattr(self.app, "pixel_tab"))
        self.assertFalse(hasattr(self.app, "conclusion_tab"))
        self.assertFalse(hasattr(self.app, "compare_text"))

    def test_one_to_four_image_layouts_without_toplevel(self) -> None:
        for count in range(1, 5):
            self.app._set_image_count(count)
            self.root.update_idletasks()
            visible = [role for role, view in self.app.views.items() if view.winfo_manager() == "grid"]
            self.assertEqual(visible, list(self.app.active_roles))
            self.assertEqual(len(visible), count)
            visible_metrics = [
                role for role, label in self.app.metric_labels.items() if label.winfo_manager() == "grid"
            ]
            visible_controls = [
                role for role, row in self.app.info_control_rows.items() if row.winfo_manager() == "grid"
            ]
            self.assertEqual(visible_metrics, list(self.app.active_roles))
            self.assertEqual(visible_controls, [])
            if count == 3:
                layout = [self.app.views[role].grid_info() for role in self.app.active_roles]
                self.assertEqual([int(item["row"]) for item in layout], [0, 0, 0])
                self.assertEqual([int(item["column"]) for item in layout], [0, 1, 2])
                self.assertEqual([int(item["rowspan"]) for item in layout], [2, 2, 2])
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
            self.assertEqual(self.app.current_paths, [paths[0].resolve()])
            self.assertEqual(len(self.app.active_roles), 1)
            self.assertEqual(self.app.images["before"].path.resolve(), paths[0].resolve())
            self.assertIn("第 1/9 组", self.app.group_status_var.get())
            self.assertEqual(str(self.app.previous_group_button.cget("state")), "disabled")
            self.assertEqual(str(self.app.next_group_button.cget("state")), "normal")
            panes = [str(pane) for pane in self.app.main_pane.panes()]
            self.assertEqual(
                panes,
                [
                    str(self.app.folder_thumbnail_strip),
                    str(self.app.viewer_container),
                    str(self.app.sidebar_frame),
                ],
            )
            self.assertEqual(self.app.folder_thumbnail_strip.winfo_manager(), "panedwindow")
            self.assertEqual(len(self.app.folder_thumbnail_strip.cards), 9)
            self.wait_until(lambda: len(self.app.folder_thumbnail_strip._photos) >= 4)
            self.assertEqual(
                (
                    self.app.folder_thumbnail_strip._photos[0].width(),
                    self.app.folder_thumbnail_strip._photos[0].height(),
                ),
                self.app.folder_thumbnail_strip.PREVIEW_SIZE,
            )
            selected_colours = [
                self.app.folder_thumbnail_strip.cards[index].itemcget("selection-background", "fill")
                for index in range(4)
            ]
            self.assertEqual(selected_colours[0], "#007AFF")
            self.assertNotIn("#007AFF", selected_colours[1:])
            self.root.deiconify()
            self.wait_until(lambda: self.app.main_pane.winfo_width() > 800)
            self.app._restore_panel_ratio()
            self.root.update_idletasks()
            left_width = self.app.main_pane.sashpos(0)
            middle_width = self.app.main_pane.sashpos(1) - left_width
            self.assertLess(left_width, middle_width)

            self.app.toggle_folder_sidebar()
            self.root.update_idletasks()
            self.assertFalse(self.app._folder_sidebar_is_visible())
            self.assertEqual(str(self.app.folder_sidebar_toggle_button.cget("text")), "显示图库")
            self.app.toggle_folder_sidebar()
            self.root.update_idletasks()
            self.assertTrue(self.app._folder_sidebar_is_visible())
            self.assertEqual(str(self.app.folder_sidebar_toggle_button.cget("text")), "收起图库")

            self.app.sidebar_header_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertEqual(
                [str(pane) for pane in self.app.main_pane.panes()],
                [str(self.app.folder_thumbnail_strip), str(self.app.viewer_container)],
            )
            self.assertEqual(str(self.app.sidebar_toggle_button.cget("text")), "显示信息")

            self.app.folder_sidebar_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertEqual(
                [str(pane) for pane in self.app.main_pane.panes()],
                [str(self.app.viewer_container)],
            )
            self.assertEqual(str(self.app.sidebar_toggle_button.cget("text")), "显示信息")

            self.app.folder_sidebar_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertEqual(
                [str(pane) for pane in self.app.main_pane.panes()],
                [str(self.app.folder_thumbnail_strip), str(self.app.viewer_container)],
            )
            self.assertFalse(self.app._analysis_sidebar_is_visible())

            self.app.sidebar_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertEqual(
                [str(pane) for pane in self.app.main_pane.panes()],
                [
                    str(self.app.folder_thumbnail_strip),
                    str(self.app.viewer_container),
                    str(self.app.sidebar_frame),
                ],
            )
            self.assertGreater(self.app.sidebar_frame.winfo_width(), 100)

            self.app.folder_sidebar_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertEqual(
                [str(pane) for pane in self.app.main_pane.panes()],
                [str(self.app.viewer_container), str(self.app.sidebar_frame)],
            )
            self.assertTrue(self.app._analysis_sidebar_is_visible())
            self.app.folder_sidebar_toggle_button.invoke()
            self.root.update_idletasks()
            self.app.show_next_group()
            self.wait_until(lambda: self.app.images["before"] is not None and self.app.images["before"].path == paths[1].resolve())
            self.assertEqual(self.app.current_paths, [paths[1].resolve()])
            self.assertIn("第 2/9 组", self.app.group_status_var.get())

            self.app.folder_thumbnail_strip.cards[0].event_generate("<Button-1>")
            self.wait_until(lambda: self.app.current_paths == [paths[0].resolve()])
            for index in range(1, 4):
                self.app.folder_thumbnail_strip.cards[index].event_generate("<Button-1>", state=0x0010)
                self.wait_until(lambda expected=index + 1: len(self.app.active_roles) == expected)
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.assertEqual(self.app.current_paths, [path.resolve() for path in paths[:4]])
            self.assertEqual(len(self.app.active_roles), 4)
            self.assertEqual(
                [
                    self.app.folder_thumbnail_strip.cards[index].itemcget("selection-background", "fill")
                    for index in range(4)
                ],
                ["#007AFF"] * 4,
            )
            self.app.show_comparison()
            self.assertEqual(str(self.app.notebook.select()), str(self.app.compare_tab))

            self.app.folder_thumbnail_strip.cards[4].event_generate("<Button-1>")
            self.wait_until(lambda: self.app.current_paths == [paths[4].resolve()])
            self.assertEqual(len(self.app.active_roles), 1)

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
        self.assertFalse(self.app._folder_sidebar_is_visible())
        self.assertEqual(
            [self.app.images[role].path.name for role in self.app.active_roles],
            [path.name for path in paths],
        )
        self.root.deiconify()
        self.wait_until(lambda: all(self.app.views[role].winfo_width() > 100 for role in self.app.active_roles))
        widths = [self.app.views[role].winfo_width() for role in self.app.active_roles]
        self.assertLessEqual(max(widths) - min(widths), 3)

    def test_direct_open_two_images_can_navigate_to_remaining_folder_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index in range(4):
                path = folder / f"pair{index + 1}.png"
                Image.new("RGB", (90, 70), (50 + index * 25, 80, 120)).save(path)
                paths.append(path)
            self.app.open_images(paths[:2])
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.assertTrue(self.app.folder_group_mode)
            self.assertEqual(self.app.folder_group_size, 2)
            self.assertEqual(self.app.folder_paths, [path.resolve() for path in paths])
            self.assertEqual(str(self.app.next_group_button.cget("state")), "normal")

            self.app.show_next_group()
            self.wait_until(lambda: all(
                self.app.images[role] is not None
                and self.app.images[role].path.resolve() == paths[index + 2].resolve()
                for index, role in enumerate(self.app.active_roles)
            ))
            self.assertEqual(self.app.current_paths, [path.resolve() for path in paths[2:]])
            self.assertEqual(str(self.app.previous_group_button.cget("state")), "normal")
            self.assertEqual(str(self.app.next_group_button.cget("state")), "disabled")

            self.app.show_previous_group()
            self.wait_until(lambda: all(
                self.app.images[role] is not None
                and self.app.images[role].path.resolve() == paths[index].resolve()
                for index, role in enumerate(self.app.active_roles)
            ))
            self.assertEqual(self.app.current_paths, [path.resolve() for path in paths[:2]])

    def test_direct_open_uses_natural_order_and_navigates_backward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index in range(1, 5):
                path = folder / f"ordered{index}.png"
                Image.new("RGB", (90, 70), (40 * index, 80, 120)).save(path)
                paths.append(path)

            self.app.open_images([paths[3], paths[2]])
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.assertEqual(self.app.current_paths, [paths[2].resolve(), paths[3].resolve()])
            self.assertEqual(self.app.folder_paths, [path.resolve() for path in paths])
            self.assertEqual(str(self.app.previous_group_button.cget("state")), "normal")
            self.assertEqual(str(self.app.next_group_button.cget("state")), "disabled")

            self.app.show_previous_group()
            self.wait_until(lambda: all(
                self.app.images[role] is not None
                and self.app.images[role].path.resolve() == paths[index].resolve()
                for index, role in enumerate(self.app.active_roles)
            ))
            self.assertEqual(self.app.current_paths, [paths[0].resolve(), paths[1].resolve()])
            self.assertEqual(str(self.app.next_group_button.cget("state")), "normal")

            self.app.open_images([paths[2], paths[0]])
            self.wait_until(lambda: all(
                self.app.images[role] is not None
                and self.app.images[role].path.resolve() == paths[index * 2].resolve()
                for index, role in enumerate(self.app.active_roles)
            ))
            self.assertEqual(self.app.current_paths, [paths[0].resolve(), paths[2].resolve()])
            self.assertEqual(str(self.app.next_group_button.cget("state")), "normal")
            self.app.show_next_group()
            self.wait_until(lambda: all(
                self.app.images[role] is not None
                and self.app.images[role].path.resolve() == paths[index * 2 + 1].resolve()
                for index, role in enumerate(self.app.active_roles)
            ))
            self.assertEqual(self.app.current_paths, [paths[1].resolve(), paths[3].resolve()])

    def test_analysis_data_is_a_narrow_collapsible_right_sidebar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index in range(2):
                path = folder / f"sidebar{index + 1}.png"
                Image.new("RGB", (640, 480), (60 + index * 30, 80, 120)).save(path)
                paths.append(path)
            self.app.open_images(paths)
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.root.deiconify()
            self.wait_until(lambda: self.app.main_pane.winfo_width() > 800)
            self.app._restore_panel_ratio()
            self.root.update_idletasks()

            panes = [str(pane) for pane in self.app.main_pane.panes()]
            self.assertEqual(str(self.app.main_pane.cget("orient")), "horizontal")
            self.assertEqual(panes, [str(self.app.viewer_container), str(self.app.sidebar_frame)])
            sash = self.app.main_pane.sashpos(0)
            self.assertGreater(sash, self.app.main_pane.winfo_width() - sash)
            self.assertLessEqual(
                (self.app.main_pane.winfo_width() - sash) / self.app.main_pane.winfo_width(),
                0.36,
            )

            self.app.sidebar_header_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertFalse(self.app._analysis_sidebar_is_visible())
            self.assertEqual(str(self.app.sidebar_toggle_button.cget("text")), "显示信息")
            self.assertEqual([str(pane) for pane in self.app.main_pane.panes()], [str(self.app.viewer_container)])

            self.app.sidebar_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertTrue(self.app._analysis_sidebar_is_visible())
            self.assertEqual(str(self.app.sidebar_toggle_button.cget("text")), "收起信息")
            self.assertEqual(
                [str(pane) for pane in self.app.main_pane.panes()],
                [str(self.app.viewer_container), str(self.app.sidebar_frame)],
            )
            self.assertGreater(self.app.sidebar_frame.winfo_width(), 100)

            self.app.sidebar_toggle_button.invoke()
            self.root.update_idletasks()
            self.assertFalse(self.app._analysis_sidebar_is_visible())

            self.app.show_comparison()
            self.root.update_idletasks()
            self.assertTrue(self.app._analysis_sidebar_is_visible())
            self.assertEqual(str(self.app.main_pane.panes()[-1]), str(self.app.sidebar_frame))
            self.assertEqual(str(self.app.notebook.select()), str(self.app.compare_tab))

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

    def test_bottom_metrics_follow_linked_click_and_roi_for_every_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first_path = folder / "metrics1.png"
            second_path = folder / "metrics2.png"
            Image.new("RGB", (160, 120), (100, 50, 25)).save(first_path)
            Image.new("RGB", (160, 120), (40, 80, 20)).save(second_path)
            self.app.open_images([first_path, second_path])
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))

            self.app._on_pixel("before", 40, 30, True)
            self.assertEqual(self.app._metric_mode, "pixel")
            self.assertIn("R:100", self.app.metric_vars["before"].get())
            self.assertIn("G:50", self.app.metric_vars["before"].get())
            self.assertIn("B:25", self.app.metric_vars["before"].get())
            self.assertIn("R/G:2.000", self.app.metric_vars["before"].get())
            self.assertIn("R/B:4.000", self.app.metric_vars["before"].get())
            self.assertIn("R:40", self.app.metric_vars["after"].get())
            self.assertIn("R/G:0.500", self.app.metric_vars["after"].get())
            self.assertIn("R/B:2.000", self.app.metric_vars["after"].get())
            self.assertIn(" · ", self.app.metric_vars["before"].get())
            self.root.update_idletasks()
            self.assertLessEqual(self.app.metrics_grid.winfo_reqheight(), 24)

            self.app._on_roi("before", ROI(24, 24, 40, 32))
            self.wait_until(
                lambda: all(self.app.roi_statistics[role] is not None for role in self.app.active_roles),
                timeout=5.0,
            )
            self.assertEqual(self.app._metric_mode, "roi")
            self.assertIn("R:100", self.app.metric_vars["before"].get())
            self.assertIn("R:40", self.app.metric_vars["after"].get())
            self.assertNotIn("ROI", self.app.metric_vars["before"].get())
            self.assertNotIn("分析", self.app.metric_vars["after"].get())

    def test_right_inspector_displays_loaded_exif_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "exif.jpg"
            image = Image.new("RGB", (80, 60), (70, 90, 110))
            exif = Image.Exif()
            exif[271] = "TuneLab Camera"
            exif[272] = "TL-1"
            image.save(path, exif=exif)
            self.app.open_images([path])
            self.wait_until(lambda: self.app.images["before"] is not None)

        self.assertIn("TuneLab Camera", self.app.exif_vars["before"].get())
        self.assertIn("TL-1", self.app.exif_vars["before"].get())
        self.assertEqual(self.app.exif_frames["before"].winfo_manager(), "pack")
        self.app.exif_visible_vars["before"].set(False)
        self.app._on_info_visibility_changed("before")
        self.assertFalse(self.app.exif_frames["before"].winfo_manager())

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

    def test_opening_multiple_images_fits_every_visible_canvas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            sizes = ((1200, 800), (600, 1400), (1600, 500), (720, 720))
            paths = []
            for index, size in enumerate(sizes, start=1):
                path = folder / f"fit-{index}.png"
                Image.new("RGB", size, (40 * index, 70, 110)).save(path)
                paths.append(path)
            self.app.open_images(paths)
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))
            self.root.deiconify()
            self.wait_until(lambda: all(
                self.app.views[role].canvas.winfo_width() > 16
                and self.app.views[role].canvas.winfo_height() > 16
                and not self.app.views[role]._needs_initial_fit
                for role in self.app.active_roles
            ))

            for role in self.app.active_roles:
                view = self.app.views[role]
                assert view.image_data is not None
                expected = min(
                    (view.canvas.winfo_width() - 16) / view.image_data.width,
                    (view.canvas.winfo_height() - 16) / view.image_data.height,
                )
                self.assertAlmostEqual(view.zoom, expected, places=6)
                self.assertAlmostEqual(
                    view.pan_x,
                    (view.canvas.winfo_width() - view.image_data.width * expected) / 2.0,
                    places=5,
                )
                self.assertAlmostEqual(
                    view.pan_y,
                    (view.canvas.winfo_height() - view.image_data.height * expected) / 2.0,
                    places=5,
                )

    def test_roi_can_start_on_any_image_and_other_rois_remain_individually_adjustable(self) -> None:
        rng = np.random.default_rng(77)
        base = rng.integers(20, 235, size=(96, 120, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            paths = []
            for index, shift in enumerate((0, 4, 8), start=1):
                path = folder / f"anchor-{index}.png"
                Image.fromarray(np.roll(base, shift=(shift, shift), axis=(0, 1)), mode="RGB").save(path)
                paths.append(path)
            self.app.load_selected_images(paths)
            self.wait_until(lambda: all(self.app.images[role] is not None for role in self.app.active_roles))

            anchor_roi = ROI(34, 28, 30, 26, "ROI 1")
            self.app._on_roi("compare3", anchor_roi)
            self.wait_until(
                lambda: all(
                    self.app.roi_statistics[role] is not None
                    for role in self.app.active_roles
                )
                and self.app.match_results["before"] is not None
                and self.app.match_results["after"] is not None,
                timeout=5.0,
            )
            self.wait_until(
                lambda: self.app.comparisons["after"] is not None
                and self.app.comparisons["compare3"] is not None
            )

            self.assertEqual(self.app.roi_anchor_role, "compare3")
            self.assertEqual(self.app.rois["compare3"], anchor_roi)
            self.assertGreater(self.app.match_results["before"].score, 0.9)
            self.assertGreater(self.app.match_results["after"].score, 0.9)
            automatically_matched_before = self.app.rois["before"]

            adjusted_after = ROI(40, 34, 30, 26, "ROI 1")
            self.app._on_roi("after", adjusted_after)
            self.wait_until(
                lambda: self.app.roi_statistics["after"] is not None
                and self.app.roi_statistics["after"].roi == adjusted_after
            )
            self.assertEqual(self.app.roi_anchor_role, "compare3")
            self.assertEqual(self.app.rois["compare3"], anchor_roi)
            self.assertEqual(self.app.rois["before"], automatically_matched_before)
            self.assertTrue(self.app.match_results["after"].manually_confirmed)

            adjusted_before = ROI(26, 22, 30, 26, "ROI 1")
            self.app._on_roi("before", adjusted_before)
            self.wait_until(
                lambda: self.app.roi_statistics["before"] is not None
                and self.app.roi_statistics["before"].roi == adjusted_before
            )
            self.assertEqual(self.app.rois["after"], adjusted_after)
            self.assertEqual(self.app.rois["compare3"], anchor_roi)
            self.assertTrue(self.app.match_results["before"].manually_confirmed)

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
        self.app.comparison_base_var.set("图像 2")
        self.app.comparison_role_var.set("图像 4")
        self.app._refresh_comparison_table()
        metrics = {
            self.app.compare_tree.set(item, "metric"): self.app.compare_tree.item(item, "values")
            for item in self.app.compare_tree.get_children()
        }
        self.assertIn("Mean R（0–255）", metrics)
        self.assertIn("R 占比 %", metrics)
        self.assertIn("Lab b*", metrics)
        self.assertTrue(any(str(values[4]).startswith(("↑", "↓", "≈")) for values in metrics.values()))
        change_values = [str(values[4]) for values in metrics.values()]
        self.assertTrue(all(value == "—" or value.endswith("%") for value in change_values))
        self.assertFalse(any("上升" in value or "下降" in value or "基本不变" in value for value in change_values))
        self.assertFalse(any("百分" in value for value in change_values))
        self.assertIn("compare2.png", self.app.comparison_files_var.get())
        self.assertIn("compare4.png", self.app.comparison_files_var.get())
        self.assertEqual(self.app.compare_tree.heading("reference", "text"), "图像 2")
        self.assertEqual(self.app.compare_tree.heading("target", "text"), "图像 4")
        for column in self.app.compare_tree.cget("columns"):
            self.assertEqual(
                str(self.app.compare_tree.heading(column, "anchor")),
                str(self.app.compare_tree.column(column, "anchor")),
            )
        self.assertIn("匹配置信度", self.app.comparison_gate_var.get())
        self.assertNotEqual(self.app.reference_swatch.cget("background"), "#DDE3EC")
        self.assertNotEqual(self.app.target_swatch.cget("background"), "#DDE3EC")

        before_delta = float(metrics["Mean R（0–255）"][3])
        self.app._swap_comparison_pair()
        swapped_metrics = {
            self.app.compare_tree.set(item, "metric"): self.app.compare_tree.item(item, "values")
            for item in self.app.compare_tree.get_children()
        }
        self.assertAlmostEqual(float(swapped_metrics["Mean R（0–255）"][3]), -before_delta, places=2)
        self.assertIn("compare4.png", self.app.comparison_files_var.get().split("→")[0])

        self.app.notebook.select(self.app.compare_tab)
        self.root.deiconify()
        self.wait_until(lambda: self.app.compare_tree.winfo_width() > 100)
        total_column_width = sum(
            int(self.app.compare_tree.column(column, "width"))
            for column in self.app.compare_tree.cget("columns")
        )
        if total_column_width > self.app.compare_tree.winfo_width():
            self.app.compare_tree.xview_moveto(1.0)
            self.root.update_idletasks()
            self.assertGreater(self.app.compare_tree.xview()[0], 0.0)
            self.assertAlmostEqual(self.app.compare_tree.xview()[1], 1.0, places=6)
        else:
            self.assertEqual(tuple(float(value) for value in self.app.compare_tree.xview()), (0.0, 1.0))

    def test_each_image_histogram_and_exif_can_be_hidden_and_restored(self) -> None:
        self.install_synthetic_images(2)
        self.app.show_histogram_var.set(False)
        self.app._on_histogram_visibility_changed()
        self.assertTrue(all(not variable.get() for variable in self.app.histogram_visible_vars.values()))
        self.assertFalse(self.app.histogram_canvases["before"].winfo_manager())
        self.assertEqual(self.app.exif_frames["before"].winfo_manager(), "pack")

        self.app.show_histogram_var.set(True)
        self.app._on_histogram_visibility_changed()
        self.assertTrue(all(variable.get() for variable in self.app.histogram_visible_vars.values()))
        self.assertEqual(self.app.histogram_canvases["before"].winfo_manager(), "pack")

        self.app.luminance_histogram_visible_vars["before"].set(True)
        self.app._on_info_visibility_changed("before")
        self.assertEqual(self.app.luminance_histogram_canvases["before"].winfo_manager(), "pack")
        self.assertTrue(self.app.luminance_histogram_canvases["before"].luminance)

        self.app.histogram_visible_vars["after"].set(False)
        self.app.luminance_histogram_visible_vars["after"].set(False)
        self.app.exif_visible_vars["after"].set(False)
        self.app._on_info_visibility_changed("after")
        self.assertFalse(self.app.info_sections["after"].winfo_manager())

        self.app.show_luminance_histogram_var.set(True)
        self.app._on_global_info_visibility_changed("luminance")
        self.assertTrue(
            all(variable.get() for variable in self.app.luminance_histogram_visible_vars.values())
        )
        self.app.show_exif_var.set(False)
        self.app._on_global_info_visibility_changed("exif")
        self.assertTrue(all(not variable.get() for variable in self.app.exif_visible_vars.values()))
        self.assertEqual(
            [self.app.notebook.tab(tab, "text") for tab in self.app.notebook.tabs()],
            ["直方图 / EXIF", "对比"],
        )

    def test_information_content_scrolls_from_controls_histograms_and_exif(self) -> None:
        self.install_synthetic_images(4)
        for variable in self.app.luminance_histogram_visible_vars.values():
            variable.set(True)
        self.app._refresh_information_sidebar()
        self.root.deiconify()
        self.wait_until(
            lambda: self.app.info_canvas.bbox("all") is not None
            and self.app.info_canvas.bbox("all")[3] > self.app.info_canvas.winfo_height()
        )

        control = self.app.info_control_rows["before"].winfo_children()[1]
        exif_label = self.app.exif_frames["before"].winfo_children()[-1]
        self.assertTrue(control.bind("<MouseWheel>"))
        self.assertTrue(self.app.histogram_canvases["before"].canvas.bind("<MouseWheel>"))
        self.assertTrue(exif_label.bind("<MouseWheel>"))
        if tk.TkVersion >= 9.0:
            self.assertTrue(control.bind("<TouchpadScroll>"))

        self.app.info_canvas.yview_moveto(0.0)
        exif_label.event_generate("<MouseWheel>", delta=-120)
        self.root.update_idletasks()
        self.assertGreater(self.app.info_canvas.yview()[0], 0.0)
        self.app.info_canvas.yview_moveto(0.0)
        if tk.TkVersion >= 9.0:
            self.app._on_info_touchpad_scroll(SimpleNamespace(delta=-120))
        else:
            self.app._on_info_mousewheel(SimpleNamespace(delta=-120))
        self.assertGreater(self.app.info_canvas.yview()[0], 0.0)

    def test_image_context_menu_rotates_mirrors_and_restores_without_editing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orientation.png"
            pixels = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
            Image.fromarray(pixels, mode="RGB").save(path)
            self.app.open_images([path])
            self.wait_until(lambda: self.app.images["before"] is not None)
            original = self.app.images["before"]
            assert original is not None

            try:
                aqua = self.root.tk.call("tk", "windowingsystem") == "aqua"
            except tk.TclError:
                aqua = False
            context_button = 2 if aqua else 3
            self.assertTrue(self.app.before_view.canvas.bind(f"<ButtonRelease-{context_button}>"))
            context_callback = mock.Mock()
            original_callback = self.app.before_view.context_callback
            self.app.before_view.context_callback = context_callback
            self.app.before_view._on_context_press(
                SimpleNamespace(x=10, y=10, x_root=20, y_root=20)
            )
            self.app.before_view._on_context_drag(
                SimpleNamespace(x=30, y=24, x_root=40, y_root=34)
            )
            self.app.before_view._on_context_release(
                SimpleNamespace(x=30, y=24, x_root=40, y_root=34)
            )
            context_callback.assert_not_called()
            self.app.before_view._on_context_press(
                SimpleNamespace(x=10, y=10, x_root=20, y_root=20)
            )
            self.app.before_view._on_context_release(
                SimpleNamespace(x=10, y=10, x_root=20, y_root=20)
            )
            context_callback.assert_called_once_with("before", 20, 20)
            self.app.before_view.context_callback = original_callback
            with mock.patch.object(tk.Menu, "tk_popup"):
                self.app._show_image_context_menu("before", 20, 20)
            labels = [
                self.app._image_context_menu.entrycget(index, "label")
                for index in range(self.app._image_context_menu.index("end") + 1)
                if self.app._image_context_menu.type(index) != "separator"
            ]
            self.assertEqual(
                labels,
                ["向左旋转 90°", "向右旋转 90°", "水平镜像", "垂直镜像", "还原图片方向"],
            )

            self.app._apply_image_orientation("before", "rotate_right")
            rotated = self.app.images["before"]
            assert rotated is not None
            self.assertEqual((rotated.width, rotated.height), (2, 3))
            self.assertTrue(np.shares_memory(rotated.rgb, original.rgb))
            self.app._apply_image_orientation("before", "flip_horizontal")
            self.assertEqual(self.app.image_transform_ops["before"], ["rotate_right", "flip_horizontal"])
            self.app._reset_image_orientation("before")
            restored = self.app.images["before"]
            assert restored is not None
            np.testing.assert_array_equal(restored.rgb, original.rgb)
            self.assertEqual(self.app.image_transform_ops["before"], [])
            with Image.open(path) as saved:
                self.assertEqual(saved.size, (3, 2))

    def test_folder_thumbnails_scroll_smoothly_with_wheel_and_touchpad_binding(self) -> None:
        strip = self.app.folder_thumbnail_strip
        strip.request_callback = lambda _items: None
        strip.set_paths([Path(f"frame-{index:03d}.png") for index in range(90)])
        self.app._folder_browser_available = True
        self.app._set_folder_sidebar_visible(True)
        self.root.deiconify()
        self.wait_until(lambda: strip.canvas.winfo_height() > 100)
        self.assertTrue(strip.canvas.bind("<MouseWheel>"))
        self.assertTrue(strip.cards[0].bind("<MouseWheel>"))
        if tk.TkVersion >= 9.0:
            self.assertTrue(strip.canvas.bind("<TouchpadScroll>"))
            self.assertTrue(strip.cards[0].bind("<TouchpadScroll>"))
        strip.canvas.yview_moveto(0.0)
        strip._on_mousewheel(SimpleNamespace(delta=-120))
        self.assertGreater(strip.canvas.yview()[0], 0.0)
        thumbnail = Image.new("RGB", strip.PREVIEW_SIZE, (30, 60, 90))
        for index, path in enumerate(strip.paths):
            strip.apply_thumbnail(index, ThumbnailData(path, thumbnail, (640, 480)))
        self.assertEqual(len(strip._photos), strip.MAX_CACHED_THUMBNAILS)

    def test_idle_hidden_and_shutdown_paths_release_render_and_image_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.png"
            Image.new("RGB", (640, 480), (80, 100, 120)).save(path)
            self.app.open_images([path])
            self.wait_until(
                lambda: self.app.images["before"] is not None
                and self.app._pending == 0
                and self.app._poll_after_id is None
            )
            self.root.deiconify()
            self.app.before_view._render()
            self.assertIsNotNone(self.app.before_view._pil_image)
            image_data = self.app.images["before"]
            assert image_data is not None
            reference = weakref.ref(image_data)

            self.app.hide()
            self.assertIsNone(self.app.before_view._pil_image)
            self.assertIsNone(self.app.before_view._photo)
            self.app.show()
            self.assertIsNotNone(self.app.before_view._pil_image)

            self.app.shutdown()
            del image_data
            gc.collect()
            self.assertIsNone(reference())


if __name__ == "__main__":
    unittest.main()
