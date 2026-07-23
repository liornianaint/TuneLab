"""Build, archive, and sign a TuneLab release for Sparkle 2."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build import ensure_sparkle_framework  # noqa: E402
from tunelab import __version__  # noqa: E402
from tunelab.update_config import (  # noqa: E402
    GITHUB_REPOSITORY,
    SPARKLE_KEY_ACCOUNT,
    SPARKLE_PUBLIC_ED_KEY,
)


SPARKLE_XML_NAMESPACE = (
    "http://www.andymatuschak.org/xml-namespaces/sparkle"
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_bundle(bundle: Path) -> str:
    plist_path = bundle / "Contents" / "Info.plist"
    if not plist_path.is_file():
        raise RuntimeError(f"未找到 macOS 应用包：{bundle}")
    with plist_path.open("rb") as handle:
        metadata = plistlib.load(handle)
    version = str(metadata.get("CFBundleVersion") or "").strip()
    if version != __version__:
        raise RuntimeError(
            f"应用包版本 {version or '未知'} 与工程版本 {__version__} 不一致。"
        )
    for required_key in ("SUFeedURL", "SUPublicEDKey"):
        if not metadata.get(required_key):
            raise RuntimeError(f"应用包缺少自动更新配置：{required_key}")
    verification = subprocess.run(
        [
            "codesign",
            "--deep",
            "--strict",
            "--verify",
            str(bundle),
        ],
        cwd=ROOT,
        check=False,
    )
    if verification.returncode != 0:
        raise RuntimeError("macOS 应用包签名验证失败。")
    return version


def create_update_archive(bundle: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    result = subprocess.run(
        [
            "ditto",
            "-c",
            "-k",
            "--sequesterRsrc",
            "--keepParent",
            str(bundle),
            str(destination),
        ],
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0 or not destination.is_file():
        raise RuntimeError("创建 macOS 更新压缩包失败。")


def remove_existing_appcast_version(appcast: Path, version: str) -> None:
    """Regenerate the current item while preserving older update entries."""

    if not appcast.is_file():
        return
    ET.register_namespace("sparkle", SPARKLE_XML_NAMESPACE)
    tree = ET.parse(appcast)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError("现有 appcast.xml 缺少 channel。")
    changed = False
    for item in list(channel.findall("item")):
        version_node = item.find(f"{{{SPARKLE_XML_NAMESPACE}}}version")
        if version_node is not None and (version_node.text or "").strip() == version:
            channel.remove(item)
            changed = True
    if changed:
        if not any(
            element.tag.startswith(f"{{{SPARKLE_XML_NAMESPACE}}}")
            for element in root.iter()
        ):
            root.set("xmlns:sparkle", SPARKLE_XML_NAMESPACE)
        tree.write(appcast, encoding="utf-8", xml_declaration=True)


def generate_appcast(
    archive: Path,
    *,
    version: str,
    release_notes: Optional[Path] = None,
) -> Path:
    sparkle_distribution = ensure_sparkle_framework().parent
    generator = sparkle_distribution / "bin" / "generate_appcast"
    key_reader = sparkle_distribution / "bin" / "generate_keys"
    verifier = sparkle_distribution / "bin" / "sign_update"
    if (
        not generator.is_file()
        or not key_reader.is_file()
        or not verifier.is_file()
    ):
        raise RuntimeError("Sparkle 发布工具不完整。")

    build_directory = ROOT / "build"
    build_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="tunelab-appcast-",
        dir=build_directory,
    ) as temporary_name:
        working_directory = Path(temporary_name)
        working_archive = working_directory / archive.name
        shutil.copy2(archive, working_archive)
        existing_appcast = ROOT / "appcast.xml"
        if existing_appcast.is_file():
            working_appcast = working_directory / "appcast.xml"
            shutil.copy2(existing_appcast, working_appcast)
            remove_existing_appcast_version(working_appcast, version)
        if release_notes is not None:
            if not release_notes.is_file():
                raise RuntimeError(f"未找到更新说明：{release_notes}")
            shutil.copy2(
                release_notes,
                working_archive.with_suffix(release_notes.suffix),
            )

        command = [
            str(generator),
            "--account",
            SPARKLE_KEY_ACCOUNT,
            "--maximum-deltas",
            "0",
            "--maximum-versions",
            "3",
            "--download-url-prefix",
            (
                f"https://github.com/{GITHUB_REPOSITORY}/releases/"
                f"download/v{version}/"
            ),
            "--link",
            f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/v{version}",
            str(working_directory),
        ]
        private_key = os.environ.get("TUNELAB_SPARKLE_PRIVATE_KEY", "").strip()
        standard_input = None
        if private_key:
            command[1:1] = ["--ed-key-file", "-"]
            standard_input = private_key + "\n"
        else:
            key_result = subprocess.run(
                [
                    str(key_reader),
                    "--account",
                    SPARKLE_KEY_ACCOUNT,
                    "-p",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if (
                key_result.returncode != 0
                or key_result.stdout.strip() != SPARKLE_PUBLIC_ED_KEY
            ):
                raise RuntimeError(
                    "钥匙串中的 Sparkle 私钥与应用内公钥不匹配。"
                )
        generated = subprocess.run(
            command,
            cwd=ROOT,
            input=standard_input,
            text=True,
            check=False,
        )
        if generated.returncode != 0:
            raise RuntimeError("生成或签名 Sparkle appcast 失败。")

        working_appcast = working_directory / "appcast.xml"
        if not working_appcast.is_file():
            raise RuntimeError("Sparkle 没有生成 appcast.xml。")
        sign_command = [
            str(verifier),
            "--account",
            SPARKLE_KEY_ACCOUNT,
            "--disable-signing-warning",
        ]
        sign_input = None
        if private_key:
            sign_command.extend(["--ed-key-file", "-"])
            sign_input = private_key + "\n"
        sign_command.append(str(working_appcast))
        signed = subprocess.run(
            sign_command,
            cwd=ROOT,
            input=sign_input,
            text=True,
            check=False,
        )
        if signed.returncode != 0:
            raise RuntimeError("Sparkle appcast 签名失败。")
        verify_command = [
            str(verifier),
            "--account",
            SPARKLE_KEY_ACCOUNT,
            "--verify",
        ]
        verify_input = None
        if private_key:
            verify_command.extend(["--ed-key-file", "-"])
            verify_input = private_key + "\n"
        verify_command.append(str(working_appcast))
        verified = subprocess.run(
            verify_command,
            cwd=ROOT,
            input=verify_input,
            text=True,
            check=False,
        )
        if verified.returncode != 0:
            raise RuntimeError("Sparkle appcast 签名验证失败。")
        shutil.copy2(working_appcast, existing_appcast)
    return ROOT / "appcast.xml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="构建 TuneLab macOS APP，并生成 Sparkle 签名更新包。"
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="复用 dist/TuneLab.app，不重新运行 PyInstaller。",
    )
    parser.add_argument(
        "--release-notes",
        type=Path,
        help="可选的 Markdown、HTML 或纯文本更新说明。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if platform.system() != "Darwin":
        print("macOS Release 只能在 macOS 上生成。", file=sys.stderr)
        return 2
    if not args.skip_build:
        built = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build.py")],
            cwd=ROOT,
            check=False,
        )
        if built.returncode != 0:
            return built.returncode

    bundle = ROOT / "dist" / "TuneLab.app"
    try:
        version = validate_bundle(bundle)
        archive = ROOT / "dist" / f"TuneLab-{version}-macOS-arm64.zip"
        create_update_archive(bundle, archive)
        appcast = generate_appcast(
            archive,
            version=version,
            release_notes=args.release_notes,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"准备 macOS Release 失败：{exc}", file=sys.stderr)
        return 2

    print(f"macOS 更新包：{archive}")
    print(f"SHA-256：{file_sha256(archive)}")
    print(f"Sparkle appcast：{appcast}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
