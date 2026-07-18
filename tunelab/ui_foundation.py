from __future__ import annotations

from dataclasses import dataclass
import platform
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Iterable, Optional


FONT_BODY = "TuneLabBodyFont"
FONT_BODY_BOLD = "TuneLabBodyBoldFont"
FONT_SMALL = "TuneLabSmallFont"
FONT_SMALL_BOLD = "TuneLabSmallBoldFont"
FONT_CARD_TITLE = "TuneLabCardTitleFont"
FONT_KPI = "TuneLabKpiFont"
FONT_TITLE = "TuneLabTitleFont"
FONT_PLOT_TITLE = "TuneLabPlotTitleFont"
FONT_MONO = "TuneLabMonoFont"
FONT_HERO = "TuneLabHeroFont"
FONT_NAV_SECTION = "TuneLabNavSectionFont"

BODY_SIZE = 12
SMALL_SIZE = 10
TITLE_SIZE = 22
SECTION_SIZE = 13
ROW_HEIGHT = 30

# TuneLab intentionally uses one light, native-macOS-inspired visual system on
# every desktop.  The restrained system colours, generous control metrics and
# sidebar/surface hierarchy are equally legible on Windows while avoiding the
# platform-dependent appearance of Tk's legacy default themes.
WINDOW_BG = "#F5F5F7"
CONTENT_BG = "#F7F7F9"
SIDEBAR_BG = "#ECEEF2"
PANEL_BG = "#FFFFFF"
CONTROL_BG = "#FFFFFF"
CONTROL_SECONDARY_BG = "#F2F2F7"
HOVER_BG = "#E9E9EE"
PRESSED_BG = "#DEDEE5"
INK = "#1D1D1F"
MUTED = "#6E6E73"
TERTIARY = "#8E8E93"
SEPARATOR = "#D1D1D6"
SUBTLE_SEPARATOR = "#E5E5EA"
TABLE_HEADING_BG = "#F2F2F7"
SELECTION_BG = "#D8EAFB"
FOCUS_BG = "#E9F3FF"
SUCCESS = "#248A3D"
WARNING = "#9A5B00"
DANGER = "#D70015"
INFO_BG = "#EEF6FF"
SUCCESS_BG = "#EDF8F0"
WARNING_BG = "#FFF7E8"
DANGER_BG = "#FFF0F1"

ACTION_BLUE = "#007AFF"
ACTION_BLUE_HOVER = "#0A84FF"
ACTION_BLUE_PRESSED = "#0062CC"
ACTION_DISABLED = "#AEAEB2"
REGION_BUTTON_BG = CONTROL_SECONDARY_BG
REGION_BUTTON_HOVER = HOVER_BG
REGION_BUTTON_PRESSED = PRESSED_BG
REGION_BUTTON_INK = INK


@dataclass(frozen=True)
class FontFamilies:
    body: str
    display: str
    mono: str


@dataclass(frozen=True)
class WindowPlacement:
    width: int
    height: int
    x: int
    y: int

    @property
    def geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def default_sources_directory() -> Path:
    """Return the current plural ``sources`` folder for native file dialogs."""

    candidates = (
        Path.cwd() / "sources",
        Path(__file__).resolve().parents[1] / "sources",
        Path(getattr(sys, "_MEIPASS", Path.cwd())) / "sources",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return Path.cwd()


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
    """Open a large centred document window without forcing platform zoom."""

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
        desired_width=desired_width,
        desired_height=desired_height,
        screen_x=screen_x,
        screen_y=screen_y,
        horizontal_margin=48,
        vertical_margin=80,
    )
    # Never let a fixed minimum force the window back outside a small display.
    window.minsize(
        min(minimum_width, placement.width),
        min(minimum_height, placement.height),
    )
    window.geometry(placement.geometry)
    return placement


def _first_available(available: Iterable[str], candidates: Iterable[str], fallback: str) -> str:
    lookup = {value.casefold(): value for value in available}
    for candidate in candidates:
        match = lookup.get(candidate.casefold())
        if match is not None:
            return match
    return fallback


