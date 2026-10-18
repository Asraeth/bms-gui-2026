"""
Widgets for the BMS GUI: a wrapping flow layout, the painted slave cards, key/value info cards, status pills
and the live plot.
"""

from dataclasses import dataclass

import pyqtgraph as pg
from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QFrame, QGridLayout, QLabel, QLayout, QSizePolicy, QToolTip, QVBoxLayout, QWidget,
                               QHBoxLayout, QPushButton)

from . import theme as th


# ------------------------------------------------------------------------------------------------
# Flow layout (Qt's FlowLayout example): children left to right, wrapping to the width
# ------------------------------------------------------------------------------------------------

class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing=12):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, apply):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        # Items of one width (the slave cards) form a centred grid; the last row lines up with the others
        left = r.x()
        widths = {item.sizeHint().width() for item in self._items}
        if len(widths) == 1:
            w = widths.pop()
            per_row = max(1, min(len(self._items), (r.width() + self._spacing) // (w + self._spacing)))
            left += max(0, (r.width() - (per_row * w + (per_row - 1) * self._spacing)) // 2)
        x, y, line_height = left, r.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > r.right() + 1 and line_height > 0:
                x, y = left, y + line_height + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


# ------------------------------------------------------------------------------------------------
# Slave card
# ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CellState:
    voltage: float = None
    delta_mv: float = None
    balancing: bool = False
    fault: bool = False
    open_wire: bool = False
    lowest: bool = False
    highest: bool = False


@dataclass(frozen=True)
class SlaveState:
    voltage: float = None
    die_temp: float = None
    comms_error: bool = False
    fault: bool = False
    cells: tuple = ()
    temps: tuple = ()
    v_range: tuple = None       # Lowest / highest cell not near a limit, for the shading
    t_range: tuple = None       # Coldest / hottest sensor not near a limit, for the shading
    t_extremes: tuple = None    # Pack's coldest / hottest sensor (▼ ▲)
    charging: bool = False      # Temperatures judged against the charge window


# How close to a limit before the near-limit bar shows (display only; the AMS trips at the limits)
NEAR_UV_V = 0.3
NEAR_OV_V = 0.05
NEAR_TEMP_C = 10.0
# Smallest spread the shading stretches over, so a balanced pack doesn't look patchy
MIN_SPAN_V = 0.02
MIN_SPAN_C = 5.0


def temp_window(model, charging):
    if charging:
        return model.cell_charge_temp_min_c, model.cell_charge_temp_max_c
    return model.cell_temp_min_c, model.cell_temp_max_c


def judge_voltage(model, v):
    """(past a limit, near-limit side "low" / "high" / None)"""
    if v <= model.cell_uv_v or v >= model.cell_ov_v:
        return True, "low" if v <= model.cell_uv_v else "high"
    if v < model.cell_uv_v + NEAR_UV_V:
        return False, "low"
    if v > model.cell_ov_v - NEAR_OV_V:
        return False, "high"
    return False, None


def judge_temp(model, t, charging):
    lo, hi = temp_window(model, charging)
    if t <= lo or t >= hi:
        return True, "low" if t <= lo else "high"
    if t < lo + NEAR_TEMP_C:
        return False, "low"
    if t > hi - NEAR_TEMP_C:
        return False, "high"
    return False, None


def shading_range(values, judge):
    """Range the shading spans: the readings that aren't near or past a limit (those have their own marks), so one
    bad cell doesn't flatten the rest"""
    normal = [v for v in values if judge(v) == (False, None)]
    pool = normal or values
    return (min(pool), max(pool)) if pool else None


class SlaveCard(QWidget):
    """One slave: header, then one faint box per cell holding its voltage and its temperature. Painted directly
    (fast with hundreds of cells). Click a tile or the header to plot it.

    Voltage and temperature tiles work the same way:
      shade        lighter = lower in the pack, stronger = higher (green voltage, amber temperature)
      outline ▼ ▲  the pack's lowest / highest (blue / red)
      bar          near a limit: blue = near the under-limit, red = near the over-limit
      red tile     past a limit (or a cell fault / open wire, or an open sensor)
      BAL          the cell is balancing
    """

    clicked = Signal(str, bool)         # signal name, add to the plot (Ctrl)

    COLS = 4
    # Sizes at theme scale 1
    SIZES = dict(PAD=12, GAP=6, GROUP_PAD=4, INNER_GAP=4, TILE_W=62, CELL_H=44, TEMP_H=28, HEADER_H=26)

    def __init__(self, slave, model, parent=None):
        super().__init__(parent)
        self.slave = slave
        self.model = model
        self.theme = th.LIGHT
        self.state = SlaveState()
        self._hits = []
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

    def __getattr__(self, name):
        if name in SlaveCard.SIZES:
            return round(SlaveCard.SIZES[name] * self.theme.scale)
        raise AttributeError(name)

    @property
    def groups(self):
        return max(self.model.cells_per_slave, self.model.temps_per_slave)

    def _group_size(self):
        h = self.GROUP_PAD * 2
        if self.model.cells_per_slave:
            h += self.CELL_H
        if self.model.temps_per_slave:
            h += self.TEMP_H
        if self.model.cells_per_slave and self.model.temps_per_slave:
            h += self.INNER_GAP
        return self.TILE_W + 2 * self.GROUP_PAD, h

    def sizeHint(self):
        gw, gh = self._group_size()
        rows = (self.groups + self.COLS - 1) // self.COLS
        return QSize(2 * self.PAD + self.COLS * gw + (self.COLS - 1) * self.GAP,
                     2 * self.PAD + self.HEADER_H + rows * gh + (rows - 1) * self.GAP)

    minimumSizeHint = sizeHint

    def set_theme(self, t):
        self.theme = t
        self.updateGeometry()                   # Size follows the theme's scale
        self.update()

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self.update()

    # ---- Judging readings -------------------------------------------------------------------------

    def _temp_window(self):
        return temp_window(self.model, self.state.charging)

    def judge_voltage(self, v):
        return judge_voltage(self.model, v)

    def judge_temp(self, t):
        return judge_temp(self.model, t, self.state.charging)

    def _mark(self, p, r, glyph, color, font):
        """▼ / ▲ in the tile's top-left corner; returns the width taken"""
        p.setFont(font)
        p.setPen(QColor(color))
        w = p.fontMetrics().horizontalAdvance(glyph) + 2
        p.drawText(QRectF(r.x() + 4, r.y() + 2, w, p.fontMetrics().height()), Qt.AlignLeft | Qt.AlignTop, glyph)
        return w

    @staticmethod
    def _position(value, value_range, min_span):
        if value_range is None:
            return 0.5
        lo, hi = value_range
        span = max(hi - lo, min_span)
        mid = (lo + hi) / 2
        return min(max((value - (mid - span / 2)) / span, 0.0), 1.0)

    # ---- Painting ----------------------------------------------------------------------------------

    def _tile(self, p, r, fill, outline, line):
        p.setPen(QPen(QColor(outline), line) if outline else Qt.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(r.adjusted(line / 2, line / 2, -line / 2, -line / 2), 6, 6)

    def _bar(self, p, r, side, inset):
        t = self.theme
        h = (3 + (1 if t.solid_tiles else 0)) * t.scale
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.low if side == "low" else t.high))
        bar = QRectF(r.x() + 7, r.bottom() - inset - h - 2, r.width() - 14, h)
        p.drawRoundedRect(bar, h / 2, h / 2)
        return h + inset + 3

    def paintEvent(self, _event):
        t, s, m = self.theme, self.state, self.model
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        half = t.line / 2 + 0.5
        rect = QRectF(self.rect()).adjusted(half, half, -half, -half)
        alarm = th.BAD if s.fault else th.WARN if s.comms_error else None
        p.setPen(QPen(QColor(t.hues[alarm] if alarm else t.border), t.line + (1 if alarm else 0)))
        p.setBrush(QColor(t.card))
        p.drawRoundedRect(rect, 12, 12)
        self._hits = []
        tile_line = max(1.0, t.line * 0.75)
        mark_line = tile_line + 1.5

        base = QFont(self.font())
        bold = QFont(base)
        bold.setBold(True)
        small = QFont(base)
        small.setPointSizeF(base.pointSizeF() * 0.8)
        small.setBold(True)
        mono = QFont("Consolas")
        mono.setPointSizeF(base.pointSizeF() * 1.05)
        mono.setBold(t.bold)
        mono_bold = QFont(mono)
        mono_bold.setBold(True)

        # Header
        x0, y0 = self.PAD, self.PAD
        header = QRectF(x0, y0, self.width() - 2 * self.PAD, self.HEADER_H)
        p.setPen(QColor(t.text))
        p.setFont(bold)
        p.drawText(header, Qt.AlignLeft | Qt.AlignVCenter, f"Slave {self.slave + 1}")
        info = "—" if s.voltage is None else f"{s.voltage:.2f} V" + ("" if s.die_temp is None else f"  ·  {s.die_temp:.1f} °C")
        if alarm:
            info = ("FAULT" if s.fault else "COMMS") + "  ·  " + info
            p.setPen(QColor(t.color(alarm)))
        else:
            p.setPen(QColor(t.text_muted))
        p.setFont(base)
        p.drawText(header, Qt.AlignRight | Qt.AlignVCenter, info)
        self._hits.append((header, m.slave_signal(self.slave, "Voltage"),
                           f"Slave {self.slave + 1}: {info}\nClick to plot the slave voltage"))
        y0 += self.HEADER_H

        gw, gh = self._group_size()
        for i in range(self.groups):
            gx = x0 + (i % self.COLS) * (gw + self.GAP)
            gy = y0 + (i // self.COLS) * (gh + self.GAP)
            p.setPen(QPen(QColor(t.group_border), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(gx + 0.5, gy + 0.5, gw - 1, gh - 1), 8, 8)
            y = gy + self.GROUP_PAD
            x = gx + self.GROUP_PAD
            if i < m.cells_per_slave:
                self._paint_cell(p, QRectF(x, y, self.TILE_W, self.CELL_H), i, tile_line, mark_line, mono_bold, small)
                y += self.CELL_H + self.INNER_GAP
            if i < m.temps_per_slave:
                self._paint_temp(p, QRectF(x, y, self.TILE_W, self.TEMP_H), i, tile_line, mark_line, mono, small)
        p.end()

    def _paint_cell(self, p, r, i, tile_line, mark_line, big, small):
        t, s, m = self.theme, self.state, self.model
        c = s.cells[i] if i < len(s.cells) else CellState()
        normal_outline = t.border if t.solid_tiles else None
        side = None
        if c.voltage is None:
            fill, ink, outline, line = QColor(t.tile), QColor(t.text_muted), normal_outline, tile_line
        else:
            past, side = self.judge_voltage(c.voltage)
            if past or c.fault or c.open_wire:
                fill, ink, _ = t.tile_style(th.BAD)
            else:
                fill = t.gradient_fill("v", self._position(c.voltage, s.v_range, MIN_SPAN_V))
                ink = QColor(t.text)
            outline, line = normal_outline, tile_line
            if c.lowest or c.highest:
                outline, line = (t.low if c.lowest else t.high), mark_line
        self._tile(p, r, fill, outline, line)

        text_bottom = r.bottom()
        if side:
            text_bottom -= self._bar(p, r, side, line)
        bad_fill = fill == t.tile_style(th.BAD)[0]
        indent = 0
        if c.voltage is not None and (c.lowest or c.highest):
            indent = self._mark(p, r, "▼" if c.lowest else "▲", ink if bad_fill else (t.low if c.lowest else t.high), small)
        p.setPen(ink)
        p.setFont(big)
        p.drawText(QRectF(r.x() + indent, r.y() + 2, r.width() - indent, r.height() * 0.5), Qt.AlignCenter,
                   "—" if c.voltage is None else f"{c.voltage:.3f}")
        lower = QRectF(r.x() + 5, r.y() + r.height() * 0.5, r.width() - 10, text_bottom - r.y() - r.height() * 0.5)
        p.setFont(small)
        if c.delta_mv is not None:
            p.setPen(ink if (t.solid_tiles or ink != QColor(t.text)) else QColor(t.text_muted))
            p.drawText(lower, Qt.AlignLeft | Qt.AlignVCenter, f"{c.delta_mv:+.0f}")
        marks = [(m_, col) for m_, on, col in (("OW", c.open_wire, t.color(th.BAD)), ("F", c.fault, t.color(th.BAD)),
                                                ("BAL", c.balancing, t.balance)) if on]
        x = lower.right()
        metrics = p.fontMetrics()
        for text, color in reversed(marks):
            w = metrics.horizontalAdvance(text)
            p.setPen(ink if bad_fill else QColor(color))
            p.drawText(QRectF(x - w, lower.y(), w, lower.height()), Qt.AlignRight | Qt.AlignVCenter, text)
            x -= w + 3

        flags = ", ".join(f for f, on in (("balancing", c.balancing), ("CELL FAULT", c.fault), ("OPEN WIRE", c.open_wire),
                                          ("lowest cell in the pack", c.lowest), ("highest cell in the pack", c.highest))
                          if on)
        near = {"low": f"near the under-voltage limit ({m.cell_uv_v:g} V)",
                "high": f"near the over-voltage limit ({m.cell_ov_v:g} V)"}.get(side, "")
        if c.voltage is not None and self.judge_voltage(c.voltage)[0]:
            near = near.replace("near", "PAST")
        tip = (f"Slave {self.slave + 1} cell {i + 1}: " +
               ("no data" if c.voltage is None else f"{c.voltage:.3f} V, {c.delta_mv:+.0f} mV from the mean") +
               "".join(f"\n{x}" for x in (near, flags) if x))
        self._hits.append((r, m.cell_voltage(self.slave, i), tip))

    def _paint_temp(self, p, r, k, tile_line, mark_line, font, small):
        t, s, m = self.theme, self.state, self.model
        v = s.temps[k] if k < len(s.temps) else None
        normal_outline = t.border if t.solid_tiles else None
        side, outline, line = None, normal_outline, tile_line
        hottest = coldest = False
        if v is None:
            fill, ink, text = QColor(t.tile), QColor(t.text_muted), "—"
        elif v >= m.temp_invalid_c - 1:
            fill, ink, _ = t.tile_style("open")
            text = "OPEN"
        else:
            past, side = self.judge_temp(v)
            if past:
                fill, ink, _ = t.tile_style(th.BAD)
            else:
                fill, ink = t.gradient_fill("t", self._position(v, s.t_range, MIN_SPAN_C)), QColor(t.text)
            text = f"{v:.1f}°"
            if s.t_extremes and s.t_extremes[1] > s.t_extremes[0]:
                hottest, coldest = v == s.t_extremes[1], v == s.t_extremes[0]
                if hottest or coldest:
                    outline, line = (t.high if hottest else t.low), mark_line
        self._tile(p, r, fill, outline, line)
        text_bottom = r.bottom()
        if side:
            text_bottom -= self._bar(p, r, side, line)
        indent = 0
        if hottest or coldest:
            bad_fill = fill == t.tile_style(th.BAD)[0]
            indent = self._mark(p, r, "▲" if hottest else "▼", ink if bad_fill else (t.high if hottest else t.low), small)
        area = QRectF(r.x() + 2 + indent, r.y(), r.width() - 4 - indent, text_bottom - r.y())
        p.setPen(ink)
        p.setFont(font)
        p.drawText(area, Qt.AlignCenter, text)

        lo, hi = self._temp_window()
        window = f"{'charge' if s.charging else 'discharge'} window {lo:g} to {hi:g} °C"
        if text == "OPEN":
            detail = "sensor open / out of range"
        elif v is None:
            detail = "no data"
        else:
            detail = f"{v:.1f} °C ({window})"
            if side:
                detail += "\n" + ("PAST" if self.judge_temp(v)[0] else "near") + \
                          (" the under-temperature limit" if side == "low" else " the over-temperature limit")
            if hottest or coldest:
                detail += "\n" + ("hottest" if hottest else "coldest") + " sensor in the pack"
        self._hits.append((r, m.cell_temp(self.slave, k), f"Slave {self.slave + 1} sensor T{k + 1}: {detail}"))

    # ---- Interaction -------------------------------------------------------------------------------

    def _hit(self, pos):
        for rect, signal, tip in self._hits:
            if rect.contains(pos):
                return signal, tip
        return None, None

    def mousePressEvent(self, event):
        signal, _ = self._hit(event.position())
        if signal and event.button() == Qt.LeftButton:
            self.clicked.emit(signal, bool(event.modifiers() & Qt.ControlModifier))

    def mouseMoveEvent(self, event):
        _, tip = self._hit(event.position())
        if tip:
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()


# ------------------------------------------------------------------------------------------------
# Cards, labels, pills
# ------------------------------------------------------------------------------------------------

class ClickLabel(QLabel):
    clicked = Signal(bool)              # Ctrl held

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(bool(event.modifiers() & Qt.ControlModifier))


class Card(QFrame):
    """Rounded card with a title and key / value rows"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(16, 12, 16, 14)
        self.outer.setSpacing(8)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            self.outer.addWidget(label)
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(5)
        self.grid.setColumnStretch(1, 1)
        self.outer.addLayout(self.grid)

    def add_row(self, key, tooltip=None, on_click=None):
        row = self.grid.rowCount()
        k = ClickLabel(key) if on_click else QLabel(key)
        k.setObjectName("muted")
        v = ClickLabel("—") if on_click else QLabel("—")
        v.setObjectName("value")
        v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        v.setWordWrap(True)
        if on_click:
            for w in (k, v):
                w.setCursor(Qt.PointingHandCursor)
                w.clicked.connect(on_click)
        if tooltip:
            k.setToolTip(tooltip)
            v.setToolTip(tooltip)
        self.grid.addWidget(k, row, 0)
        self.grid.addWidget(v, row, 1)
        return v


class Pill(QLabel):
    """Rounded status label coloured by state (theme.OK, WARN, BAD, ...)"""

    def __init__(self, text="", state=th.NEUTRAL, parent=None):
        super().__init__(text, parent)
        self.setObjectName("pill")
        self.setAlignment(Qt.AlignCenter)
        self._pill = None
        self.set(text, state)

    def set(self, text, state):
        if (text, state) != self._pill:
            self._pill = (text, state)
            self.setText(text)
            self.restyle()

    def restyle(self):
        bg, fg = th.CURRENT.pill(self._pill[1])
        self.setStyleSheet(f"background: {bg}; color: {fg};")


def set_label(label, text, state=None):
    """Only touches the label when something changed. state: theme.OK, WARN, BAD, ... or None (normal text)"""
    key = (text, state, th.CURRENT.name)
    if getattr(label, "_state", None) != key:
        label._state = key
        label.setText(text)
        label.setStyleSheet(f"color: {th.CURRENT.color(state)};" if state else "")


# ------------------------------------------------------------------------------------------------
# Live plot
# ------------------------------------------------------------------------------------------------

class LivePlot(QFrame):
    """Last WINDOW_S seconds of the signals clicked: click replaces, Ctrl+click adds"""

    WINDOW_S = 60.0
    MAX_TRACES = 8

    def __init__(self, units, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.units = units
        self.signals = []
        self.curves = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        self.title = QLabel("Click any value to plot it, Ctrl+click to add it to the plot")
        self.title.setObjectName("cardTitle")
        top.addWidget(self.title, 1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        top.addWidget(clear)
        layout.addLayout(top)
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "seconds")
        self.plot.setXRange(-self.WINDOW_S, 0, padding=0)
        self.plot.getViewBox().setMouseEnabled(x=False, y=True)
        self.legend = self.plot.addLegend(offset=(8, 8))
        layout.addWidget(self.plot)

    def set_theme(self, t):
        self.plot.setBackground(t.card)
        font = QFont(self.font())
        font.setPointSizeF(9 * t.scale)
        font.setBold(t.bold)
        for axis in ("left", "bottom"):
            ax = self.plot.getAxis(axis)
            ax.setPen(pg.mkPen(t.text_muted if t.bold else t.border, width=t.line))
            ax.setTextPen(pg.mkPen(t.text_muted))
            ax.setStyle(tickFont=font)
        self.plot.showGrid(x=True, y=True, alpha=0.45 if t.bold else 0.25)
        self.legend.setLabelTextColor(t.text)
        for _, label in self.legend.items:                       # Labels already in the legend
            label.setText(label.text, color=t.text, size=f"{9 * t.scale:.0f}pt", bold=t.bold)
        for i, signal in enumerate(self.signals):
            self.curves[signal].setPen(pg.mkPen(t.traces[i % len(t.traces)], width=t.plot_width))

    def select(self, signal, add=False):
        if not add:
            self.clear()
        if signal in self.curves or len(self.signals) >= self.MAX_TRACES:
            return
        traces = th.CURRENT.traces
        color = traces[len(self.signals) % len(traces)]
        unit = {"degC": "°C"}.get(self.units.get(signal, ""), self.units.get(signal, ""))
        self.curves[signal] = self.plot.plot([], [], pen=pg.mkPen(color, width=th.CURRENT.plot_width),
                                             name=f"{signal} [{unit}]" if unit else signal)
        self.signals.append(signal)
        self.title.setText(signal if len(self.signals) == 1 else f"{len(self.signals)} signals")

    def clear(self):
        for curve in self.curves.values():
            self.plot.removeItem(curve)
        self.legend.clear()
        self.curves.clear()
        self.signals.clear()
        self.title.setText("Click any value to plot it, Ctrl+click to add it to the plot")

    def refresh(self, store, now):
        for signal, curve in self.curves.items():
            times, values = store.series(signal)
            pts = [(t - now, float(v)) for t, v in zip(times, values) if now - t <= self.WINDOW_S]
            curve.setData([p[0] for p in pts], [p[1] for p in pts])
        if len(self.signals) == 1 and self.curves:
            times, values = store.series(self.signals[0])
            if values:
                unit = {"degC": "°C"}.get(self.units.get(self.signals[0], ""), self.units.get(self.signals[0], ""))
                self.title.setText(f"{self.signals[0]}   {float(values[-1]):.3f} {unit}".rstrip())
