"""Small Objective-C bridge to the Sparkle 2 framework embedded in TuneLab."""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import sys
import threading
from pathlib import Path
from typing import Callable, Optional


class SparkleUnavailable(RuntimeError):
    """The native updater cannot be used by this TuneLab process."""


def bundled_sparkle_binary(executable: Optional[Path] = None) -> Path:
    """Return Sparkle's binary inside a normal PyInstaller macOS bundle."""

    executable_path = Path(executable or sys.executable)
    contents_directory = executable_path.parent.parent
    return (
        contents_directory
        / "Frameworks"
        / "Sparkle.framework"
        / "Versions"
        / "B"
        / "Sparkle"
    )


def can_use_sparkle() -> bool:
    """Only packaged macOS applications have a host bundle Sparkle can replace."""

    return platform.system() == "Darwin" and bool(getattr(sys, "frozen", False))


class SparkleUpdater:
    """Own ``SPUStandardUpdaterController`` without requiring PyObjC."""

    def __init__(
        self,
        framework_binary: Optional[Path] = None,
        *,
        framework_loader: Callable[..., object] = ctypes.CDLL,
        objc_loader: Callable[..., object] = ctypes.CDLL,
    ) -> None:
        if platform.system() != "Darwin":
            raise SparkleUnavailable("Sparkle 仅可在 macOS 上运行。")
        if threading.current_thread() is not threading.main_thread():
            raise SparkleUnavailable("Sparkle 必须从应用主线程启动。")

        self.framework_binary = Path(
            framework_binary or bundled_sparkle_binary()
        )
        if not self.framework_binary.is_file():
            raise SparkleUnavailable("应用包中缺少 Sparkle 更新框架。")

        try:
            self._framework = framework_loader(
                str(self.framework_binary),
                mode=getattr(ctypes, "RTLD_GLOBAL", 0),
            )
            objc_path = ctypes.util.find_library("objc")
            if not objc_path:
                raise OSError("libobjc not found")
            self._objc = objc_loader(objc_path)
        except (AttributeError, OSError) as exc:
            raise SparkleUnavailable("无法载入 Sparkle 更新框架。") from exc

        try:
            self._objc.objc_getClass.restype = ctypes.c_void_p
            self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
            self._objc.sel_registerName.restype = ctypes.c_void_p
            self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
            controller_class = self._objc.objc_getClass(
                b"SPUStandardUpdaterController"
            )
            if not controller_class:
                raise SparkleUnavailable("Sparkle 更新控制器不可用。")
            allocated = self._message(controller_class, "alloc")
            controller = self._message(
                allocated,
                "initWithUpdaterDelegate:userDriverDelegate:",
                argument_types=(ctypes.c_void_p, ctypes.c_void_p),
                arguments=(None, None),
            )
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            raise SparkleUnavailable("无法初始化 Sparkle 更新控制器。") from exc
        if not controller:
            raise SparkleUnavailable("Sparkle 更新控制器启动失败。")

        # The Objective-C initializer returns an owned object. Keeping its
        # pointer alive also keeps Sparkle's scheduler and user driver alive.
        self._controller = controller

    def _selector(self, value: str) -> int:
        return self._objc.sel_registerName(value.encode("ascii"))

    def _message(
        self,
        receiver: int,
        method: str,
        *,
        result_type: object = ctypes.c_void_p,
        argument_types: tuple[object, ...] = (),
        arguments: tuple[object, ...] = (),
    ) -> object:
        function = ctypes.CFUNCTYPE(
            result_type,
            ctypes.c_void_p,
            ctypes.c_void_p,
            *argument_types,
        )(("objc_msgSend", self._objc))
        return function(receiver, self._selector(method), *arguments)

    def check_for_updates(self) -> None:
        """Show Sparkle's standard check/download/install interface."""

        if not self._controller:
            raise SparkleUnavailable("Sparkle 更新控制器已经关闭。")
        if threading.current_thread() is not threading.main_thread():
            raise SparkleUnavailable("检查更新必须从应用主线程发起。")
        self._message(
            self._controller,
            "checkForUpdates:",
            result_type=None,
            argument_types=(ctypes.c_void_p,),
            arguments=(None,),
        )

    def shutdown(self) -> None:
        """Release the native controller while Tk still owns the main thread."""

        controller = getattr(self, "_controller", None)
        if not controller:
            return
        self._controller = None
        try:
            self._message(controller, "release", result_type=None)
        except (AttributeError, OSError, TypeError, ValueError):
            pass


def create_sparkle_updater() -> Optional[SparkleUpdater]:
    """Create the packaged macOS updater, or choose the portable fallback."""

    if not can_use_sparkle():
        return None
    return SparkleUpdater()
