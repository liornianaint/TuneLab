from __future__ import annotations

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
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller 未安装。请先运行: python -m pip install pyinstaller", file=sys.stderr)
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
    return result


if __name__ == "__main__":
    raise SystemExit(main())
