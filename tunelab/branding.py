from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk

from .ui_foundation import (
    FONT_BODY,
    FONT_BODY_BOLD,
    FONT_HERO,
    FONT_SMALL,
    INK,
    MUTED,
    PANEL_BG,
    SUBTLE_SEPARATOR,
    TERTIARY,
)


APP_NAME = "TuneLab"
APP_VERSION = "0.2.0"
APP_TAGLINE = "Qualcomm Camera Tuning Workbench"
AUTHOR_EMAIL = "kaiyi.jiang@thundersoft.com"
WORKBENCH_HELP_TEXT = (
    "TuneLab 是一个持续扩展的本地 Camera Tuning 工作台。当前可用模块以首页和“工具”菜单为准；"
    "后续新增模块也会从同一入口提供。\n\n"
    "现有工具覆盖统一 CCM / ColorChecker 校正、Gamma 优化、普通图片像素与 ROI 检查等任务。"
    "每个模块拥有独立的输入要求、算法边界和结果解释，请进入对应模块后查看其专属帮助。\n\n"
    "所有计算均在本地完成，不调用云端服务。分析结果用于工程调试与方向判断；"
    "涉及设备画质或参数修改时，仍应通过上机、重新拍摄和目标场景验证。"
)


def show_workbench_help(root: tk.Misc) -> None:
    """Show module-neutral help that remains valid as TuneLab grows."""

    messagebox.showinfo("TuneLab 使用说明", WORKBENCH_HELP_TEXT, parent=root)


def application_icon_path() -> Path:
    bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return bundled_root / "tunelab" / "assets" / "tunelab.png"


def _about_icon(master: tk.Misc, source: Path, size: int = 104):
    """Return a sharp, content-cropped PhotoImage for the custom About panel."""

    from PIL import Image, ImageTk

    with Image.open(source) as image:
        image = image.convert("RGBA")
        bounds = image.getchannel("A").getbbox() or (0, 0, image.width, image.height)
        content_side = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
        crop_side = min(min(image.size), content_side + round(content_side * 0.18))
        centre_x = (bounds[0] + bounds[2]) / 2
        centre_y = (bounds[1] + bounds[3]) / 2
        left = max(0, min(image.width - crop_side, round(centre_x - crop_side / 2)))
        top = max(0, min(image.height - crop_side, round(centre_y - crop_side / 2)))
        image = image.crop((left, top, left + crop_side, top + crop_side))
        image = image.resize((size, size), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image, master=master)


def show_about_dialog(root: tk.Misc, icon_path: Path | None = None) -> tk.Toplevel:
    """Show one reusable TuneLab-owned About dialog on every tool page."""

    existing = getattr(root, "_tunelab_about_dialog", None)
    if existing is not None:
        try:
            if int(root.tk.call("winfo", "exists", str(existing))):
                existing.deiconify()
                existing.lift()
                existing.after_idle(existing.focus_set)
                return existing
        except tk.TclError:
            pass

    surface = PANEL_BG
    dialog = tk.Toplevel(root)
    setattr(root, "_tunelab_about_dialog", dialog)
    dialog.withdraw()
    dialog.title("关于 TuneLab")
    dialog.configure(background=surface)
    dialog.resizable(False, False)
    dialog.transient(root.winfo_toplevel())
    try:
        dialog.tk.call("::tk::unsupported::MacWindowStyle", "style", dialog._w, "moveableModal", "none")
    except tk.TclError:
        pass

    available_fonts = set(tkfont.names(root))
    title_font = FONT_HERO if FONT_HERO in available_fonts else "TkHeadingFont"
    body_font = FONT_BODY if FONT_BODY in available_fonts else "TkDefaultFont"
    body_bold_font = FONT_BODY_BOLD if FONT_BODY_BOLD in available_fonts else "TkHeadingFont"
    small_font = FONT_SMALL if FONT_SMALL in available_fonts else "TkDefaultFont"

    body = tk.Frame(dialog, background=surface, padx=42, pady=34)
    body.pack(fill="both", expand=True)
    try:
        about_image = _about_icon(dialog, icon_path or application_icon_path(), size=112)
        setattr(dialog, "_tunelab_icon", about_image)
        tk.Label(body, image=about_image, background=surface, borderwidth=0).pack(pady=(0, 16))
    except (ImportError, OSError, ValueError, tk.TclError):
        pass

    tk.Label(body, text=APP_NAME, background=surface, foreground=INK, font=title_font).pack()
    tk.Label(body, text=APP_TAGLINE, background=surface, foreground=MUTED, font=body_font).pack(pady=(6, 0))

    details = tk.Frame(body, background=surface, highlightthickness=1, highlightbackground=SUBTLE_SEPARATOR, padx=16, pady=12)
    details.pack(fill="x", pady=(22, 0))
    for index, (label, value) in enumerate((("版本", APP_VERSION), ("联系", AUTHOR_EMAIL))):
        tk.Label(details, text=label, background=surface, foreground=TERTIARY, font=small_font).grid(row=index, column=0, sticky="w", pady=3)
        tk.Label(details, text=value, background=surface, foreground=INK, font=body_bold_font).grid(row=index, column=1, sticky="e", padx=(24, 0), pady=3)
    details.columnconfigure(1, weight=1)

    tk.Label(
        body,
        text="所有计算均在本地完成",
        background=surface,
        foreground=MUTED,
        font=small_font,
    ).pack(pady=(18, 0))
    ttk.Button(body, text="完成", command=dialog.destroy, style="Primary.TButton").pack(fill="x", pady=(18, 0))

    def forget_dialog(event: tk.Event) -> None:
        if event.widget is dialog and getattr(root, "_tunelab_about_dialog", None) is dialog:
            setattr(root, "_tunelab_about_dialog", None)

    dialog.bind("<Destroy>", forget_dialog, add="+")
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.update_idletasks()
    width = max(460, dialog.winfo_reqwidth())
    height = dialog.winfo_reqheight()
    owner = root.winfo_toplevel()
    x_pos = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
    y_pos = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
    dialog.geometry(f"{width}x{height}+{x_pos}+{y_pos}")
    dialog.deiconify()
    dialog.lift()
    dialog.after_idle(dialog.focus_set)
    return dialog
