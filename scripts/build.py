from __future__ import annotations

import hashlib
import importlib
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tunelab import __version__  # noqa: E402
from tunelab.update_config import (  # noqa: E402
    SPARKLE_APPCAST_URL,
    SPARKLE_ARCHIVE_NAME,
    SPARKLE_ARCHIVE_SHA256,
    SPARKLE_ARCHIVE_SIZE,
    SPARKLE_DOWNLOAD_URL,
    SPARKLE_PUBLIC_ED_KEY,
    SPARKLE_VERSION,
)


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_sparkle_archive(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and file_sha256(destination) == SPARKLE_ARCHIVE_SHA256:
        return
    temporary = destination.with_name(destination.name + ".download")
    try:
        if temporary.exists():
            temporary.unlink()
        request = Request(
            SPARKLE_DOWNLOAD_URL,
            headers={"User-Agent": f"TuneLab-builder/{APP_VERSION}"},
        )
        downloaded = 0
        with urlopen(request, timeout=180) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > SPARKLE_ARCHIVE_SIZE:
                    raise RuntimeError("Sparkle 发布包大小超过固定值。")
                output.write(chunk)
        if downloaded != SPARKLE_ARCHIVE_SIZE:
            raise RuntimeError(
                f"Sparkle 发布包大小异常：{downloaded} 字节。"
            )
        actual_digest = file_sha256(temporary)
        if actual_digest != SPARKLE_ARCHIVE_SHA256:
            raise RuntimeError(
                "Sparkle 发布包校验失败："
                f"期望 {SPARKLE_ARCHIVE_SHA256}，实际 {actual_digest}"
            )
        temporary.replace(destination)
    except (HTTPError, URLError, TimeoutError, OSError, RuntimeError):
        if temporary.exists():
            temporary.unlink()
        raise


def ensure_sparkle_framework() -> Path:
    vendor_directory = ROOT / "build" / "sparkle" / SPARKLE_VERSION
    framework = vendor_directory / "Sparkle.framework"
    framework_binary = framework / "Versions" / "B" / "Sparkle"
    if framework_binary.is_file():
        return framework
    archive_path = ROOT / "build" / "sparkle" / SPARKLE_ARCHIVE_NAME
    download_sparkle_archive(archive_path)
    vendor_directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:xz") as archive:
        members = archive.getmembers()
        destination_root = vendor_directory.resolve()
        for member in members:
            target = (vendor_directory / member.name).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise RuntimeError("Sparkle 发布包包含不安全的文件路径。") from exc
        archive.extractall(vendor_directory)
    if not framework_binary.is_file():
        raise RuntimeError("Sparkle 发布包中缺少 Sparkle.framework。")
    return framework


def embed_sparkle_framework(bundle: Path, source_framework: Path) -> Path:
    destination = bundle / "Contents" / "Frameworks" / "Sparkle.framework"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source_framework, destination, symlinks=True)
    license_source = source_framework.parent / "LICENSE"
    if license_source.is_file():
        shutil.copy2(
            license_source,
            bundle / "Contents" / "Resources" / "Sparkle-LICENSE.txt",
        )
    return destination


def macos_minimum_version(paths: list[Path]) -> str:
    versions: list[tuple[int, ...]] = []
    for path in paths:
        if not path.exists():
            continue
        inspected = subprocess.run(
            ["otool", "-l", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            continue
        for value in re.findall(
            r"^\s*minos\s+(\d+(?:\.\d+){1,2})\s*$",
            inspected.stdout,
            flags=re.MULTILINE,
        ):
            versions.append(tuple(int(part) for part in value.split(".")))
    if not versions:
        raise RuntimeError("无法判断构建产物支持的最低 macOS 版本。")
    latest = max(versions)
    return ".".join(str(part) for part in latest[:2])


def sign_macos_bundle(bundle: Path) -> int:
    identity = os.environ.get("TUNELAB_CODESIGN_IDENTITY", "-").strip() or "-"
    command = ["codesign", "--force", "--deep"]
    if identity != "-":
        command.extend(["--options", "runtime", "--timestamp"])
    command.extend(["--sign", identity, str(bundle)])
    result = subprocess.call(command, cwd=ROOT)
    if result != 0:
        return result
    return subprocess.call(
        ["codesign", "--deep", "--strict", "--verify", str(bundle)],
        cwd=ROOT,
    )


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
    sparkle_framework = None
    if platform.system() == "Darwin":
        try:
            sparkle_framework = ensure_sparkle_framework()
        except (OSError, RuntimeError, tarfile.TarError) as exc:
            print(f"准备 Sparkle 自动更新框架失败：{exc}", file=sys.stderr)
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
        if sparkle_framework is None:
            print("Sparkle 自动更新框架未准备完成。", file=sys.stderr)
            return 2
        embed_sparkle_framework(bundle, sparkle_framework)
        plist_path = bundle / "Contents" / "Info.plist"
        minimum_macos = macos_minimum_version(
            [
                bundle / "Contents" / "MacOS" / "TuneLab",
                (
                    bundle
                    / "Contents"
                    / "Frameworks"
                    / "Python.framework"
                    / "Versions"
                    / "Current"
                    / "Python"
                ),
                (
                    bundle
                    / "Contents"
                    / "Frameworks"
                    / "Sparkle.framework"
                    / "Versions"
                    / "Current"
                    / "Sparkle"
                ),
            ]
        )
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        plist.update(
            {
                "CFBundleDisplayName": "TuneLab",
                "CFBundleName": "TuneLab",
                "CFBundleShortVersionString": APP_VERSION,
                "CFBundleVersion": APP_VERSION,
                "LSMinimumSystemVersion": minimum_macos,
                "SUFeedURL": SPARKLE_APPCAST_URL,
                "SUPublicEDKey": SPARKLE_PUBLIC_ED_KEY,
                "SUEnableAutomaticChecks": True,
                "SUAllowsAutomaticUpdates": True,
                "SUAutomaticallyUpdate": True,
                "SUScheduledCheckInterval": 86400,
                "SUVerifyUpdateBeforeExtraction": True,
            }
        )
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle)
        result = sign_macos_bundle(bundle)
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