def select_font_families(root: tk.Misc, *, system: Optional[str] = None) -> FontFamilies:
    """Choose a stable native UI stack for macOS, Windows and development hosts."""

    available = tuple(tkfont.families(root))
    default_family = str(tkfont.Font(root=root, name="TkDefaultFont", exists=True).actual("family"))
    fixed_family = str(tkfont.Font(root=root, name="TkFixedFont", exists=True).actual("family"))
    current_system = system or platform.system()
    if current_system == "Darwin":
        # The hidden Apple family resolves to the current San Francisco UI
        # face and keeps Apple's own Chinese fallback (usually PingFang SC).
        body = default_family if default_family.startswith(".AppleSystemUI") else _first_available(
            available,
            ("SF Pro Text", "Helvetica Neue", "PingFang SC"),
            default_family,
        )
        display = body
        mono = _first_available(available, ("SF Mono", "Menlo", "Monaco"), fixed_family)
    elif current_system == "Windows":
        body = _first_available(
            available,
            ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI"),
            default_family,
        )
        display = _first_available(
            available,
            ("Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI"),
            body,
        )
        mono = _first_available(available, ("Cascadia Mono", "Cascadia Code", "Consolas"), fixed_family)
    else:
        body = _first_available(available, ("Inter", "Noto Sans CJK SC", "Noto Sans"), default_family)
        display = body
        mono = _first_available(available, ("JetBrains Mono", "Noto Sans Mono", "DejaVu Sans Mono"), fixed_family)
    return FontFamilies(body=body, display=display, mono=mono)


def configure_typography(
    root: tk.Misc,
    style: Optional[ttk.Style] = None,
    *,
    body_size: int = BODY_SIZE,
    small_size: int = SMALL_SIZE,
    title_size: int = TITLE_SIZE,
    section_size: int = SECTION_SIZE,
) -> ttk.Style:
    """Install TuneLab's cross-platform San-Francisco-like type hierarchy."""

    selected_style = style or ttk.Style(root)
    families = select_font_families(root)
    base_size = max(10, body_size)
    font_owner = root._root() if hasattr(root, "_root") else root
    installed_fonts = getattr(font_owner, "_tunelab_named_fonts", {})

    def install(
        name: str,
        *,
        size: int,
        weight: str = "normal",
        family: Optional[str] = None,
    ) -> None:
        font = installed_fonts.get(name)
        if font is None:
            try:
                font = tkfont.Font(root=root, name=name, exists=True)
            except tk.TclError:
                font = tkfont.Font(root=root, name=name, exists=False)
            installed_fonts[name] = font
        font.configure(family=family or families.body, size=size, weight=weight)

    install(FONT_BODY, size=base_size)
    install(FONT_BODY_BOLD, size=base_size, weight="bold")
    install(FONT_SMALL, size=small_size)
    install(FONT_SMALL_BOLD, size=small_size, weight="bold")
    install(FONT_CARD_TITLE, size=section_size, weight="bold", family=families.display)
    install(FONT_KPI, size=base_size + 3, weight="bold", family=families.display)
    install(FONT_TITLE, size=title_size, weight="bold", family=families.display)
    install(FONT_HERO, size=title_size + 7, weight="bold", family=families.display)
    install(FONT_PLOT_TITLE, size=section_size, weight="bold", family=families.display)
    install(FONT_NAV_SECTION, size=max(9, small_size - 1), weight="bold")
    install(FONT_MONO, size=max(small_size, base_size - 1), family=families.mono)
    setattr(font_owner, "_tunelab_named_fonts", installed_fonts)
    setattr(font_owner, "_tunelab_font_families", families)

    named_defaults = (
        ("TkDefaultFont", families.body, base_size, "normal"),
        ("TkTextFont", families.body, base_size, "normal"),
        ("TkHeadingFont", families.display, base_size, "bold"),
        ("TkFixedFont", families.mono, max(small_size, base_size - 1), "normal"),
        ("TkMenuFont", families.body, base_size, "normal"),
        ("TkCaptionFont", families.display, base_size, "bold"),
        ("TkSmallCaptionFont", families.body, small_size, "normal"),
        ("TkIconFont", families.body, base_size, "normal"),
        ("TkTooltipFont", families.body, small_size, "normal"),
    )
    for name, family, size, weight in named_defaults:
        try:
            tkfont.Font(root=root, name=name, exists=True).configure(
                family=family,
                size=size,
                weight=weight,
            )
        except tk.TclError:
            pass

    # Classic Tk surfaces and Combobox popdowns are outside ttk's style tree.
    for pattern, value in (
        ("*Font", FONT_BODY),
        ("*Menu.font", FONT_BODY),
        ("*Listbox.font", FONT_BODY),
        ("*TCombobox*Listbox.font", FONT_BODY),
        ("*Text.font", FONT_BODY),
        ("*Entry.font", FONT_BODY),
        ("*Menu.background", PANEL_BG),
        ("*Menu.foreground", INK),
        ("*Menu.activeBackground", SELECTION_BG),
        ("*Menu.activeForeground", INK),
        ("*Menu.borderWidth", 0),
    ):
        root.option_add(pattern, value, "widgetDefault")

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


