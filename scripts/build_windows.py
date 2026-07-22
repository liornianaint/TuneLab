from __future__ import annotations

import importlib
import os
import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = ("numpy", "PIL", "cv2", "reportlab", "pillow_heif")


def missing_build_modules() -> list[str]:
    missing: list[str] = []
    for module in ("PyInstaller", *RUNTIME_MODULES):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    return missing


def prepare_icon() -> Path:
    from PIL import Image

    source = ROOT / "tunelab" / "assets" / "tunelab.png"
    destination = ROOT / "build" / "tunelab-app-icon.ico"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image = image.convert("RGBA")
        bounds = image.getchannel("A").getbbox() or (0, 0, image.width, image.height)
        content_side = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
        crop_side = min(min(image.size), content_side + round(content_side * 0.22))
        centre_x = (bounds[0] + bounds[2]) / 2
        centre_y = (bounds[1] + bounds[3]) / 2
        left = max(0, min(image.width - crop_side, round(centre_x - crop_side / 2)))
        top = max(0, min(image.height - crop_side, round(centre_y - crop_side / 2)))
        icon = image.crop((left, top, left + crop_side, top + crop_side)).resize(
            (256, 256), Image.Resampling.LANCZOS
        )
        icon.save(destination, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return destination


def main() -> int:
    if platform.system() != "Windows":
        print("Windows EXE 必须在 Windows 上构建；PyInstaller 不支持跨系统生成 EXE。", file=sys.stderr)
        return 2
    expected_environment = ROOT / ".venv"
    if expected_environment.is_dir() and Path(sys.prefix).resolve() != expected_environment.resolve():
        print("请使用 .venv\\Scripts\\python scripts\\build_windows.py 构建。", file=sys.stderr)
        return 2
    missing = missing_build_modules()
    if missing:
        print(
            "构建环境缺少运行模块：" + ", ".join(missing)
            + "\n请先运行: .venv\\Scripts\\python -m pip install -e . pyinstaller",
            file=sys.stderr,
        )
        return 2
    icon = prepare_icon()
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "TuneLab",
        "--icon",
        str(icon),
        "--add-data",
        f"{ROOT / 'tunelab' / 'assets' / 'tunelab.png'}{os.pathsep}tunelab/assets",
        "--hidden-import",
        "numpy",
        "--hidden-import",
        "cv2",
        "--hidden-import",
        "pillow_heif",
        "--paths",
        str(ROOT),
        str(ROOT / "run_tunelab.py"),
    ]
    print("Building:", " ".join(command))
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
