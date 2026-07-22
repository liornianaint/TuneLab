from __future__ import annotations

import importlib
import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tunelab import __version__  # noqa: E402


APP_VERSION = __version__
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
    destination = ROOT / "build" / "tunelab-app-icon.png"
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
        image.crop((left, top, left + crop_side, top + crop_side)).resize(
            (1024, 1024), Image.Resampling.LANCZOS
        ).save(destination)
    return destination


def main() -> int:
    expected_environment = ROOT / ".venv"
    if expected_environment.is_dir() and Path(sys.prefix).resolve() != expected_environment.resolve():
        print(
            f"请使用工程虚拟环境构建，避免生成缺少运行库的 APP：\n"
            f"  {expected_environment / 'bin' / 'python'} scripts/build.py",
            file=sys.stderr,
        )
        return 2
    missing = missing_build_modules()
    if missing:
        print(
            "构建环境缺少运行模块：" + ", ".join(missing) + "\n"
            "请先运行：.venv/bin/python -m pip install -e . pyinstaller",
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
        "--windowed",
        "--name",
        "TuneLab",
        "--icon",
        str(icon),
        "--add-data",
        f"{ROOT / 'tunelab' / 'assets' / 'tunelab.png'}:tunelab/assets",
        "--hidden-import",
        "numpy",
        "--hidden-import",
        "cv2",
        "--hidden-import",
        "pillow_heif",
        "--paths",
        str(ROOT),
    ]
    if platform.system() == "Darwin":
        command.extend(["--osx-bundle-identifier", "com.tunelab.app"])
    command.append(str(ROOT / "run_tunelab.py"))
    print("Building:", " ".join(command))
    result = subprocess.call(command, cwd=ROOT)
    if result == 0 and platform.system() == "Darwin":
        bundle = ROOT / "dist" / "TuneLab.app"
        plist_path = bundle / "Contents" / "Info.plist"
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        plist.update(
            {
                "CFBundleDisplayName": "TuneLab",
                "CFBundleName": "TuneLab",
                "CFBundleShortVersionString": APP_VERSION,
                "CFBundleVersion": APP_VERSION,
            }
        )
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle)
        result = subprocess.call(["codesign", "--force", "--deep", "--sign", "-", str(bundle)])
        if result == 0:
            environment = os.environ.copy()
            environment["TUNELAB_SMOKE_TEST"] = "1"
            executable = bundle / "Contents" / "MacOS" / "TuneLab"
            try:
                smoke = subprocess.run(
                    [str(executable)],
                    cwd=ROOT,
                    env=environment,
                    timeout=45,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                print("构建产物启动自检超时。", file=sys.stderr)
                return 3
            if smoke.returncode != 0:
                print(f"构建产物启动自检失败，退出码 {smoke.returncode}。", file=sys.stderr)
                return smoke.returncode or 3
            print(f"启动自检通过：{bundle}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
