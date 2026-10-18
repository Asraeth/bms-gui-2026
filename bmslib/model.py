"""
The pack layout, limits, SoC model and runtime settings of the firmware this GUI talks to.

They come from GUI/bms_model.json, which tools/generate.py writes from Core/Inc/bms_config.h on every
firmware build, test run and commit, so the GUI can't drift from the firmware. CAN signals are decoded
with the generated can/bms_can1.dbc.
"""

import json
import os
from dataclasses import dataclass

GUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MODEL = os.path.join(GUI_DIR, "bms_model.json")


@dataclass(frozen=True)
class Setting:
    """A runtime setting. Values on CAN are integers in raw units (value = raw x scale)."""
    id: int
    name: str
    label: str
    unit: str
    scale: float
    default: int
    min: int
    max: int
    description: str

    @property
    def is_switch(self):
        return self.min == 0 and self.max == 1 and self.scale == 1

    @property
    def display_unit(self):
        """Shown and entered in this unit: power in kW (easier to read than five-digit watts)"""
        return {"degC": "°C", "W": "kW"}.get(self.unit, self.unit)

    @property
    def display_factor(self):
        return 0.001 if self.unit == "W" else 1.0

    @property
    def decimals(self):
        return 1 if self.unit == "W" or self.scale < 1 else 0

    def to_display(self, raw):
        return raw * self.scale * self.display_factor

    def from_display(self, value):
        return int(round(float(value) / self.display_factor / self.scale))

    def to_raw(self, value):
        return int(round(float(value) / self.scale))

    def to_value(self, raw):
        return raw * self.scale

    def format(self, raw, unit=True):
        if raw is None:
            return "-"
        if self.is_switch:
            return "On" if raw else "Off"
        text = f"{self.to_display(raw):.{self.decimals}f}"
        return f"{text} {self.display_unit}" if unit and self.unit else text

    def range_text(self):
        if self.is_switch:
            return "Off / On"
        return f"{self.format(self.min, False)} - {self.format(self.max)}"

    def in_range(self, raw):
        return self.min <= raw <= self.max


class Model:
    def __init__(self, path=DEFAULT_MODEL):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        self.protocol_version=d["protocol_version"]
        self.config_version=d["config_version"]
        self.faults={int(k):v for k,v in d["faults"].items()}
        self.path = path
        self.dbc_path = os.path.normpath(os.path.join(os.path.dirname(path), d["dbc"]))
        self.can1_bitrate = d["can1_bitrate"]
        self.slaves = d["slaves"]
        self.cells_per_slave = d["cells_per_slave"]
        self.temps_per_slave = d["temps_per_slave"]
        self.cell_uv_v = d["cell_uv_v"]
        self.cell_ov_v = d["cell_ov_v"]
        self.cell_temp_min_c = d["cell_temp_min_c"]
        self.cell_temp_max_c = d["cell_temp_max_c"]
        self.cell_charge_temp_min_c = d.get("cell_charge_temp_min_c", self.cell_temp_min_c)    # While charging
        self.cell_charge_temp_max_c = d.get("cell_charge_temp_max_c", self.cell_temp_max_c)
        self.temp_invalid_c = d["temp_invalid_c"]
        self.power_limit_w = d["power_limit_w"]
        self.soc = d["soc"]
        self.config_request_id = d["config_request_id"]
        self.config_response_id = d["config_response_id"]
        self.commands = d["commands"]                               # name -> number
        self.results = {int(k): v for k, v in d["results"].items()}  # number -> name
        self.settings = [Setting(**s) for s in d["settings"]]
        self.settings_by_id = {s.id: s for s in self.settings}

    @property
    def total_cells(self):
        return self.slaves * self.cells_per_slave

    # Signal names in can/bms_can1.dbc (tools/gen_dbc.py)
    @staticmethod
    def seg(slave):
        return f"Seg{slave + 1:02d}"

    def cell_voltage(self, slave, cell):
        return f"{self.seg(slave)}_Cell{cell + 1:02d}_Voltage"

    def cell_flag(self, slave, cell, flag):
        """flag: Balancing, Fault or OpenWire"""
        return f"{self.seg(slave)}_Cell{cell + 1:02d}_{flag}"

    def cell_temp(self, slave, sensor):
        return f"{self.seg(slave)}_Temp{sensor + 1:02d}"

    def slave_signal(self, slave, name):
        """name: Voltage, Die_Temp, Comms_Error or Fault"""
        return f"{self.seg(slave)}_{name}"
