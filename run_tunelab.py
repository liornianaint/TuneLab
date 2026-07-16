"""Development launcher that owns and reuses TuneLab's project virtualenv."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
DEPENDENCY_MARKER = VENV_DIR / ".tunelab-dependencies"


def project_python(venv_dir: Path = VENV_DIR) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def dependency_fingerprint(root: Path = ROOT) -> str:
    metadata = (root / "pyproject.toml").read_bytes()
    interpreter = f"{sys.version_info.major}.{sys.version_info.minor}".encode("ascii")
    return hashlib.sha256(metadata + b"\0" + interpreter).hexdigest()


def ensure_project_environment() -> None:
    """Create/synchronise .venv once, then re-exec this launcher inside it."""

    if getattr(sys, "frozen", False):
        return
    python = project_python()
    if not python.exists():
        print(f"正在创建 TuneLab 工程虚拟环境：{VENV_DIR}")
        venv.EnvBuilder(with_pip=True).create(VENV_DIR)
    if Path(sys.prefix).resolve() != VENV_DIR.resolve():
        os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])

    fingerprint = dependency_fingerprint()
    try:
        installed = DEPENDENCY_MARKER.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        installed = ""
    if installed == fingerprint:
        return
    print("正在同步 TuneLab 工程依赖…")
    subprocess.check_call([str(python), "-m", "pip", "install", "-e", str(ROOT)])
    DEPENDENCY_MARKER.write_text(fingerprint + "\n", encoding="ascii")


def main() -> int:
    try:
        ensure_project_environment()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"TuneLab 工程环境初始化失败：{exc}", file=sys.stderr)
        return 2
    from tunelab.app import main as app_main

    app_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
