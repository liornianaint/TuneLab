from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tunelab.image_inspector.renaming import (
    BatchRenameError,
    RenameItem,
    build_rename_plan,
    execute_rename_plan,
)


class ImageInspectorBatchRenameTests(unittest.TestCase):
    def test_preview_preserves_extensions_and_supports_original_name_and_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / "A.JPG", root / "B.png"]
            for path in paths:
                path.write_bytes(path.name.encode())
            plan = build_rename_plan(paths, "{name}_edited_{n}", start=7, digits=3)
        self.assertEqual(
            [item.destination.name for item in plan],
            ["A_edited_007.JPG", "B_edited_008.png"],
        )

    def test_duplicate_and_existing_destinations_are_rejected_before_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            second = root / "second.jpg"
            occupied = root / "Image_01.jpg"
            for path in (first, second, occupied):
                path.write_bytes(path.name.encode())
            with self.assertRaises(BatchRenameError):
                build_rename_plan((first, second), "same")
            with self.assertRaises(BatchRenameError):
                build_rename_plan((first, second), "Image_{n}", digits=2)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_two_phase_commit_handles_name_swaps_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one.jpg"
            second = root / "two.jpg"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            mapping = execute_rename_plan(
                [
                    RenameItem(first, second),
                    RenameItem(second, first),
                ]
            )
            self.assertEqual(mapping[first], second)
            self.assertEqual(mapping[second], first)
            self.assertEqual(first.read_bytes(), b"two")
            self.assertEqual(second.read_bytes(), b"one")
            self.assertEqual(list(root.glob(".tunelab-rename-*")), [])

    def test_sequence_commit_renames_every_file_without_changing_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / f"capture-{index}.jpg" for index in range(3)]
            for index, path in enumerate(paths):
                path.write_bytes(bytes((index,)))
            plan = build_rename_plan(paths, "D65_{n}", start=1, digits=2)
            mapping = execute_rename_plan(plan)
            self.assertEqual([mapping[path.resolve()].name for path in paths], ["D65_01.jpg", "D65_02.jpg", "D65_03.jpg"])
            self.assertEqual([mapping[path.resolve()].read_bytes() for path in paths], [b"\x00", b"\x01", b"\x02"])

    def test_case_only_rename_works_on_finder_style_filesystems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.jpg"
            source.write_bytes(b"pixels")
            source_key = source.resolve()
            plan = build_rename_plan([source], "Photo")
            mapping = execute_rename_plan(plan)
            self.assertEqual(mapping[source_key].name, "Photo.jpg")
            self.assertEqual([path.name for path in root.iterdir()], ["Photo.jpg"])
            self.assertEqual((root / "Photo.jpg").read_bytes(), b"pixels")


if __name__ == "__main__":
    unittest.main()
