"""GitHub Release update checks and their small native Tk controller."""

from __future__ import annotations

import json
import queue
import re
import socket
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from http.client import HTTPException
from tkinter import messagebox
from typing import Any, Callable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import __version__


GITHUB_REPOSITORY = "liornianaint/TuneLab"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
MAX_RELEASE_RESPONSE_BYTES = 1_000_000


class UpdateCheckError(RuntimeError):
    """The update channel could not return a trustworthy result."""


class NoPublishedRelease(UpdateCheckError):
    """The repository has not published a stable GitHub Release yet."""


@dataclass(frozen=True, order=True)
class ParsedVersion:
    release: tuple[int, ...]
    stability: int
    prerelease_number: int


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag_name: str
    name: str
    page_url: str
    notes: str = ""
    published_at: str = ""


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_release: ReleaseInfo
    update_available: bool


_VERSION_PATTERN = re.compile(r"^\s*[vV]?(\d+(?:\.\d+)*)(.*?)\s*$")
_PRERELEASE_PATTERN = re.compile(
    r"^[-_.]?(dev|alpha|a|beta|b|rc|preview|pre)[-_.]?(\d*)$",
    re.IGNORECASE,
)
_PRERELEASE_RANK = {
    "dev": 0,
    "alpha": 1,
    "a": 1,
    "beta": 2,
    "b": 2,
    "rc": 3,
    "preview": 3,
    "pre": 3,
}


