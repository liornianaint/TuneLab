from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox, ttk


APP_NAME = "TuneLab"
APP_VERSION = "0.2.0"
APP_TAGLINE = "Qualcomm Camera Tuning Workbench"
AUTHOR_EMAIL = "kaiyi.jiang@thundersoft.com"
WORKBENCH_HELP_TEXT = (
    "TuneLab 是一个持续扩展的本地 Camera Tuning 工作台。当前可用模块以首页和“工具”菜单为准；"
    "后续新增模块也会从同一入口提供。\n\n"
    "现有工具覆盖 CC 校正、Gamma 优化、普通图片像素与 ROI 检查等任务。"
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

    surface = "#F8FAFC"
    ink = "#172033"
    muted = "#667085"
    dialog = tk.Toplevel(root)
    setattr(root, "_tunelab_about_dialog", dialog)
    dialog.withdraw()
    dialog.title("关于 TuneLab")
    dialog.configure(background=surface)
    dialog.resizable(False, False)
    dialog.transient(root.winfo_toplevel())

    available_fonts = set(tkfont.names(root))
    title_font = "TuneLabTitleFont" if "TuneLabTitleFont" in available_fonts else "TkHeadingFont"
    body_font = "TuneLabBodyFont" if "TuneLabBodyFont" in available_fonts else "TkDefaultFont"
    small_font = "TuneLabSmallFont" if "TuneLabSmallFont" in available_fonts else "TkDefaultFont"

    body = tk.Frame(dialog, background=surface, padx=38, pady=28)
    body.pack(fill="both", expand=True)
    try:
        about_image = _about_icon(dialog, icon_path or application_icon_path())
        setattr(dialog, "_tunelab_icon", about_image)
        tk.Label(body, image=about_image, background=surface, borderwidth=0).pack(pady=(0, 12))
    except (ImportError, OSError, ValueError, tk.TclError):
        pass

    tk.Label(body, text=APP_NAME, background=surface, foreground=ink, font=title_font).pack()
    tk.Label(body, text=APP_TAGLINE, background=surface, foreground=muted, font=body_font).pack(pady=(4, 0))
    tk.Label(body, text=f"版本 {APP_VERSION}", background=surface, foreground=ink, font=body_font).pack(pady=(12, 0))
    tk.Label(
        body,
        text=f"作者联系邮箱：{AUTHOR_EMAIL}",
        background=surface,
        foreground=ink,
        font=body_font,
    ).pack(pady=(6, 0))
    ttk.Separator(body, orient="horizontal").pack(fill="x", pady=(18, 14))
    tk.Label(body, text="本地运行 · 模块化工作区 · 持续扩展", background=surface, foreground=muted, font=small_font).pack()
    ttk.Button(body, text="关闭", command=dialog.destroy).pack(pady=(18, 0), ipadx=14)

    def forget_dialog(event: tk.Event) -> None:
        if event.widget is dialog and getattr(root, "_tunelab_about_dialog", None) is dialog:
            setattr(root, "_tunelab_about_dialog", None)

    dialog.bind("<Destroy>", forget_dialog, add="+")
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.update_idletasks()
    width = max(420, dialog.winfo_reqwidth())
    height = dialog.winfo_reqheight()
    owner = root.winfo_toplevel()
    x_pos = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
    y_pos = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
    dialog.geometry(f"{width}x{height}+{x_pos}+{y_pos}")
    dialog.deiconify()
    dialog.lift()
    dialog.after_idle(dialog.focus_set)
    return dialog
