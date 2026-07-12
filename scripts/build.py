from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller 未安装。请先运行: python -m pip install pyinstaller", file=sys.stderr)
        return 2
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
        str(ROOT / "source" / "app.png"),
        "--add-data",
        f"{ROOT / 'source' / 'app.png'}:source",
        "--paths",
        str(ROOT),
    ]
    if platform.system() == "Darwin":
        command.extend(["--osx-bundle-identifier", "com.tunelab.app"])
    command.append(str(ROOT / "run_matrixcorrect.py"))
    print("Building:", " ".join(command))
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
