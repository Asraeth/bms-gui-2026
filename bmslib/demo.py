"""
A pretend BMS on a python-can virtual bus, for trying the GUI without hardware (bms_gui.py --demo) and for
the GUI's tests. It sends the BMS's CAN1 frames, encoded with the generated can/bms_can1.dbc so the GUI
decodes them exactly as it would the car's, and answers settings requests by the firmware's rules
(Core/Src/bms_settings.c).

Scenario, repeating every 60 s: 15 s at rest with the TS off, then the TS on and 30 s of driving
(current up to 150 A, regen bursts), then the TS off again.
"""

import math
import random
import struct
import threading
import time

import can

from .settings import FLAG_MODIFIED, FLAG_STORED, FLAG_UNSAVED

FAST_PERIOD_S = 0.02        # The car sends these every 4 ms; slower is plenty for a demo
BURST_PERIOD_S = 0.1
PACK_R_OHM = 0.3


class DemoBms:
    def __init__(self, model, db, channel="bms-demo", seed=1):
        self.model = model
        self.db = db
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.rng = random.Random(seed)
        self.cells = [[3.86 + self.rng.uniform(-0.015, 0.015) for _ in range(model.cells_per_slave)]
                      for _ in range(model.slaves)]
        self.temps = [[24.0 + self.rng.uniform(-1.0, 3.0) for _ in range(model.temps_per_slave)]
                      for _ in range(model.slaves)]
        self.current = 0.0
        self.ts_active = False
        self.values = {s.id: s.default for s in model.settings}
        self.saved = dict(self.values)
        self.stored = False
        self.energy_used = 0.0
        self.start_soc = None
        self._stop = threading.Event()
        self._thread = None
        self.token=0
        self.t0 = time.monotonic()

    # ---- Life cycle ------------------------------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, name="demo-bms", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.bus.shutdown()

    def _run(self):
        next_fast = next_burst = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            msg = self.bus.recv(timeout=0.002)
            while msg is not None:
                if msg.is_extended_id and msg.arbitration_id == self.model.config_request_id:
                    self._handle_request(bytes(msg.data))
                msg = self.bus.recv(timeout=0)
            if now >= next_fast:
                self._step(FAST_PERIOD_S, now - self.t0)
                self._send_fast()
                next_fast += FAST_PERIOD_S
            if now >= next_burst:
                self._send_burst()
                next_burst += BURST_PERIOD_S

    # ---- Pack model ------------------------------------------------------------------------------

    def _step(self, dt, t):
        phase = t % 60.0
        self.ts_active = 15.0 <= phase < 50.0
        if 18.0 <= phase < 48.0:
            drive = max(0.0, math.sin((phase - 18.0) * 0.9)) * 150.0
            regen = -30.0 if math.sin((phase - 18.0) * 0.9) < -0.8 else 0.0
            self.current = drive + regen + self.rng.uniform(-2.0, 2.0)
        else:
            self.current = self.rng.uniform(-0.3, 0.3)
        # Cells discharge with the current, the hottest warm up a little
        dv = self.current * dt / 3600.0 / 21.0 * 0.9         # ~21 Ah per cell group, ~0.9 V over the range
        for s in range(self.model.slaves):
            for c in range(self.model.cells_per_slave):
                self.cells[s][c] -= dv
            for k in range(self.model.temps_per_slave):
                self.temps[s][k] += (abs(self.current) * 0.00002 - (self.temps[s][k] - 24.0) * 0.0005) * dt * 50
        self.energy_used += self.pack_voltage() * self.current * dt / 3600.0

    def ocv(self):
        return sum(sum(row) for row in self.cells)

    def pack_voltage(self):
        return self.ocv() - PACK_R_OHM * self.current

    def soc(self):
        from .soc import mean_cell_voltage, soc_from_cell_voltage
        mean, _ = mean_cell_voltage([v for row in self.cells for v in row], self.model.soc["cell_min_v"],
                                    self.model.soc["cell_max_v"])
        if self.start_soc is None:
            self.start_soc = soc_from_cell_voltage(mean, self.model.soc)
        energy = self.model.soc["pack_energy_wh"]
        remaining = max(0.0, self.start_soc / 100.0 * energy - self.energy_used)
        return remaining / energy * 100.0, remaining

    def limits(self):
        s = {st.name: st.to_value(self.values[st.id]) for st in self.model.settings}
        u = self.pack_voltage()
        hot = max(max(row) for row in self.temps)
        shift = 60.0 - s["Temp_Limit"]
        f = min(max((hot - (55.0 - shift)) / 5.0, 0.0), 1.0)
        derated = s["Discharge_Current_Limit"] - max(s["Discharge_Current_Limit"] - 25.0, 0.0) * f
        discharge = min(derated, s["Power_Limit"] / u)
        cold=min(min(row) for row in self.temps)
        regen = s["Regen_Current_Limit"] if s["Regen_Enabled"] and cold>=self.model.cell_charge_temp_min_c and hot<=self.model.cell_charge_temp_max_c else 0.0
        return discharge, regen, discharge * u, f > 0, s["Power_Limit"] / u < derated

    # ---- Frames ----------------------------------------------------------------------------------

    def _send(self, name, values):
        msg = self.db.get_message_by_name(name)
        data = msg.encode(values, strict=False)
        self.bus.send(can.Message(arbitration_id=msg.frame_id, data=data, is_extended_id=msg.is_extended_frame))

    def _send_fast(self):
        soc, remaining = self.soc()
        self._send("BMS_Fast_1", {"Fast_Pack_Current": self.current, "Fast_Pack_Voltage": self.pack_voltage()})
        self._send("BMS_Fast_2", {"Fast_Vehicle_Voltage": self.pack_voltage() if self.ts_active else 0.4,
                                  "Fast_Power": self.current * self.pack_voltage(),
                                  "Fast_Energy_Remaining": round(remaining)})
        discharge, regen, power, temp_derating, power_limited = self.limits()
        self._send("BMS_Limits", {
            "Discharge_Power_Limit": power, "Discharge_Current_Limit": discharge, "Regen_Current_Limit": regen,
            "Limit_Temp_Derating": int(temp_derating), "Limit_Power": int(power_limited),
            "Limit_Regen_Temp_Derating": 0, "Limit_AMS_Fault": 0, "Limit_Boost": 0,
            "Limit_Settings_Modified": int(self._modified()), "Window_Avg_Power": max(0.0, self.current * self.pack_voltage()),
        })

    def _send_burst(self):
        m = self.model
        self._send("BMS_Identity",dict(BMS_ProtocolVersion=m.protocol_version,BMS_ConfigVersion=m.config_version,BMS_BoardProfile=4,BMS_Slaves=m.slaves,BMS_CellsPerSlave=m.cells_per_slave,BMS_TempsPerSlave=m.temps_per_slave,BMS_PhysicalChannels=16,BMS_Bench=0))
        for name in ("BMS_Quality","BMS_IVT_Diagnostics","BMS_Charger"):
            values={s.name:0 for s in self.db.get_message_by_name(name).signals}
            if name=="BMS_Quality":values.update({"BMS_"+k:1 for k in ["CellsValid","AuxValid","CelsiusValid","IVTConfigured","CurrentValid","U1Valid","U2Valid","EnergyValid"]})
            self._send(name,values)
        for ic in range(m.slaves):
            prefix=m.seg(ic)
            self._send(prefix+"_Diagnostics",{s.name:255 if "ValidMask" in s.name else 0 for s in self.db.get_message_by_name(prefix+"_Diagnostics").signals})
            for k in range((m.temps_per_slave+3)//4):self._send(prefix+f"_RawAux{k+1}",{prefix+f"_RawAux{k*4+j+1:02d}":1.5 for j in range(4)})
        flat_cells = [v for row in self.cells for v in row]
        flat_temps = [v for row in self.temps for v in row]
        soc, remaining = self.soc()
        u = self.pack_voltage()
        self._send("BMS_Pack", {"Pack_Voltage_IVT": u, "Pack_Current": self.current,
                                "Pack_Voltage_SumOfCells": self.ocv() - PACK_R_OHM * self.current * 0.8,
                                "IVT_Current_Timeout": 0, "IVT_Voltage_Timeout": 0, "IVT_Sensor_Error": 0})
        status = {s.name: 0 for s in self.db.get_message_by_name("BMS_Status").signals}
        status.update({"TSAC_Ready": 1, "SDC_Closed": 1, "Measurement_Age": 35, "Measurement_Cycle_Time": 31,
                       "Precharge_State": 3 if self.ts_active else 0, "Precharge_Percent": 100 if self.ts_active else 0,
                       "Balancing_Enabled": 1})
        self._send("BMS_Status", status)
        self._send("BMS_Summary", {"Cell_Voltage_Min": min(flat_cells), "Cell_Voltage_Max": max(flat_cells),
                                   "Cell_Temp_Min": min(flat_temps), "Cell_Temp_Max": max(flat_temps)})
        self._send("BMS_Energy", {"Power": self.current * u, "Energy_Remaining": round(remaining), "SoC": soc,
                                  "Energy_Used": round(self.energy_used)})
        lowest = min(flat_cells)
        for s in range(m.slaves):
            seg = m.seg(s)
            values = {f"{seg}_Voltage": sum(self.cells[s]), f"{seg}_Die_Temp": 31.0 + s * 0.2,
                      f"{seg}_Comms_Error": 0, f"{seg}_Fault": 0}
            for c in range(m.cells_per_slave):
                values[m.cell_flag(s, c, "Balancing")] = int(self.cells[s][c] - lowest > 0.02 and not self.ts_active)
                values[m.cell_flag(s, c, "Fault")] = 0
                values[m.cell_flag(s, c, "OpenWire")] = 0
            self._send(f"BMS_{seg}", values)
            for k in range((m.cells_per_slave + 3) // 4):
                self._send(f"BMS_{seg}_Voltages_{k + 1}",
                           {m.cell_voltage(s, c): self.cells[s][c] for c in range(k * 4, min(k * 4 + 4, m.cells_per_slave))})
            for k in range((m.temps_per_slave + 3) // 4):
                self._send(f"BMS_{seg}_Temps_{k + 1}",
                           {m.cell_temp(s, t): self.temps[s][t] for t in range(k * 4, min(k * 4 + 4, m.temps_per_slave))})

    # ---- Settings (Core/Src/bms_settings.c) ------------------------------------------------------

    def _modified(self):
        return any(self.values[s.id] != s.default for s in self.model.settings)

    def _flags(self):
        return ((FLAG_UNSAVED if self.values != self.saved else 0) | (FLAG_MODIFIED if self._modified() else 0) |
                (FLAG_STORED if self.stored else 0))

    def _respond(self, command, setting_id, result):
        value = self.values.get(setting_id, 0)
        data = struct.pack("<BBBBi", command, setting_id, result, self._flags()|(self.token<<3), value)
        self.bus.send(can.Message(arbitration_id=self.model.config_response_id, data=data, is_extended_id=True))

    def _handle_request(self, data):
        if len(data)!=8:return
        command,setting_id,version,self.token,value=struct.unpack("<BBBBi",data)
        if version!=self.model.config_version:self._respond(command,setting_id,7);return
        if self.token>31:self.token&=31;self._respond(command,setting_id,8);return
        cmd = self.model.commands
        setting = self.model.settings_by_id.get(setting_id)
        if command == cmd["Read"]:
            self._respond(command, setting_id, 0 if setting else 1)
        elif command == cmd["Write"]:
            if setting is None:
                self._respond(command, setting_id, 1)
            elif not setting.in_range(value):
                self._respond(command, setting_id, 2)
            else:
                self.values[setting_id] = value
                self._respond(command, setting_id, 0)
        elif command == cmd["Save"]:
            if self.ts_active:
                self._respond(command, 0, 3)
            else:
                self.saved = dict(self.values)
                self.stored = True
                self._respond(command, 0, 0)
        elif command == cmd["Defaults"]:
            self.values = {s.id: s.default for s in self.model.settings}
            self._respond(command, 0, 0)
        elif command == cmd["Read_All"]:
            for s in self.model.settings:
                self._respond(command, s.id, 0)
        else:
            self._respond(command, setting_id, 5)
