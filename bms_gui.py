"""
BMS GUI: live cell voltages and temperatures, pack state, the open-circuit SoC estimate and the AMS's
runtime settings, over CAN1 (the car / datalogger bus). Windows and Linux, Qt (PySide6), light and dark.

    python bms_gui.py                                   pick the adapter in the window
    python bms_gui.py --interface slcan --channel COM5 --connect
    python bms_gui.py --demo                            simulated BMS, no hardware

Pack layout, limits, SoC model and settings come from bms_model.json and ../can/bms_can1.dbc, both
generated from Core/Inc/bms_config.h by tools/generate.py, so any pack size lays itself out.
"""

import argparse
import json
import os
import sys
import time
from collections import deque

import cantools
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter,
                               QStyledItemDelegate, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from bmslib import soc as soclib
from bmslib import theme as th
from bmslib.link import BITRATES, INTERFACES, CanLink, list_serial_ports
from bmslib.model import GUI_DIR, Model
from bmslib.settings import SettingsClient
from bmslib.store import SignalStore
from bmslib.widgets import (Card, CellState, FlowLayout, LivePlot, Pill, SlaveCard, SlaveState, judge_temp,
                            judge_voltage, set_label, shading_range)

PREFS_PATH = os.environ.get("BMS_GUI_PREFS", os.path.join(os.path.expanduser("~"), ".bms_gui.json"))
HV500_DBC = os.path.join(GUI_DIR, "hv500_can2_map_v24_EID_custom.dbc")      # Inverter frames, CAN traffic tab

STALE_S = 0.45           # Fast signals older than this show as missing
BURST_STALE_S = 0.45     # 10 Hz burst signals
POLL_MS = 30
REFRESH_MS = 200

PRECHARGE_STATES = {0: "Idle (TS off)", 1: "Precharging", 2: "Closing AIR", 3: "TS active", 4: "Precharge failed"}
FAULT_SIGNALS = ["Fault_Comms", "Fault_Voltage", "Fault_Temp", "Fault_Current", "Fault_OpenWire", "Fault_IVT",
                 "Fault_Startup"]


def unavailable(name, value):
    """The firmware's "not available" codes (README CAN table)"""
    if value is None:
        return True
    if name.startswith("Fast_") and name.endswith(("Current", "Voltage")):
        return value <= -2147483.0
    if name in ("Power", "Fast_Power"):
        return value < -81918.0
    if name in ("Energy_Remaining", "Fast_Energy_Remaining"):
        return value >= 65535
    if name == "SoC":
        return value >= 6553.0
    if name.startswith("Seg") and "RawAux" in name:return value<=-32.768
    if ("Temp" in name or "Voltage" in name) and value<=-32:return True
    return False


def fmt(v, unit, decimals):
    return "—" if v is None else f"{v:.{decimals}f} {unit}".rstrip()


# ------------------------------------------------------------------------------------------------
# CAN traffic table
# ------------------------------------------------------------------------------------------------

class TrafficModel(QAbstractTableModel):
    HEADERS = ["ID", "Message", "Hz", "Count", "Data", "Decoded"]

    def __init__(self, stats):
        super().__init__()
        self.stats = stats
        self.keys = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.keys)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if role not in (Qt.DisplayRole, Qt.UserRole):
            return None
        key = self.keys[index.row()]
        st = self.stats[key]
        col = index.column()
        if col == 0:
            return key[0] if role == Qt.UserRole else (f"0x{key[0]:08X}" if key[1] else f"0x{key[0]:03X}")
        if col == 1:
            return st["name"]
        if col == 2:
            times = st["times"]
            rate = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
            return rate if role == Qt.UserRole else f"{rate:.0f}"
        if col == 3:
            return st["count"]
        if col == 4:
            return st["data"]
        decoded = st.get("raw_decoded")
        if decoded is None:
            return ""
        return ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in decoded.items())[:500]

    def refresh(self):
        keys = sorted(self.stats)
        if keys != self.keys:
            self.beginResetModel()
            self.keys = keys
            self.endResetModel()
        elif self.keys:
            self.dataChanged.emit(self.index(0, 2), self.index(len(self.keys) - 1, len(self.HEADERS) - 1))