def configure_macos_theme(
    root: tk.Misc,
    style: Optional[ttk.Style] = None,
    *,
    body_size: int = BODY_SIZE,
    small_size: int = SMALL_SIZE,
    title_size: int = TITLE_SIZE,
    section_size: int = SECTION_SIZE,
) -> ttk.Style:
    """Apply the shared macOS-inspired control language on every platform."""

    selected_style = style or ttk.Style(root)
    if "clam" in selected_style.theme_names():
        selected_style.theme_use("clam")
    selected_style = configure_typography(
        root,
        selected_style,
        body_size=body_size,
        small_size=small_size,
        title_size=title_size,
        section_size=section_size,
    )
    try:
        root.configure(background=WINDOW_BG)
    except tk.TclError:
        pass

    selected_style.configure(".", background=WINDOW_BG, foreground=INK, bordercolor=SEPARATOR)
    selected_style.configure("TFrame", background=WINDOW_BG, borderwidth=0)
    selected_style.configure("Surface.TFrame", background=PANEL_BG, borderwidth=0)
    selected_style.configure(
        "Card.TFrame",
        background=PANEL_BG,
        borderwidth=1,
        relief="solid",
        bordercolor=SUBTLE_SEPARATOR,
        lightcolor=SUBTLE_SEPARATOR,
        darkcolor=SUBTLE_SEPARATOR,
    )
    selected_style.configure("Sidebar.TFrame", background=SIDEBAR_BG, borderwidth=0)
    selected_style.configure("TLabel", background=WINDOW_BG, foreground=INK)
    selected_style.configure("Surface.TLabel", background=PANEL_BG, foreground=INK)
    selected_style.configure("Muted.TLabel", background=WINDOW_BG, foreground=MUTED)
    selected_style.configure("SurfaceMuted.TLabel", background=PANEL_BG, foreground=MUTED)
    selected_style.configure("Title.TLabel", background=WINDOW_BG, foreground=INK, font=FONT_TITLE)
    selected_style.configure("Hero.TLabel", background=WINDOW_BG, foreground=INK, font=FONT_HERO)
    selected_style.configure("Section.TLabel", background=WINDOW_BG, foreground=INK, font=FONT_CARD_TITLE)
    selected_style.configure("Eyebrow.TLabel", background=WINDOW_BG, foreground=TERTIARY, font=FONT_NAV_SECTION)

    selected_style.configure(
        "TButton",
        background=CONTROL_SECONDARY_BG,
        foreground=INK,
        padding=(13, 7),
        borderwidth=0,
        relief="flat",
        focusthickness=1,
        focuscolor=ACTION_BLUE,
    )
    selected_style.map(
        "TButton",
        background=[("disabled", CONTROL_SECONDARY_BG), ("pressed", PRESSED_BG), ("active", HOVER_BG)],
        foreground=[("disabled", ACTION_DISABLED), ("pressed", INK), ("active", INK)],
    )
    selected_style.configure("Quiet.TButton", background=CONTROL_SECONDARY_BG, foreground=INK, padding=(11, 6))
    selected_style.configure("Icon.TButton", background=CONTROL_SECONDARY_BG, foreground=INK, padding=(8, 6))
    selected_style.configure("Link.TButton", background=PANEL_BG, foreground=ACTION_BLUE, padding=(0, 3), font=FONT_BODY_BOLD)
    selected_style.map("Link.TButton", background=[("active", PANEL_BG), ("pressed", PANEL_BG)], foreground=[("active", ACTION_BLUE_HOVER)])
    selected_style.configure("Nav.TButton", background=SIDEBAR_BG, foreground=INK, anchor="w", padding=(14, 10), borderwidth=0)
    selected_style.map("Nav.TButton", background=[("active", HOVER_BG), ("pressed", PRESSED_BG)])
    selected_style.configure("ActiveNav.TButton", background="#DDE8F6", foreground="#0057B8", anchor="w", padding=(14, 10), borderwidth=0, font=FONT_BODY_BOLD)
    selected_style.map("ActiveNav.TButton", background=[("active", "#D5E3F3"), ("pressed", "#CADBF0")], foreground=[("active", "#0057B8")])

    for name in ("TEntry", "TCombobox"):
        selected_style.configure(
            name,
            fieldbackground=CONTROL_BG,
            background=CONTROL_BG,
            foreground=INK,
            insertcolor=INK,
            padding=(8, 7),
            borderwidth=1,
            relief="flat",
            bordercolor=SEPARATOR,
            lightcolor=SEPARATOR,
            darkcolor=SEPARATOR,
            arrowcolor=MUTED,
        )
        selected_style.map(
            name,
            fieldbackground=[("readonly", CONTROL_BG), ("disabled", CONTROL_SECONDARY_BG)],
            bordercolor=[("focus", ACTION_BLUE), ("active", TERTIARY)],
            foreground=[("disabled", ACTION_DISABLED), ("readonly", INK)],
            selectbackground=[("!disabled", SELECTION_BG)],
            selectforeground=[("!disabled", INK)],
        )
    selected_style.configure("TCheckbutton", background=PANEL_BG, foreground=INK, padding=(3, 3))
    selected_style.map("TCheckbutton", background=[("active", PANEL_BG)], foreground=[("disabled", ACTION_DISABLED)])
    selected_style.configure("TRadiobutton", background=PANEL_BG, foreground=INK, padding=(3, 3))
    selected_style.map("TRadiobutton", background=[("active", PANEL_BG)], foreground=[("disabled", ACTION_DISABLED)])

    selected_style.configure("TNotebook", background=WINDOW_BG, borderwidth=0, tabmargins=(0, 0, 0, 8))
    selected_style.configure("TNotebook.Tab", background=CONTROL_SECONDARY_BG, foreground=MUTED, padding=(15, 8), borderwidth=0)
    selected_style.map(
        "TNotebook.Tab",
        background=[("selected", PANEL_BG), ("active", HOVER_BG)],
        foreground=[("selected", INK), ("active", INK)],
        expand=[("selected", (0, 0, 0, 1))],
    )
    selected_style.configure(
        "Treeview",
        rowheight=ROW_HEIGHT,
        background=PANEL_BG,
        fieldbackground=PANEL_BG,
        foreground=INK,
        borderwidth=0,
        relief="flat",
    )
    selected_style.map("Treeview", background=[("selected", SELECTION_BG)], foreground=[("selected", INK)])
    selected_style.configure(
        "Treeview.Heading",
        background=TABLE_HEADING_BG,
        foreground=MUTED,
        padding=(8, 7),
        relief="flat",
        borderwidth=0,
        font=FONT_SMALL_BOLD,
    )
    selected_style.map("Treeview.Heading", background=[("active", HOVER_BG)])
    selected_style.configure("TSeparator", background=SUBTLE_SEPARATOR)
    selected_style.configure("TPanedwindow", background=SUBTLE_SEPARATOR, sashwidth=1)
    selected_style.configure("TScale", background=PANEL_BG, troughcolor=CONTROL_SECONDARY_BG)
    selected_style.configure(
        "TProgressbar",
        background=ACTION_BLUE,
        troughcolor=CONTROL_SECONDARY_BG,
        borderwidth=0,
        lightcolor=ACTION_BLUE,
        darkcolor=ACTION_BLUE,
    )
    selected_style.configure("Vertical.TScrollbar", background="#C7C7CC", troughcolor=WINDOW_BG, borderwidth=0, arrowcolor=MUTED)
    selected_style.configure("Horizontal.TScrollbar", background="#C7C7CC", troughcolor=WINDOW_BG, borderwidth=0, arrowcolor=MUTED)
    configure_action_styles(selected_style)
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
        padding=(14, 7),
        borderwidth=0,
        font=FONT_BODY_BOLD,
        relief="flat",
        focuscolor=ACTION_BLUE_PRESSED,
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", ACTION_DISABLED),
            ("pressed", ACTION_BLUE_PRESSED),
            ("active", ACTION_BLUE_HOVER),
        ],
        foreground=[
            ("disabled", "#F5F5F7"),
            ("pressed", "white"),
            ("active", "white"),
        ],
    )
    style.configure(
        "RegionMatch.TButton",
        background=REGION_BUTTON_BG,
        foreground=REGION_BUTTON_INK,
        padding=(11, 6),
        borderwidth=0,
        font=FONT_BODY,
        relief="flat",
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
    style.configure(
        "Danger.TButton",
        background=DANGER,
        foreground="white",
        padding=(14, 7),
        borderwidth=0,
        font=FONT_BODY_BOLD,
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", ACTION_DISABLED), ("pressed", "#A90011"), ("active", "#E10A20")],
        foreground=[("disabled", "#F5F5F7"), ("pressed", "white"), ("active", "white")],
    )
    return style