def parse_version(value: str) -> ParsedVersion:
    """Parse the stable/prerelease forms used by TuneLab Release tags."""

    match = _VERSION_PATTERN.fullmatch(str(value))
    if match is None:
        raise ValueError(f"无法识别版本号：{value}")
    release = [int(part) for part in match.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    suffix = match.group(2)
    if "+" in suffix:
        suffix, _build = suffix.split("+", 1)
    if not suffix:
        return ParsedVersion(tuple(release), 4, 0)
    prerelease = _PRERELEASE_PATTERN.fullmatch(suffix)
    if prerelease is None:
        raise ValueError(f"无法识别版本号：{value}")
    label = prerelease.group(1).casefold()
    number = int(prerelease.group(2) or 0)
    return ParsedVersion(tuple(release), _PRERELEASE_RANK[label], number)


def is_newer_version(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def _safe_release_url(value: object) -> str:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    expected_path = f"/{GITHUB_REPOSITORY}/releases".casefold()
    if (
        parsed.scheme == "https"
        and parsed.netloc.casefold() == "github.com"
        and (
            parsed.path.casefold() == expected_path
            or parsed.path.casefold().startswith(expected_path + "/")
        )
    ):
        return candidate
    return RELEASES_PAGE_URL


def _release_from_payload(payload: Mapping[str, Any]) -> ReleaseInfo:
    tag_name = str(payload.get("tag_name") or "").strip()
    if not tag_name:
        raise UpdateCheckError("更新服务器返回的数据缺少版本标签。")
    try:
        parse_version(tag_name)
    except ValueError as exc:
        raise UpdateCheckError(f"Release 标签不是有效版本号：{tag_name}") from exc
    version = tag_name[1:] if tag_name[:1].casefold() == "v" else tag_name
    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        name=str(payload.get("name") or tag_name).strip(),
        page_url=_safe_release_url(payload.get("html_url")),
        notes=str(payload.get("body") or ""),
        published_at=str(payload.get("published_at") or ""),
    )


def fetch_latest_release(
    *,
    timeout: float = 5.0,
    opener: Optional[Callable[..., Any]] = None,
) -> ReleaseInfo:
    """Fetch the latest stable Release without credentials or third-party deps."""

    request = Request(
        LATEST_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"TuneLab/{__version__}",
        },
    )
    open_url = opener or urlopen
    try:
        response = open_url(request, timeout=max(1.0, float(timeout)))
        try:
            raw = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except HTTPError as exc:
        try:
            exc.close()
        except OSError:
            pass
        if exc.code == 404:
            raise NoPublishedRelease("更新通道尚未发布正式 Release。") from exc
        raise UpdateCheckError(f"更新服务器返回 HTTP {exc.code}。") from exc
    except (URLError, TimeoutError, socket.timeout, OSError, HTTPException) as exc:
        raise UpdateCheckError("暂时无法连接更新服务器，请检查网络后重试。") from exc
    if len(raw) > MAX_RELEASE_RESPONSE_BYTES:
        raise UpdateCheckError("更新服务器返回的数据异常过大。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateCheckError("更新服务器返回的数据无法解析。") from exc
    if not isinstance(payload, Mapping):
        raise UpdateCheckError("更新服务器返回的数据格式无效。")
    return _release_from_payload(payload)


def check_for_updates(current_version: str = __version__) -> UpdateCheckResult:
    release = fetch_latest_release()
    try:
        available = is_newer_version(release.version, current_version)
    except ValueError as exc:
        raise UpdateCheckError("本地或远端版本号无法比较。") from exc
    return UpdateCheckResult(
        current_version=current_version,
        latest_release=release,
        update_available=available,
    )


class UpdateController:
    """Run network checks off the Tk thread and present native dialogs."""

    def __init__(
        self,
        root: tk.Misc,
        *,
        checker: Callable[[], UpdateCheckResult] = check_for_updates,
    ) -> None:
        self.root = root
        self.checker = checker
        self._results: "queue.Queue[tuple[int, object]]" = queue.Queue()
        self._check_id = 0
        self._checking = False
        self._manual_requested = False
        self._poll_after_id: Optional[str] = None
        self._startup_after_id: Optional[str] = None
        self._startup_scheduled = False
        self._startup_satisfied = False
        self._closed = False

    def schedule_startup_check(self, delay_ms: int = 900) -> None:
        if self._closed or self._startup_scheduled:
            return
        self._startup_scheduled = True
        self._startup_after_id = self.root.after(
            max(0, int(delay_ms)), self._run_scheduled_startup_check
        )

    def _run_scheduled_startup_check(self) -> None:
        self._startup_after_id = None
        if self._closed or self._startup_satisfied:
            return
        try:
            # TuneLab's real launch window is visible; withdrawn roots are used
            # by UI tests and should never generate external network traffic.
            if str(self.root.winfo_toplevel().state()) == "withdrawn":
                return
        except tk.TclError:
            return
        self.check(manual=False)

    def check(self, *, manual: bool) -> None:
        if self._closed:
            return
        if manual:
            self._manual_requested = True
            self._startup_satisfied = True
        if self._checking:
            return
        self._checking = True
        self._check_id += 1
        check_id = self._check_id
        thread = threading.Thread(
            target=self._run_check,
            args=(check_id,),
            name="TuneLabUpdateCheck",
            daemon=True,
        )
        thread.start()
        self._poll_after_id = self.root.after(80, self._poll_result)

    def _run_check(self, check_id: int) -> None:
        try:
            outcome: object = self.checker()
        except (NoPublishedRelease, UpdateCheckError) as exc:
            outcome = exc
        except Exception:
            outcome = UpdateCheckError("检查更新时发生未知错误。")
        self._results.put((check_id, outcome))

    def _poll_result(self) -> None:
        self._poll_after_id = None
        if self._closed:
            return
        outcome: Optional[object] = None
        while True:
            try:
                check_id, candidate = self._results.get_nowait()
            except queue.Empty:
                break
            if check_id == self._check_id:
                outcome = candidate
        if outcome is None:
            self._poll_after_id = self.root.after(80, self._poll_result)
            return
        self._checking = False
        manual = self._manual_requested
        self._manual_requested = False
        self._startup_satisfied = True
        self._present(outcome, manual=manual)

    def _present(self, outcome: object, *, manual: bool) -> None:
        if isinstance(outcome, NoPublishedRelease):
            if manual:
                messagebox.showinfo(
                    "检查更新",
                    f"当前版本：{__version__}\n\n更新通道尚未发布正式 Release。",
                    parent=self.root,
                )
            return
        if isinstance(outcome, UpdateCheckError):
            if manual:
                messagebox.showwarning("检查更新", str(outcome), parent=self.root)
            return
        if not isinstance(outcome, UpdateCheckResult):
            if manual:
                messagebox.showwarning("检查更新", "检查更新时发生未知错误。", parent=self.root)
            return
        release = outcome.latest_release
        if not outcome.update_available:
            if manual:
                messagebox.showinfo(
                    "检查更新",
                    f"当前版本：{outcome.current_version}\n"
                    f"最新版本：{release.version}\n\n当前已是最新版本。",
                    parent=self.root,
                )
            return
        open_page = messagebox.askyesno(
            "发现新版本",
            f"当前版本：{outcome.current_version}\n"
            f"最新版本：{release.version}\n\n"
            "是否打开 Release 页面下载并手动安装？",
            parent=self.root,
        )
        if open_page:
            try:
                opened = webbrowser.open_new_tab(release.page_url)
            except (webbrowser.Error, OSError):
                opened = False
            if opened is False:
                messagebox.showerror(
                    "无法打开更新页面",
                    f"请在浏览器中打开：\n{release.page_url}",
                    parent=self.root,
                )

    def shutdown(self) -> None:
        self._closed = True
        for after_id in (self._startup_after_id, self._poll_after_id):
            if after_id is None:
                continue
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._startup_after_id = None
        self._poll_after_id = None


def update_controller_for(root: tk.Misc) -> UpdateController:
    controller = getattr(root, "_tunelab_update_controller", None)
    if isinstance(controller, UpdateController):
        return controller
    controller = UpdateController(root)
    setattr(root, "_tunelab_update_controller", controller)
    return controller
