"""
Themes. Light and Dark follow LVGL's default theme (the dash and the charger screen use LVGL): flat rounded cards
on a grey screen, LVGL's blue as the accent and its palette for states. Daylight is for a laptop in direct
sun: black on white, no grey text, thick outlines, bigger bold type, and solid colour only where something
needs attention, so a problem stands out even when the screen is washed out.

Widgets ask for colours by state (OK, WARN, ...) rather than by hue, so each theme picks colours that keep
their contrast on its own background.
"""

import os
import tempfile
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette

# LVGL palette (lv_palette_main)
BLUE = "#2196F3"
LIGHT_BLUE = "#03A9F4"
RED = "#F44336"
ORANGE = "#FF9800"
AMBER = "#FFC107"
GREEN = "#4CAF50"
TEAL = "#009688"
PURPLE = "#9C27B0"
GREY = "#9E9E9E"

# States
OK, CAUTION, WARN, BAD, INFO, NEUTRAL = "ok", "caution", "warn", "bad", "info", "neutral"


@dataclass(frozen=True)
class Theme:
    name: str
    screen: str             # Window background
    card: str               # Cards
    tile: str               # Neutral tiles inside cards
    text: str
    text_muted: str
    border: str
    accent: str = BLUE
    accent_hover: str = "#1E88E5"
    tint: float = 0.22      # How strongly state colours tint a tile (tinted tiles)
    solid_tiles: bool = False   # Daylight: plain outlined tiles when normal, solid colour when not
    scale: float = 1.0      # Type and tile size
    line: int = 1           # Outline width
    bold: bool = False      # All text bold
    plot_width: float = 2.0
    state_text: dict = field(default_factory=lambda: {OK: GREEN, CAUTION: "#B28704", WARN: ORANGE, BAD: RED,
                                                      INFO: BLUE, NEUTRAL: GREY})
    pills: dict = field(default_factory=lambda: {OK: (GREEN, "#FFFFFF"), CAUTION: (AMBER, "#212121"),
                                                 WARN: (ORANGE, "#FFFFFF"), BAD: (RED, "#FFFFFF"),
                                                 INFO: (BLUE, "#FFFFFF"), NEUTRAL: (GREY, "#FFFFFF")})
    hues: dict = field(default_factory=lambda: {OK: GREEN, CAUTION: AMBER, WARN: ORANGE, BAD: RED, INFO: BLUE,
                                                NEUTRAL: GREY})
    traces: tuple = (BLUE, ORANGE, GREEN, RED, PURPLE, TEAL, AMBER, LIGHT_BLUE)
    group_border: str = "#E3E5E8"   # Faint box around a cell's voltage and temperature
    gradient_v: str = GREEN         # Shade for voltage: lightest at the pack's lowest cell, strongest at the highest
    gradient_t: str = AMBER         # Shade for temperature
    gradient: tuple = (0.06, 0.5)   # Tint strength at the lowest and highest
    balance: str = TEAL             # BAL tag

    @property
    def low(self):
        """Low side: lowest cell, coldest sensor, near an under-limit"""
        return self.hues[INFO]

    @property
    def high(self):
        """High side: highest cell, hottest sensor, near an over-limit"""
        return self.hues[BAD]

    def gradient_fill(self, kind, position):
        """Tile fill for a normal reading. kind "v" or "t"; position 0 (pack lowest) .. 1 (pack highest)"""
        lo, hi = self.gradient
        return self.tinted(self.gradient_v if kind == "v" else self.gradient_t, lo + (hi - lo) * position)

    def color(self, state):
        """Text colour for a state on a card"""
        return self.state_text.get(state, self.text)

    def pill(self, state):
        """(background, text) of a status pill"""
        return self.pills.get(state, self.pills[NEUTRAL])

    def tinted(self, color, strength=None):
        """A colour over the tile colour, readable with the theme's text colour"""
        f = self.tint if strength is None else strength
        a, b = QColor(self.tile), QColor(color)
        return QColor(round(a.red() + (b.red() - a.red()) * f), round(a.green() + (b.green() - a.green()) * f),
                      round(a.blue() + (b.blue() - a.blue()) * f))

    def tile_style(self, state):
        """(fill, text colour, outline or None) of a cell / sensor tile. state: a state, "open" or None (no data)"""
        if state is None:
            return QColor(self.tile), QColor(self.text_muted), (self.border if self.solid_tiles else None)
        if self.solid_tiles:
            if state == OK:
                return QColor(self.tile), QColor(self.text), self.border
            if state == "open":
                return QColor("#000000"), QColor("#FFFFFF"), None
            bg, fg = self.pill(state)
            return QColor(bg), QColor(fg), None
        if state == "open":
            return self.tinted(GREY, 0.5), QColor(self.text), None
        return self.tinted(self.hues[state], 0.6 if state == BAD else None), QColor(self.text), None


