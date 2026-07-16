from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Optional


FONT_BODY = "TuneLabBodyFont"
FONT_BODY_BOLD = "TuneLabBodyBoldFont"
FONT_SMALL = "TuneLabSmallFont"
FONT_SMALL_BOLD = "TuneLabSmallBoldFont"
FONT_CARD_TITLE = "TuneLabCardTitleFont"
FONT_KPI = "TuneLabKpiFont"
FONT_TITLE = "TuneLabTitleFont"
FONT_PLOT_TITLE = "TuneLabPlotTitleFont"
FONT_MONO = "TuneLabMonoFont"

BODY_SIZE = 11
SMALL_SIZE = 10
TITLE_SIZE = 18
SECTION_SIZE = 12
ROW_HEIGHT = 28
TABLE_HEADING_BG = "#F2F4F7"

ACTION_BLUE = "#2563EB"
ACTION_BLUE_HOVER = "#1D4ED8"
ACTION_BLUE_PRESSED = "#1E40AF"
ACTION_DISABLED = "#98A2B3"
REGION_BUTTON_BG = "#F2F4F7"
REGION_BUTTON_HOVER = "#E4E7EC"
REGION_BUTTON_PRESSED = "#D0D5DD"
REGION_BUTTON_INK = "#344054"


@dataclass(frozen=True)
class WindowPlacement:
    width: int
    height: int
    x: int
    y: int

    @property
    def geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def calculate_window_placement(
    screen_width: int,
    screen_height: int,
    *,
    desired_width: int,
    desired_height: int,
    screen_x: int = 0,
    screen_y: int = 0,
    horizontal_margin: int = 0,
    vertical_margin: int = 0,
) -> WindowPlacement:
    """Fit a desired window inside one screen and centre it exactly."""

    if screen_width <= 0 or screen_height <= 0:
        raise ValueError("屏幕尺寸必须大于 0。")
    usable_width = max(1, screen_width - horizontal_margin)
    usable_height = max(1, screen_height - vertical_margin)
    width = min(desired_width, usable_width)
    height = min(desired_height, usable_height)
    x = screen_x + max(0, (screen_width - width) // 2)
    y = screen_y + max(0, (screen_height - height) // 2)
    return WindowPlacement(width, height, x, y)


def fit_window_to_screen(
    window: tk.Misc,
    *,
    desired_width: int = 1520,
    desired_height: int = 980,
    minimum_width: int = 1180,
    minimum_height: int = 760,
) -> WindowPlacement:
    """Maximize into the usable desktop, with a full-screen geometry fallback."""

    screen_width = int(window.winfo_screenwidth())
    screen_height = int(window.winfo_screenheight())
    try:
        screen_x = int(window.winfo_vrootx())
        screen_y = int(window.winfo_vrooty())
    except (AttributeError, tk.TclError):
        screen_x = screen_y = 0
    placement = calculate_window_placement(
        screen_width,
        screen_height,
        # The app should occupy the available desktop like a native maximized
        # workspace.  ``desired_*`` remain as API-compatible lower targets for
        # callers on virtual roots larger than the physical screen.
        desired_width=max(desired_width, screen_width),
        desired_height=max(desired_height, screen_height),
        screen_x=screen_x,
        screen_y=screen_y,
    )
    # Never let a fixed minimum force the window back outside a small display.
    window.minsize(
        min(minimum_width, placement.width),
        min(minimum_height, placement.height),
    )
    window.geometry(placement.geometry)
    try:
        # Tk 9 on macOS resolves this to the visible frame (menu bar and Dock
        # excluded); Windows/Linux window managers provide the equivalent work
        # area.  Older Aqua Tk versions fall back to the full-screen geometry.
        window.state("zoomed")
    except (AttributeError, tk.TclError):
        pass
    return placement


def configure_typography(
    root: tk.Misc,
    style: Optional[ttk.Style] = None,
    *,
    body_size: int = BODY_SIZE,
    small_size: int = SMALL_SIZE,
    title_size: int = TITLE_SIZE,
    section_size: int = SECTION_SIZE,
) -> ttk.Style:
    """Install one font family and hierarchy for all Tk/ttk surfaces."""

    selected_style = style or ttk.Style(root)
    if "clam" in selected_style.theme_names():
        selected_style.theme_use("clam")

    default_font = tkfont.Font(root=root, name="TkDefaultFont", exists=True)
    family = str(default_font.actual("family"))
    native_size = abs(int(default_font.cget("size")))
    base_size = max(10, min(body_size, native_size))
    font_owner = root._root() if hasattr(root, "_root") else root
    installed_fonts = getattr(font_owner, "_tunelab_named_fonts", {})

    def install(name: str, *, size: int, weight: str = "normal") -> None:
        font = installed_fonts.get(name)
        if font is None:
            try:
                font = tkfont.Font(root=root, name=name, exists=True)
            except tk.TclError:
                font = tkfont.Font(root=root, name=name, exists=False)
            installed_fonts[name] = font
        font.configure(family=family, size=size, weight=weight)

    install(FONT_BODY, size=base_size)
    install(FONT_BODY_BOLD, size=base_size, weight="bold")
    install(FONT_SMALL, size=max(small_size, base_size - 1))
    install(FONT_SMALL_BOLD, size=max(small_size, base_size - 1), weight="bold")
    install(FONT_CARD_TITLE, size=section_size, weight="bold")
    install(FONT_KPI, size=base_size + 2, weight="bold")
    install(FONT_TITLE, size=title_size, weight="bold")
    install(FONT_PLOT_TITLE, size=section_size, weight="bold")
    # Numeric matrices/diffs keep their alignment and size, but deliberately
    # use the same family as every other visible control.
    install(FONT_MONO, size=max(small_size, base_size - 1))
    setattr(font_owner, "_tunelab_named_fonts", installed_fonts)

    for name, size in (
        ("TkDefaultFont", base_size),
        ("TkTextFont", base_size),
        ("TkHeadingFont", base_size),
        ("TkFixedFont", max(small_size, base_size - 1)),
        ("TkMenuFont", base_size),
        ("TkCaptionFont", base_size),
        ("TkSmallCaptionFont", max(small_size, base_size - 1)),
        ("TkIconFont", base_size),
        ("TkTooltipFont", max(small_size, base_size - 1)),
    ):
        try:
            tkfont.Font(root=root, name=name, exists=True).configure(
                family=family,
                size=size,
            )
        except tk.TclError:
            pass

    # Classic Tk menus and the native Combobox popdown do not inherit ttk
    # styles.  Option defaults cover those surfaces (including 前乘/后乘 and
    # RGB 联动 choices) while explicit title/KPI fonts still take precedence.
    for pattern, font_name in (
        ("*Font", FONT_BODY),
        ("*Menu.font", FONT_BODY),
        ("*Listbox.font", FONT_BODY),
        ("*TCombobox*Listbox.font", FONT_BODY),
        ("*Text.font", FONT_BODY),
        ("*Entry.font", FONT_BODY),
    ):
        root.option_add(pattern, font_name, "widgetDefault")

    selected_style.configure(".", font=FONT_BODY)
    selected_style.configure("TLabel", font=FONT_BODY)
    selected_style.configure("TButton", font=FONT_BODY)
    selected_style.configure("TEntry", font=FONT_BODY)
    selected_style.configure("TCombobox", font=FONT_BODY)
    selected_style.configure("TCheckbutton", font=FONT_BODY)
    selected_style.configure("TRadiobutton", font=FONT_BODY)
    selected_style.configure("TMenubutton", font=FONT_BODY)
    selected_style.configure("TNotebook.Tab", font=FONT_BODY)
    selected_style.configure("Treeview", font=FONT_SMALL)
    selected_style.configure("Treeview.Heading", font=FONT_SMALL_BOLD)
    return selected_style


def bind_responsive_wrap(
    label: ttk.Label,
    *,
    horizontal_padding: int = 20,
    minimum: int = 260,
) -> None:
    """Keep long status/help text inside its allocated label box."""

    def update(event: tk.Event[tk.Misc]) -> None:
        width = max(minimum, int(getattr(event, "width", minimum)) - horizontal_padding)
        try:
            current = int(float(label.cget("wraplength")))
        except (TypeError, ValueError, tk.TclError):
            current = -1
        if current != width:
            label.configure(wraplength=width)

    label.bind("<Configure>", update, add="+")


def elide_canvas_text(
    widget: tk.Misc,
    value: str,
    font_name: str,
    maximum_width: int,
) -> str:
    """Ellipsize Canvas text so it never paints outside its card."""

    if maximum_width <= 0:
        return ""
    font = tkfont.Font(root=widget, name=font_name, exists=True)
    if font.measure(value) <= maximum_width:
        return value
    suffix = "…"
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if font.measure(value[:middle] + suffix) <= maximum_width:
            low = middle
        else:
            high = middle - 1
    return value[:low].rstrip() + suffix


def configure_action_styles(style: ttk.Style) -> ttk.Style:
    """Install identical primary and Region-match button states in every module."""

    style.configure(
        "Primary.TButton",
        background=ACTION_BLUE,
        foreground="white",
        padding=(11, 6),
        borderwidth=0,
        font=FONT_BODY,
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", ACTION_DISABLED),
            ("pressed", ACTION_BLUE_PRESSED),
            ("active", ACTION_BLUE_HOVER),
        ],
        foreground=[
            ("disabled", "#F2F4F7"),
            ("pressed", "white"),
            ("active", "white"),
        ],
    )
    style.configure(
        "RegionMatch.TButton",
        background=REGION_BUTTON_BG,
        foreground=REGION_BUTTON_INK,
        padding=(8, 4),
        borderwidth=0,
        font=FONT_BODY,
    )
    style.map(
        "RegionMatch.TButton",
        background=[
            ("pressed", REGION_BUTTON_PRESSED),
            ("active", REGION_BUTTON_HOVER),
        ],
        foreground=[
            ("pressed", REGION_BUTTON_INK),
            ("active", REGION_BUTTON_INK),
        ],
    )
    return style