# ------------------------------------------------------------------------------------------------
# Main window
# ------------------------------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.model = Model(args.model) if args.model else Model()
        self.db = cantools.database.load_file(args.dbc or self.model.dbc_path, strict=True)
        self.messages = {(m.frame_id, m.is_extended_frame): m for m in self.db.messages}
        self.other_messages = {}
        if os.path.exists(HV500_DBC):
            try:
                for m in cantools.database.load_file(HV500_DBC, strict=True).messages:
                    self.other_messages.setdefault((m.frame_id, m.is_extended_frame), m)
            except Exception as exc:                              # noqa: BLE001 - the inverter DBC is optional
                print(f"Inverter DBC not loaded: {exc}")
        self.units = {s.name: s.unit or "" for m in self.db.messages for s in m.signals}

        self.store = SignalStore()
        self.link = CanLink()
        self.settings = SettingsClient(self.model, self._send_frame, self._settings_event)
        self.demo = None
        self.frame_times = deque(maxlen=5000)
        self.error_frames = 0
        self.last_error = ""
        self.id_stats = {}
        self.fields = []                    # (label, fn) refreshed every REFRESH_MS
        self.entry_synced = {}              # setting id -> raw value last put in its entry
        self.prefs = self._load_prefs()
        self.theme_mode = args.theme or self.prefs.get("theme", "system")
        self.theme = th.LIGHT

        self.setWindowTitle("BMS GUI")
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.9), int(screen.height() * 0.88))

        root = QWidget()
        root.setObjectName("screen")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_header())
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 8, 14, 10)
        body_layout.addWidget(self.tabs)
        layout.addWidget(body, 1)
        self.setCentralWidget(root)
        self.tabs.addTab(self._build_pack_tab(), "Pack")
        self.tabs.addTab(self._build_settings_tab(), "AMS settings")
        self.tabs.addTab(self._build_traffic_tab(), "CAN traffic")
        self.tabs.addTab(self._build_diagnostics_tab(),"Raw AUX / board health")
        self.status = self.statusBar()
        self.status_text = QLabel("")
        self.status.addPermanentWidget(self.status_text)
        # Drop-down items drawn by the item delegate in every style, so the stylesheet's hover colours apply
        for combo in self.findChildren(QComboBox):
            combo.setItemDelegate(QStyledItemDelegate(combo))

        self.interface_box.setCurrentText(args.interface or self.prefs.get("interface", "slcan"))
        self._interface_changed()
        if args.channel or self.prefs.get("channel"):
            self.channel_box.setCurrentText(args.channel or self.prefs.get("channel"))
        self.bitrate_box.setCurrentText(str(args.bitrate or self.prefs.get("bitrate", self.model.can1_bitrate)))
        self.set_theme(self.theme_mode)
        try:
            QGuiApplication.styleHints().colorSchemeChanged.connect(lambda *_: self.set_theme(self.theme_mode))
        except AttributeError:
            pass

        if args.demo:
            from bmslib.demo import DemoBms
            self.demo = DemoBms(self.model, self.db).start()
            self.interface_box.setCurrentText("virtual")
            self._interface_changed()
            self.channel_box.setCurrentText("bms-demo")
        if args.connect or args.demo:
            QTimer.singleShot(100, self.connect_bus)

        self.poll_timer = QTimer(self, interval=POLL_MS, timeout=self._poll)
        self.poll_timer.start()
        self.refresh_timer = QTimer(self, interval=REFRESH_MS, timeout=self._refresh)
        self.refresh_timer.start()

    # ---- Header and connection ---------------------------------------------------------------------

    def _build_header(self):
        bar = QFrame()
        bar.setObjectName("header")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(8)
        title = QLabel("BMS")
        title.setObjectName("big")
        h.addWidget(title)
        self.bms_pill = Pill("Disconnected", th.NEUTRAL)
        h.addWidget(self.bms_pill)
        h.addSpacing(16)

        self.interface_box = QComboBox()
        for name, desc, _ in INTERFACES:
            self.interface_box.addItem(name)
            self.interface_box.setItemData(self.interface_box.count() - 1, desc, Qt.ToolTipRole)
        self.interface_box.currentTextChanged.connect(lambda _: self._interface_changed())
        self.channel_box = QComboBox()
        self.channel_box.setEditable(True)
        self.channel_box.setMinimumWidth(260)
        ports = QPushButton("Ports")
        ports.setToolTip("Refresh the list of serial ports")
        ports.clicked.connect(self._refresh_ports)
        self.bitrate_box = QComboBox()
        self.bitrate_box.setEditable(True)
        self.bitrate_box.addItems([str(b) for b in BITRATES])
        self.log_check = QCheckBox("Log")
        self.log_check.setChecked(True)
        self.log_check.setToolTip("Log every frame to GUI/logs (candump format)")
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("primary")
        self.connect_button.setMinimumWidth(110)
        self.connect_button.clicked.connect(self.toggle_connection)
        for label, widget in (("Interface", self.interface_box), ("Channel", self.channel_box), (None, ports),
                              ("Bit rate", self.bitrate_box), (None, self.log_check), (None, self.connect_button)):
            if label:
                muted = QLabel(label)
                muted.setObjectName("muted")
                h.addWidget(muted)
            h.addWidget(widget)
        h.addStretch(1)
        muted = QLabel("Theme")
        muted.setObjectName("muted")
        h.addWidget(muted)
        self.theme_box = QComboBox()
        self.theme_box.addItems(["System", "Light", "Dark", "Daylight"])
        self.theme_box.setItemData(3, "High contrast for bright sunlight: black on white, bigger bold type, "
                                      "solid colour only where something needs attention", Qt.ToolTipRole)
        self.theme_box.currentTextChanged.connect(lambda text: self.set_theme(text.lower(), save=True))
        h.addWidget(self.theme_box)
        return bar

    def _interface_changed(self):
        name = self.interface_box.currentText()
        info = {i[0]: i for i in INTERFACES}.get(name)
        self.channel_box.clear()
        if name == "slcan":
            self._refresh_ports()
        elif info:
            self.channel_box.addItem(info[2])

    def _refresh_ports(self):
        if self.interface_box.currentText() != "slcan":
            return
        current = self.channel_box.currentText().split("  (")[0].strip()
        self.channel_box.clear()
        ports = list_serial_ports()
        for dev, desc in ports:
            self.channel_box.addItem(f"{dev}  ({desc})")
        devices = [p[0] for p in ports]
        if current in devices:
            self.channel_box.setCurrentIndex(devices.index(current))
        else:
            # A USB adapter rather than a Bluetooth serial link
            usb = [i for i, (_, desc) in enumerate(ports) if "bluetooth" not in desc.lower()]
            if usb:
                self.channel_box.setCurrentIndex(usb[0])

    def toggle_connection(self):
        if self.link.connected:
            self.disconnect_bus()
        else:
            self.connect_bus()

    def _set_connect_button(self, connected):
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.connect_button.setObjectName("danger" if connected else "primary")
        self.connect_button.style().unpolish(self.connect_button)
        self.connect_button.style().polish(self.connect_button)

    def connect_bus(self):
        interface = self.interface_box.currentText()
        channel = self.channel_box.currentText().split("  (")[0].strip()
        try:
            bitrate = int(self.bitrate_box.currentText())
        except ValueError:
            QMessageBox.warning(self, "BMS GUI", f"Bit rate '{self.bitrate_box.currentText()}' is not a number")
            return
        self.bms_pill.set("Connecting...", th.CAUTION)
        QApplication.processEvents()
        try:
            self.link.connect(interface, channel, bitrate, log=self.log_check.isChecked())
        except Exception as exc:                                  # noqa: BLE001 - shown to the user
            hint = ""
            if interface == "slcan":
                hint = ("\n\nCheck the adapter is plugged in, pick its COM port (Ports refreshes the list) and close "
                        "any other program using the port.")
                if "serial" in str(exc).lower() and "module" in str(exc).lower():
                    hint = "\n\nThe slcan interface needs pyserial: pip install -r requirements.txt"
            QMessageBox.critical(self, "BMS GUI", f"Could not connect to {interface} {channel}:\n{exc}{hint}")
            self.bms_pill.set("Disconnected", th.NEUTRAL)
            return
        self._set_connect_button(True)
        if not self.args.demo:                                   # Remember real adapters only
            self._save_prefs(interface=interface, channel=channel, bitrate=bitrate)
        self.store.clear()
        self.settings.cancel()
        self.settings.read_all()

    def disconnect_bus(self):
        self.settings.cancel()
        self.link.disconnect()
        self._set_connect_button(False)
        self.bms_pill.set("Disconnected", th.NEUTRAL)

    def _send_frame(self, arbitration_id, data):
        if self.link.connected:
            try:
                self.link.send(arbitration_id, data)
            except Exception as exc:                              # noqa: BLE001
                self._settings_event("error", f"Could not send: {exc}")

    def _load_prefs(self):
        try:
            with open(PREFS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_prefs(self, **prefs):
        self.prefs.update(prefs)
        try:
            with open(PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f)
        except OSError:
            pass

    # ---- Theme ---------------------------------------------------------------------------------------

    def set_theme(self, mode, save=False):
        self.theme_mode = mode
        self.theme_box.blockSignals(True)
        self.theme_box.setCurrentText(mode.capitalize())
        self.theme_box.blockSignals(False)
        self.theme = th.resolve(mode)
        th.apply(QApplication.instance(), self.theme)
        for card in self.slave_cards:
            card.set_theme(self.theme)
        self.plot.set_theme(self.theme)
        for pill in self.findChildren(Pill):
            pill.restyle()
        for item in (self.settings_log.item(i) for i in range(self.settings_log.count())):
            item.setForeground(QColor(self.theme.color(item.data(Qt.UserRole) or th.OK)))
        self.flow.invalidate()                                    # Card sizes follow the theme's scale
        self.side_area.setFixedWidth(int(420 * self.theme.scale))
        if save:
            self._save_prefs(theme=mode)
        self._refresh()

    # ---- Pack tab --------------------------------------------------------------------------------------

    def _build_pack_tab(self):
        m = self.model
        tab = QWidget()
        h = QHBoxLayout(tab)
        h.setContentsMargins(0, 8, 0, 0)
        h.setSpacing(14)

        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        cards_area = QScrollArea()
        cards_area.setWidgetResizable(True)
        cards_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 6, 0)
        legend = QLabel(f"{m.slaves} slaves × {m.cells_per_slave} cells, {m.temps_per_slave} sensors each; every box is one "
                        "cell: voltage (mV from the mean) above its temperature.   Shade: stronger = higher in the pack.   "
                        "Outline ▼ ▲: pack lowest / highest.   Bar: near the under- (blue) or over-limit (red).   "
                        "Red: past a limit.   BAL: balancing.   Click to plot, Ctrl+click to add.")
        legend.setObjectName("muted")
        legend.setWordWrap(True)
        outer.addWidget(legend)
        flow_widget = QWidget()
        self.flow = FlowLayout(flow_widget, spacing=12)
        self.slave_cards = []
        for s in range(m.slaves):
            card = SlaveCard(s, m)
            card.clicked.connect(self.plot_signal)
            self.flow.addWidget(card)
            self.slave_cards.append(card)
        outer.addWidget(flow_widget)
        outer.addStretch(1)
        cards_area.setWidget(holder)
        self.cards_area = cards_area
        split.addWidget(cards_area)

        self.plot = LivePlot(self.units)
        split.addWidget(self.plot)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        split.setSizes([700, 260])
        h.addWidget(split, 1)

        side_area = QScrollArea()
        side_area.setWidgetResizable(True)
        side_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.side_area = side_area
        side = QWidget()
        v = QVBoxLayout(side)
        v.setContentsMargins(0, 0, 8, 0)
        v.setSpacing(12)
        self._build_soc_card(v)
        self._build_info_cards(v)
        v.addStretch(1)
        side_area.setWidget(side)
        h.addWidget(side_area)
        return tab

    def _field(self, card, key, fn, signal=None, tooltip=None):
        label = card.add_row(key, tooltip, (lambda add, s=signal: self.plot_signal(s, add)) if signal else None)
        self.fields.append((label, fn))
        return label

    def _build_soc_card(self, v):
        card = Card("State of charge")
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)
        for col, title, tip in ((0, "BMS", "The BMS's live SoC: its start-up estimate, then the IVT energy counter"),
                                (1, "At rest", "The same open-circuit estimate the BMS takes at start-up (Battery "
                                               "Tracker VToSoc model on the mean cell voltage). Only valid with no "
                                               "current flowing: under load the cells sag and it reads low.")):
            label = QLabel(title)
            label.setObjectName("muted")
            label.setToolTip(tip)
            grid.addWidget(label, 0, col)
        self.soc_bms = QLabel("—")
        self.soc_bms.setObjectName("big")
        self.soc_rest = QLabel("—")
        self.soc_rest.setObjectName("big")
        grid.addWidget(self.soc_bms, 1, 0)
        grid.addWidget(self.soc_rest, 1, 1)
        self.soc_bms_bar = QProgressBar()
        self.soc_rest_bar = QProgressBar()
        for col, bar in ((0, self.soc_bms_bar), (1, self.soc_rest_bar)):
            bar.setRange(0, 1000)
            bar.setTextVisible(False)
            bar.setFixedHeight(10)
            grid.addWidget(bar, 2, col)
        self.soc_bms_wh = QLabel("—")
        self.soc_bms_wh.setObjectName("muted")
        self.soc_rest_wh = QLabel("—")
        self.soc_rest_wh.setObjectName("muted")
        grid.addWidget(self.soc_bms_wh, 3, 0)
        grid.addWidget(self.soc_rest_wh, 3, 1)
        card.outer.addLayout(grid)
        row = QHBoxLayout()
        self.rest_pill = Pill("—", th.NEUTRAL)
        row.addWidget(self.rest_pill)
        row.addStretch(1)
        card.outer.addLayout(row)
        self.rest_detail = QLabel("—")
        self.rest_detail.setObjectName("muted")
        self.rest_detail.setWordWrap(True)
        card.outer.addWidget(self.rest_detail)
        v.addWidget(card)

    def _build_info_cards(self, v):
        val = self.value
        pack = Card("Pack")
        self._field(pack, "Pack voltage (IVT U1)", lambda: fmt(val("Fast_Pack_Voltage", "Pack_Voltage_IVT"), "V", 2),
                    "Fast_Pack_Voltage")
        self._field(pack, "Vehicle side (IVT U2)", lambda: fmt(val("Fast_Vehicle_Voltage"), "V", 2), "Fast_Vehicle_Voltage")
        self._field(pack, "Current (+ discharge)", lambda: fmt(val("Fast_Pack_Current", "Pack_Current"), "A", 2),
                    "Fast_Pack_Current")
        self._field(pack, "Power", lambda: fmt(self._kw(val("Fast_Power", "Power")), "kW", 2), "Fast_Power")
        self._field(pack, "Sum of cells", lambda: fmt(val("Pack_Voltage_SumOfCells"), "V", 1), "Pack_Voltage_SumOfCells")
        self._field(pack, "Cell min / max", self._cell_range_text, "Cell_Voltage_Min")
        self._field(pack, "Temperature min / max", lambda: self._pair(val("Cell_Temp_Min"), val("Cell_Temp_Max"), "°C", 1),
                    "Cell_Temp_Max")
        v.addWidget(pack)

        status = Card("Status")
        self._field(status, "AMS fault output",
                    lambda: self._flag("Fault_Output_Active", ("ACTIVE", th.BAD), ("off", th.OK)))
        self._field(status, "Faults", self._faults_text)
        self._field(status, "Precharge / TS", self._precharge_text, "Precharge_Percent")
        self._field(status, "SDC / TSAC ready", lambda: self._pair_flags("SDC_Closed", "closed", "open",
                                                                          "TSAC_Ready", "ready", "not ready"))
        self._field(status, "Charging / balancing", lambda: self._pair_flags("Charging", "charging", "not charging",
                                                                             "Balancing_Enabled", "balancing on",
                                                                             "balancing off"))
        self._field(status, "Measurement age / cycle",
                    lambda: self._pair(val("Measurement_Age"), val("Measurement_Cycle_Time"), "ms", 0))
        self._field(status, "Build", lambda: (("BENCH OVERRIDES", th.BAD) if val("Local_Config_Overrides")
                                              else "car configuration" if val("Local_Config_Overrides") is not None
                                              else "—"))
        v.addWidget(status)

        limits = Card("Inverter limits (live)")
        self._field(limits, "Discharge power limit", lambda: fmt(self._kw(val("Discharge_Power_Limit")), "kW", 1),
                    "Discharge_Power_Limit")
        self._field(limits, "Discharge current limit", lambda: fmt(val("Discharge_Current_Limit"), "A", 1),
                    "Discharge_Current_Limit")
        self._field(limits, "Regen current limit", lambda: fmt(val("Regen_Current_Limit"), "A", 1), "Regen_Current_Limit")
        self._field(limits, "500 ms average power", lambda: fmt(self._kw(val("Window_Avg_Power")), "kW", 1),
                    "Window_Avg_Power")
        self._field(limits, "Limited by", self._limit_flags_text)
        v.addWidget(limits)
        health=Card("Acquisition / protocol")
        for name in ["BMS_ProtocolVersion","BMS_BoardProfile","BMS_CellsValid","BMS_AuxValid","BMS_CelsiusValid","BMS_IVTConfigured","IVT_CounterGaps","IVT_RestartEpoch","Charger_State","Charger_FaultMask","Charger_Healthy"]:
            self._field(health,name.replace('_',' '),lambda n=name: str(val(n)) if val(n) is not None else "—",name)
        self._field(health,"U1 − summed cells",lambda: fmt(None if val("Fast_Pack_Voltage") is None or val("Pack_Voltage_SumOfCells") is None else val("Fast_Pack_Voltage")-val("Pack_Voltage_SumOfCells"),"V",2))
        v.addWidget(health)

    # ---- Settings tab ----------------------------------------------------------------------------------

    def _build_settings_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(12)
        card = Card("Runtime settings")
        intro = QLabel("Settings in the BMS. A write takes effect at once; Save stores them in the BMS's flash so "
                       "they load at every start-up (refused while the TS is active or charging). The inverter limits "
                       "can only be lowered below the values built into the firmware (bms_config.h); the AMS trip "
                       "limits are not settings and never change.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        card.outer.insertWidget(1, intro)
        grid = card.grid
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 0)
        grid.setColumnStretch(6, 1)
        for col, text in enumerate(["Setting", "In the BMS", "New value", "Range", "Built-in", "", ""]):
            label = QLabel(text)
            label.setObjectName("muted")
            grid.addWidget(label, 0, col)
        self.setting_widgets = {}
        for i, s in enumerate(self.model.settings):
            r = i + 1
            name = QLabel(s.label)
            name.setToolTip(s.description)
            grid.addWidget(name, r, 0)
            current = QLabel("—")
            current.setObjectName("value")
            current.setMinimumWidth(110)
            grid.addWidget(current, r, 1)
            if s.is_switch:
                entry = QComboBox()
                entry.addItems(["On", "Off"])
                entry.currentTextChanged.connect(lambda _, s=s: self._mark_changed(s))
            else:
                entry = QDoubleSpinBox()
                entry.setDecimals(s.decimals)
                entry.setRange(s.to_display(s.min), s.to_display(s.max))
                entry.setSingleStep(1.0)
                entry.setSuffix(f" {s.display_unit}" if s.unit else "")
                entry.setKeyboardTracking(False)
                entry.setMinimumWidth(130)
                entry.valueChanged.connect(lambda _, s=s: self._mark_changed(s))
            grid.addWidget(entry, r, 2)
            grid.addWidget(QLabel(s.range_text()), r, 3)
            grid.addWidget(QLabel(s.format(s.default)), r, 4)
            button = QPushButton("Write")
            button.clicked.connect(lambda _, s=s: self.write_setting(s))
            grid.addWidget(button, r, 5)
            desc = QLabel(s.description)
            desc.setObjectName("muted")
            desc.setWordWrap(True)
            desc.setMinimumWidth(320)
            grid.addWidget(desc, r, 6)
            self.setting_widgets[s.id] = (current, entry, button)

        buttons = QHBoxLayout()
        for text, slot, name in (("Read from BMS", self.read_settings, None),
                                 ("Write all changes", self.write_all_settings, "primary"),
                                 ("Save to BMS flash", self.save_settings, None),
                                 ("Restore built-in values", self.restore_settings, None)):
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.settings_pill = Pill("Not read yet", th.NEUTRAL)
        buttons.addWidget(self.settings_pill)
        card.outer.addSpacing(6)
        card.outer.addLayout(buttons)
        self.settings_state = QLabel("Connect, then Read from BMS")
        self.settings_state.setObjectName("muted")
        self.settings_state.setWordWrap(True)
        card.outer.addWidget(self.settings_state)
        v.addWidget(card)

        log_card = Card("Messages")
        self.settings_log = QListWidget()
        self.settings_log.setMinimumHeight(140)
        log_card.outer.addWidget(self.settings_log)
        v.addWidget(log_card, 1)
        return tab

    def _entry_raw(self, s):
        entry = self.setting_widgets[s.id][1]
        if s.is_switch:
            return 1 if entry.currentText() == "On" else 0
        return s.from_display(entry.value())

    def _set_entry(self, s, raw):
        entry = self.setting_widgets[s.id][1]
        entry.blockSignals(True)
        if s.is_switch:
            entry.setCurrentText("On" if raw else "Off")
        else:
            entry.setValue(s.to_display(raw))
        entry.blockSignals(False)
        self._mark_changed(s)

    def _mark_changed(self, s):
        entry = self.setting_widgets[s.id][1]
        bms = self.settings.values.get(s.id)
        changed = bms is not None and self._entry_raw(s) != bms
        if bool(entry.property("changed")) != changed:
            entry.setProperty("changed", changed)
            entry.style().unpolish(entry)
            entry.style().polish(entry)

    def read_settings(self):
        if self._require_connection():
            self.settings.read_all()

    def write_setting(self, s):
        if not self._require_connection():
            return
        raw = self._entry_raw(s)
        if not s.in_range(raw):
            QMessageBox.warning(self, "AMS settings", f"{s.label}: outside {s.range_text()}")
            return
        self.settings.write(s.id, raw)

    def write_all_settings(self):
        if not self._require_connection():
            return
        changes = [(s, self._entry_raw(s)) for s in self.model.settings
                   if self._entry_raw(s) != self.settings.values.get(s.id)]
        if not changes:
            self._settings_event("ok", "Nothing to write: the BMS already has these values")
        for s, raw in changes:
            self.settings.write(s.id, raw)

    def save_settings(self):
        if self._require_connection() and QMessageBox.question(
                self, "AMS settings", "Store the settings now in force in the BMS flash? They will load at every "
                                      "start-up.") == QMessageBox.Yes:
            self.settings.save()

    def restore_settings(self):
        if self._require_connection() and QMessageBox.question(
                self, "AMS settings", "Put every setting back to the value built into the firmware? This takes effect "
                                      "at once; Save to keep it after a restart.") == QMessageBox.Yes:
            self.entry_synced.clear()
            self.settings.restore_defaults()

    def _require_connection(self):
        if not self.link.connected:
            QMessageBox.information(self, "AMS settings", "Connect to CAN1 first")
            return False
        return True

    def _settings_event(self, kind, text):
        item = QListWidgetItem(f"{time.strftime('%H:%M:%S')}   {text}")
        item.setData(Qt.UserRole, th.BAD if kind == "error" else th.OK)
        item.setForeground(QColor(th.CURRENT.color(th.BAD if kind == "error" else th.OK)))
        self.settings_log.addItem(item)
        self.settings_log.scrollToBottom()
        self.status.showMessage(text, 6000)

    def _refresh_settings(self):
        for s in self.model.settings:
            current, entry, _ = self.setting_widgets[s.id]
            raw = self.settings.values.get(s.id)
            set_label(current, s.format(raw), th.WARN if raw is not None and raw != s.default else None)
            # Fill the entry with the BMS's value unless the user has changed it since
            if raw is not None and self.entry_synced.get(s.id) != raw:
                previous = self.entry_synced.get(s.id)
                if previous is None or self._entry_raw(s) == previous:
                    self._set_entry(s, raw)
                self.entry_synced[s.id] = raw
            self._mark_changed(s)
        c = self.settings
        if c.flags is None:
            self.settings_pill.set("Not read yet", th.NEUTRAL)
            text = "Connect, then Read from BMS"
        else:
            if c.unsaved:
                self.settings_pill.set("Unsaved changes", th.WARN)
            elif c.modified:
                self.settings_pill.set("Saved, limits changed", th.INFO)
            else:
                self.settings_pill.set("Built-in values", th.OK)
            text = ("Flash holds saved settings." if c.stored else
                    "Nothing saved in flash: the built-in values load at start-up.")
            if c.unsaved:
                text += " The settings in force differ from flash and are lost at the next restart unless saved."
        if c.busy():
            text += "   Waiting for the BMS..."
        set_label(self.settings_state, text)

    # ---- CAN traffic tab -------------------------------------------------------------------------------

    def _build_traffic_tab(self):
        tab = QWidget()
        v = QVBoxLayout(tab)
        v.setContentsMargins(0, 8, 0, 0)
        self.traffic_filter = QLineEdit()
        self.traffic_filter.setPlaceholderText("Filter by ID, message or signal")
        v.addWidget(self.traffic_filter)
        self.traffic_model = TrafficModel(self.id_stats)
        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(self.traffic_model)
        proxy.setFilterKeyColumn(-1)
        proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        proxy.setSortRole(Qt.UserRole)
        self.traffic_filter.textChanged.connect(proxy.setFilterFixedString)
        table = QTableView()
        table.setModel(proxy)
        table.setSortingEnabled(True)
        table.sortByColumn(0, Qt.AscendingOrder)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        header = table.horizontalHeader()
        for col, width in enumerate((110, 200, 50, 70, 190)):
            table.setColumnWidth(col, width)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        v.addWidget(table)
        return tab

    def _build_diagnostics_tab(self):
        self.diagnostics=QTableWidget(self.model.slaves*self.model.temps_per_slave,8)
        self.diagnostics.setHorizontalHeaderLabels(["Board / signal","Raw AUX V","Raw valid","Celsius","Celsius valid","PEC count","Counter count","Transport / stop"])
        self.diagnostics.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.diagnostics.horizontalHeader().setStretchLastSection(True)
        return self.diagnostics

    def _refresh_diagnostics(self):
        for board in range(self.model.slaves):
            prefix=self.model.seg(board)
            rawmask=self.value(prefix+"_RawAux_ValidMask")
            cmask=self.value(prefix+"_Celsius_ValidMask")
            for sensor in range(self.model.temps_per_slave):
                row=board*self.model.temps_per_slave+sensor
                values=[f"{board+1} / TEMP{sensor+1}",fmt(self.value(prefix+f"_RawAux{sensor+1:02d}"),"V",4),
                        "—" if rawmask is None else str(bool(int(rawmask)&(1<<sensor))),
                        fmt(self.value(self.model.cell_temp(board,sensor)),"°C",1),
                        "—" if cmask is None else str(bool(int(cmask)&(1<<sensor))),
                        str(self.value(prefix+"_PEC_Count")),str(self.value(prefix+"_Counter_Count")),
                        f"{self.value(prefix+'_Transport_Error')} / {self.value(prefix+'_BalanceStop_Failed')}"]
                for col,value in enumerate(values):self.diagnostics.setItem(row,col,QTableWidgetItem(value))

    # ---- Receiving ---------------------------------------------------------------------------------------

    def _poll(self):
        q = self.link.queue
        for _ in range(20000):
            try:
                msg = q.get_nowait()
            except Exception:                                     # noqa: BLE001 - queue.Empty
                break
            if isinstance(msg, Exception):
                self.last_error = str(msg)
                continue
            self._handle(msg)
        self.settings.poll()

    def _handle(self, msg):
        if msg.is_remote_frame or msg.is_fd:return
        if msg.is_error_frame:
            self.error_frames += 1
            return
        t = msg.timestamp or time.time()
        self.frame_times.append(t)
        key = (msg.arbitration_id, msg.is_extended_id)
        data = bytes(msg.data)
        if msg.is_extended_id and msg.arbitration_id == self.model.config_response_id:
            if len(data)==8:self.settings.handle(data)
        message = self.messages.get(key) or self.other_messages.get(key)
        decoded = None
        if message is not None:
            try:
                if len(data)!=message.length:return
                decoded = message.decode(data, decode_choices=False)
            except Exception:                                     # noqa: BLE001 - short or odd frames
                decoded = None
            if decoded is not None and key in self.messages:
                self.store.update(t, decoded)
                if message.name=="BMS_Identity":self.settings.observe_identity(decoded["BMS_ProtocolVersion"],decoded["BMS_ConfigVersion"])
        st = self.id_stats.get(key)
        if st is None:
            st = self.id_stats[key] = {"count": 0, "times": deque(maxlen=50), "name": message.name if message else ""}
        st["count"] += 1
        st["times"].append(t)
        st["data"] = data.hex(" ").upper()
        st["raw_decoded"] = decoded

    # ---- Values ------------------------------------------------------------------------------------------

    def value(self, *names):
        """Latest fresh value of the first available signal"""
        now = time.time()
        for name in names:
            v = self.store.get(name, STALE_S if name.startswith("Fast_") else BURST_STALE_S, now)
            if v is not None and not unavailable(name, v):
                return v
        return None

    @staticmethod
    def _kw(w):
        return None if w is None else w / 1000.0

    @staticmethod
    def _pair(a, b, unit, decimals):
        if a is None and b is None:
            return "—"
        fa = "—" if a is None else f"{a:.{decimals}f}"
        fb = "—" if b is None else f"{b:.{decimals}f}"
        return f"{fa} / {fb} {unit}"

    def _flag(self, name, on, off):
        v = self.value(name)
        return "—" if v is None else (on if v else off)

    def _pair_flags(self, a, a_on, a_off, b, b_on, b_off):
        va, vb = self.value(a), self.value(b)
        if va is None and vb is None:
            return "—"
        return f"{'—' if va is None else (a_on if va else a_off)}, {'—' if vb is None else (b_on if vb else b_off)}"

    def _cell_range_text(self):
        lo, hi = self.value("Cell_Voltage_Min"), self.value("Cell_Voltage_Max")
        if lo is None or hi is None:
            return "—"
        return f"{lo:.3f} / {hi:.3f} V  (Δ {(hi - lo) * 1000:.0f} mV)"

    def _faults_text(self):
        mask=self.value("BMS_FaultMask")
        if mask is not None:
            names=[name for bit,name in self.model.faults.items() if int(mask)&bit]
            return (", ".join(names),th.BAD) if names else ("none",th.OK)
        seen = [self.value(f) for f in FAULT_SIGNALS]
        if all(x is None for x in seen):
            return "—"
        active = [f.replace("Fault_", "") for f, x in zip(FAULT_SIGNALS, seen) if x]
        return (", ".join(active), th.BAD) if active else ("none", th.OK)

    def _precharge_text(self):
        state = self.value("Precharge_State")
        if state is None:
            return "—"
        pct = self.value("Precharge_Percent")
        text = PRECHARGE_STATES.get(int(state), f"state {int(state)}")
        return text + (f" ({pct:.0f} %)" if pct is not None and int(state) in (1, 2) else "")

    def _limit_flags_text(self):
        names = [("Limit_AMS_Fault", "AMS fault"), ("Limit_Temp_Derating", "temperature"), ("Limit_Power", "power"),
                 ("Limit_Regen_Temp_Derating", "regen temperature"), ("Limit_Boost", "window boost"),
                 ("Limit_Settings_Modified", "settings")]
        seen = [(label, self.value(n)) for n, label in names]
        if all(v is None for _, v in seen):
            return "—"
        active = [label for label, v in seen if v]
        return (", ".join(active), th.WARN) if active else "current limit only"

    def rest_estimate(self, now):
        m = self.model
        voltages = [self.store.get(m.cell_voltage(s, c), BURST_STALE_S, now)
                    for s in range(m.slaves) for c in range(m.cells_per_slave)]
        current = self.value("Fast_Pack_Current", "Pack_Current")
        state = self.value("Precharge_State")
        ts_active = None if state is None else int(state) in (1, 2, 3)
        return soclib.estimate(voltages, current, m.soc, ts_active)

    # ---- Refreshing --------------------------------------------------------------------------------------

    def _refresh(self):
        now = time.time()
        if self.tabs.currentIndex() == 0:
            self._refresh_cards(now)
            self._refresh_soc(now)
            for label, fn in self.fields:
                out = fn()
                text, color = out if isinstance(out, tuple) else (out, None)
                set_label(label, text, color)
            self.plot.refresh(self.store, now)
        elif self.tabs.currentIndex() == 2:
            self.traffic_model.refresh()
        elif self.tabs.currentIndex()==3:
            self._refresh_diagnostics()
        self._refresh_settings()
        self._refresh_status(now)

    def _refresh_cards(self, now):
        m = self.model
        def get(name):
            value=self.value(name)
            if name.startswith("Seg") and "_Cell" in name and name.endswith("_Voltage") and self.value("BMS_CellsValid")!=1:return None
            if name.startswith("Seg") and "_Temp" in name and self.value("BMS_CelsiusValid")!=1:return None
            return value
        voltages = [[get(m.cell_voltage(s, c)) for c in range(m.cells_per_slave)] for s in range(m.slaves)]
        known = [v for row in voltages for v in row if v is not None]
        mean = sum(known) / len(known) if known else None
        lo, hi = (min(known), max(known)) if known else (None, None)
        temps = [[get(m.cell_temp(s, k)) for k in range(m.temps_per_slave)] for s in range(m.slaves)]
        real_temps = [x for row in temps for x in row if x is not None and x < m.temp_invalid_c - 1]
        charging = bool(self.value("Charging"))
        v_range = shading_range(known, lambda v: judge_voltage(m, v))
        t_range = shading_range(real_temps, lambda x: judge_temp(m, x, charging))
        t_extremes = (min(real_temps), max(real_temps)) if real_temps else None
        for s, card in enumerate(self.slave_cards):
            cells = []
            for c in range(m.cells_per_slave):
                v = voltages[s][c]
                cells.append(CellState(
                    voltage=v, delta_mv=None if v is None or mean is None else (v - mean) * 1000.0,
                    balancing=bool(get(m.cell_flag(s, c, "Balancing"))), fault=bool(get(m.cell_flag(s, c, "Fault"))),
                    open_wire=bool(get(m.cell_flag(s, c, "OpenWire"))),
                    lowest=v is not None and v == lo and hi != lo, highest=v is not None and v == hi and hi != lo))
            card.set_state(SlaveState(
                voltage=get(m.slave_signal(s, "Voltage")), die_temp=get(m.slave_signal(s, "Die_Temp")),
                comms_error=bool(get(m.slave_signal(s, "Comms_Error"))), fault=bool(get(m.slave_signal(s, "Fault"))),
                cells=tuple(cells), temps=tuple(temps[s]), v_range=v_range, t_range=t_range, t_extremes=t_extremes,
                charging=charging))

    def _refresh_soc(self, now):
        soc, wh = self.value("SoC"), self.value("Fast_Energy_Remaining", "Energy_Remaining")
        if soc is None:
            set_label(self.soc_bms, "—")
            set_label(self.soc_bms_wh, "not estimated yet" if self.store.get("SoC") is not None else "—")
            self.soc_bms_bar.setValue(0)
        else:
            set_label(self.soc_bms, f"{soc:.1f} %")
            set_label(self.soc_bms_wh, "—" if wh is None else f"{wh:.0f} Wh remaining")
            self.soc_bms_bar.setValue(int(soc * 10))

        e = self.rest_estimate(now)
        limit = self.model.soc["no_load_current_a"]
        if e.soc_percent is None:
            set_label(self.soc_rest, "—")
            set_label(self.soc_rest_wh, "—")
            self.soc_rest_bar.setValue(0)
        else:
            set_label(self.soc_rest, f"{e.soc_percent:.1f} %", None if e.at_rest else th.WARN)
            set_label(self.soc_rest_wh, f"{e.energy_wh:.0f} Wh")
            self.soc_rest_bar.setValue(int(e.soc_percent * 10))
        if e.at_rest is None:
            self.rest_pill.set("No current reading", th.NEUTRAL)
        elif e.at_rest:
            self.rest_pill.set("At rest" + (f"  {e.current_a:+.1f} A" if e.current_a is not None else "  TS off"), th.OK)
        else:
            self.rest_pill.set(f"Under load  {e.current_a:+.0f} A", th.WARN)
        if e.mean_v is None:
            detail = "No cell voltages yet" if not e.cells_seen else f"No cells inside the model's range ({e.cells_seen} seen)"
        else:
            detail = (f"Mean of {e.cells_used} / {self.model.total_cells} cells: {e.mean_v:.4f} V. "
                      f"Valid only below {limit:g} A or with the TS off.")
        set_label(self.rest_detail, detail)

    def _refresh_status(self, now):
        recent = sum(1 for t in self.frame_times if now - t <= 1.0)
        if self.link.connected:
            bms_t = self.store.time("Fast_Pack_Current") or self.store.time("Cell_Voltage_Min")
            if bms_t is None:
                self.bms_pill.set("No BMS frames", th.CAUTION)
            elif now - bms_t > STALE_S:
                self.bms_pill.set(f"BMS silent {now - bms_t:.0f} s", th.BAD)
            elif self.value("Fault_Output_Active"):
                self.bms_pill.set("AMS FAULT", th.BAD)
            else:
                self.bms_pill.set("BMS OK", th.OK)
        parts = [self.link.description or "Disconnected", f"{recent} frames/s"]
        if self.error_frames:
            parts.append(f"{self.error_frames} error frames")
        if self.link.log_file is not None:
            parts.append(f"logging to {os.path.relpath(self.link.log_path, GUI_DIR)}")
        if self.last_error:
            parts.append(self.last_error[:80])
        set_label(self.status_text, "   ·   ".join(parts))

    def plot_signal(self, signal, add=False):
        self.plot.select(signal, add)
        self.plot.refresh(self.store, time.time())

    # ---- Shutdown ----------------------------------------------------------------------------------------

    def closeEvent(self, event):
        self.poll_timer.stop()
        self.refresh_timer.stop()
        self.link.disconnect()
        if self.demo:
            self.demo.stop()
            self.demo = None
        super().closeEvent(event)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="BMS GUI: live pack data and AMS settings over CAN1")
    p.add_argument("--interface", help="python-can interface: " + ", ".join(i[0] for i in INTERFACES))
    p.add_argument("--channel", help="e.g. COM5 (slcan on Windows), /dev/ttyACM0, PCAN_USBBUS1, can0")
    p.add_argument("--bitrate", type=int, help="CAN1 bit rate (default from the firmware configuration)")
    p.add_argument("--connect", action="store_true", help="connect at start-up")
    p.add_argument("--demo", action="store_true", help="simulated BMS on a virtual bus, no hardware")
    p.add_argument("--theme", choices=["system", "light", "dark", "daylight"],
                   help="colour theme (default: last used, or system); daylight = high contrast for sunlight")
    p.add_argument("--model", help="bms_model.json to use (default: the generated one next to this script)")
    p.add_argument("--dbc", help="CAN1 DBC to use (default: ../can/bms_can1.dbc)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("BMS GUI")
    window = MainWindow(args)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