# LVGL default theme: light screen lv_palette_lighten(GREY, 4), dark screen 0x15171A / card 0x282B30
LIGHT = Theme("light", screen="#F2F3F5", card="#FFFFFF", tile="#F2F3F5", text="#212121", text_muted="#6B6F76",
              border="#E0E0E0", tint=0.28)
DARK = Theme("dark", screen="#15171A", card="#282B30", tile="#2F3237", text="#F5F5F5", text_muted="#9DA3AB",
             border="#3A3E44", tint=0.38, group_border="#3A3E44", gradient=(0.1, 0.55))

# Bright daylight: every colour at least ~6:1 against white (WCAG AA is 4.5:1), darker LVGL palette shades (900s)
DAYLIGHT = Theme(
    "daylight", screen="#FFFFFF", card="#FFFFFF", tile="#FFFFFF", text="#000000", text_muted="#1A1A1A",
    border="#000000", accent="#0D47A1", accent_hover="#08306B", solid_tiles=True, scale=1.2, line=2, bold=True,
    plot_width=3.0,
    state_text={OK: "#1B5E20", CAUTION: "#6D4C00", WARN: "#A33A00", BAD: "#B71C1C", INFO: "#0D47A1", NEUTRAL: "#1A1A1A"},
    pills={OK: ("#1B5E20", "#FFFFFF"), CAUTION: ("#FFD600", "#000000"), WARN: ("#FF9800", "#000000"),
           BAD: ("#B71C1C", "#FFFFFF"), INFO: ("#0D47A1", "#FFFFFF"), NEUTRAL: ("#424242", "#FFFFFF")},
    hues={OK: "#1B5E20", CAUTION: "#FFD600", WARN: "#FF9800", BAD: "#B71C1C", INFO: "#0D47A1", NEUTRAL: "#424242"},
    traces=("#0D47A1", "#E65100", "#1B5E20", "#B71C1C", "#4A148C", "#004D40", "#000000", "#880E4F"),
    group_border="#8A8A8A", gradient_v="#2E7D32", gradient_t="#FFA000", gradient=(0.0, 0.45), balance="#004D40",
)

THEMES = {"light": LIGHT, "dark": DARK, "daylight": DAYLIGHT}
CURRENT = LIGHT


def system_is_dark():
    try:
        return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:                                  # Qt < 6.5
        return False


def resolve(mode):
    """mode: "system", "light", "dark" or "daylight" -> Theme"""
    if mode == "system":
        return DARK if system_is_dark() else LIGHT
    return THEMES.get(mode, LIGHT)


def palette(t):
    p = QPalette()
    roles = {
        QPalette.Window: t.screen, QPalette.WindowText: t.text, QPalette.Base: t.card,
        QPalette.AlternateBase: t.tile, QPalette.Text: t.text, QPalette.Button: t.card,
        QPalette.ButtonText: t.text, QPalette.ToolTipBase: t.card, QPalette.ToolTipText: t.text,
        QPalette.Highlight: t.accent, QPalette.HighlightedText: "#FFFFFF", QPalette.PlaceholderText: t.text_muted,
        QPalette.Link: t.accent,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(t.text_muted))
    return p


_ICON_DIR = os.path.join(tempfile.gettempdir(), "bms_gui_icons")
_ICONS = {
    "up": '<polyline points="2.5,7.5 6,4 9.5,7.5" fill="none" stroke="{c}" stroke-width="{w}" '
          'stroke-linecap="round" stroke-linejoin="round"/>',
    "down": '<polyline points="2.5,4.5 6,8 9.5,4.5" fill="none" stroke="{c}" stroke-width="{w}" '
            'stroke-linecap="round" stroke-linejoin="round"/>',
    "check": '<polyline points="2.5,6.5 5,9 9.5,3.5" fill="none" stroke="{c}" stroke-width="{w}" '
             'stroke-linecap="round" stroke-linejoin="round"/>',
}


def icon(name, color, width=1.8):
    """Small SVG icon in a colour, as a path a stylesheet can use (Qt stylesheets need image files)"""
    os.makedirs(_ICON_DIR, exist_ok=True)
    path = os.path.join(_ICON_DIR, f"{name}_{color.lstrip('#')}_{width:g}.svg")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">' +
                    _ICONS[name].format(c=color, w=width) + "</svg>")
    return path.replace("\\", "/")


def stylesheet(t):
    def pt(size):
        return f"{size * t.scale:.1f}pt"
    weight = 600 if t.bold else 400
    line = t.line
    arrow = 2.6 if t.bold else 1.8
    danger_bg, danger_fg = t.pill(BAD)
    return f"""
    QWidget {{ font-family: "Montserrat", "Segoe UI", "Inter", sans-serif; font-size: {pt(10)}; font-weight: {weight}; }}
    QMainWindow, QWidget#screen {{ background: {t.screen}; }}
    QFrame#card {{ background: {t.card}; border-radius: 12px; border: {line}px solid {t.border}; }}
    QLabel#cardTitle {{ font-size: {pt(11)}; font-weight: {700 if t.bold else 600}; }}
    QLabel#muted {{ color: {t.text_muted}; }}
    QLabel#big {{ font-size: {pt(20)}; font-weight: {700 if t.bold else 600}; }}
    QLabel#value {{ font-family: "Consolas", "Cascadia Mono", monospace; font-weight: {700 if t.bold else 400}; }}
    QLabel#pill {{ border-radius: 10px; padding: 2px 10px; font-weight: {700 if t.bold else 600}; }}
    QFrame#header {{ background: {t.card}; border-bottom: {line}px solid {t.border}; }}
    QPushButton {{ background: {t.tile}; border: {line}px solid {t.border}; border-radius: 8px; padding: 6px 14px; }}
    QPushButton:hover {{ border-color: {t.accent}; }}
    QPushButton:pressed {{ background: {t.border}; color: {t.card}; }}
    QPushButton:disabled {{ color: {t.text_muted}; }}
    QPushButton#primary {{ background: {t.accent}; color: #FFFFFF; border: none; font-weight: 700; }}
    QPushButton#primary:hover {{ background: {t.accent_hover}; }}
    QPushButton#danger {{ background: {danger_bg}; color: {danger_fg}; border: none; font-weight: 700; }}
    QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {{
        background: {t.tile}; border: {line}px solid {t.border}; border-radius: 8px; padding: 4px 8px; min-height: 22px; }}
    QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus {{ border-color: {t.accent}; }}
    QDoubleSpinBox[changed="true"], QComboBox[changed="true"] {{ border: {line + 1}px solid {t.accent}; }}
    QComboBox QAbstractItemView {{ background: {t.card}; color: {t.text}; border: {line}px solid {t.border};
                                   selection-background-color: {t.accent}; selection-color: #FFFFFF; outline: 0; }}
    QComboBox QAbstractItemView::item {{ min-height: {round(24 * t.scale)}px; padding: 2px 8px; color: {t.text};
                                         background: {t.card}; }}
    QComboBox QAbstractItemView::item:hover, QComboBox QAbstractItemView::item:selected {{
        background: {t.accent}; color: #FFFFFF; }}
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{ background: transparent; color: {t.text_muted}; padding: 8px 18px; margin-right: 4px;
                    border-bottom: {line + 2}px solid transparent; font-weight: {700 if t.bold else 600}; }}
    QTabBar::tab:selected {{ color: {t.text}; border-bottom: {line + 2}px solid {t.accent}; }}
    QTabBar::tab:hover {{ color: {t.text}; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: {10 + 4 * (line - 1)}px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {t.text_muted if t.bold else t.border}; border-radius: 4px; min-height: 30px; }}
    QScrollBar:horizontal {{ background: transparent; height: {10 + 4 * (line - 1)}px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {t.text_muted if t.bold else t.border}; border-radius: 4px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QTableView, QListWidget {{ background: {t.card}; border: {line}px solid {t.border}; border-radius: 12px;
                               gridline-color: {t.border}; alternate-background-color: {t.tile}; }}
    QHeaderView::section {{ background: {t.card}; color: {t.text_muted}; border: none;
                            border-bottom: {line}px solid {t.border}; padding: 6px; font-weight: 700; }}
    QProgressBar {{ background: {t.tile}; border: {line - 1}px solid {t.border}; border-radius: 6px; height: 12px;
                    text-align: center; }}
    QProgressBar::chunk {{ background: {t.accent}; border-radius: 5px; }}
    QSplitter::handle {{ background: transparent; }}
    QStatusBar {{ background: {t.card}; color: {t.text_muted}; border-top: {line}px solid {t.border}; }}
    QToolTip {{ background: {t.card}; color: {t.text}; border: {line}px solid {t.border}; padding: 6px; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border: {line}px solid {t.text_muted}; border-radius: 4px;
                            background: {t.tile}; }}
    QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent};
                                    image: url({icon("check", "#FFFFFF", arrow + 0.4)}); }}
    QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 18px; border: none; background: transparent; }}
    QAbstractSpinBox::up-arrow {{ image: url({icon("up", t.text_muted, arrow)}); width: 10px; height: 10px; }}
    QAbstractSpinBox::down-arrow {{ image: url({icon("down", t.text_muted, arrow)}); width: 10px; height: 10px; }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox::down-arrow {{ image: url({icon("down", t.text_muted, arrow)}); width: 12px; height: 12px; }}
    """


def apply(app, t):
    global CURRENT
    CURRENT = t
    app.setStyle("Fusion")
    app.setPalette(palette(t))
    app.setStyleSheet(stylesheet(t))
