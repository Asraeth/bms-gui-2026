"""
GUI tests: the SoC estimate against the firmware, the settings protocol against the DBC and the demo BMS,
and the whole window against the demo BMS (hidden).

    python -m unittest discover -s GUI/tests -v          (from the repository root)
"""

import csv
import json
import os
import tempfile
import sys
import time
import unittest

import can
import cantools

GUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = GUI_DIR
sys.path.insert(0, GUI_DIR)

from bmslib import soc as soclib                                        # noqa: E402
from bmslib.demo import DemoBms                                          # noqa: E402
from bmslib.model import Model                                           # noqa: E402
from bmslib.settings import FLAG_MODIFIED, FLAG_STORED, FLAG_UNSAVED, SettingsClient, encode_request  # noqa: E402

FIRMWARE_SOC_CURVE = os.environ.get("BMS_SOC_CURVE",os.path.join(ROOT,"protocol","soc_model.csv"))


class SocTest(unittest.TestCase):
    def setUp(self):
        self.model = Model()

    def test_spreadsheet_values(self):
        # Battery Tracker.xlsx cached VToSoc results (as test/tests.c soc_model)
        self.assertAlmostEqual(soclib.soc_from_cell_voltage(3.68, self.model.soc), 51.6531, places=3)
        self.assertAlmostEqual(soclib.soc_from_cell_voltage(3.69, self.model.soc), 52.7241, places=3)

    @unittest.skipUnless(os.path.exists(FIRMWARE_SOC_CURVE), "run test/run_tests.sh soc_model first")
    def test_matches_firmware_curve(self):
        worst = 0.0
        with open(FIRMWARE_SOC_CURVE, newline="") as f:
            for v, fw in csv.reader(f):
                worst = max(worst, abs(soclib.soc_from_cell_voltage(float(v), self.model.soc) - float(fw)))
        self.assertLess(worst, 0.01, "GUI and firmware SoC curves differ")

    def test_mean_leaves_out_cells_outside_the_model(self):
        s = self.model.soc
        mean, used = soclib.mean_cell_voltage([3.7, 3.9, s["cell_min_v"], s["cell_max_v"], 1.0, None], s["cell_min_v"],
                                              s["cell_max_v"])
        self.assertEqual(used, 2)
        self.assertAlmostEqual(mean, 3.8)

    def test_rest_condition(self):
        s = self.model.soc
        cells = [3.8] * 10
        self.assertTrue(soclib.estimate(cells, 0.5, s).at_rest)
        self.assertFalse(soclib.estimate(cells, s["no_load_current_a"] + 1, s).at_rest)
        self.assertIsNone(soclib.estimate(cells, None, s, ts_active=False).at_rest)    # TS off: nothing can flow
        self.assertIsNone(soclib.estimate(cells, None, s).at_rest)
        e = soclib.estimate(cells, 0.0, s)
        self.assertAlmostEqual(e.energy_wh, e.soc_percent / 100 * s["pack_energy_wh"])


class SettingsEncodingTest(unittest.TestCase):
    def test_request_matches_dbc(self):
        model = Model()
        db = cantools.database.load_file(model.dbc_path)
        msg = db.get_message_by_name("BMS_Config_Request")
        self.assertEqual(msg.frame_id, model.config_request_id)
        for s in model.settings:
            raw = (s.min + s.max) // 2
            expected = msg.encode({"Config_Command": "Write", "Config_Setting": s.name,
                                   f"Setting_{s.name}": s.to_value(raw),"Config_Version":2,"Config_Token":0})
            self.assertEqual(encode_request(model.commands["Write"], s.id, raw), expected, s.name)
        self.assertEqual(db.get_message_by_name("BMS_Config_Response").frame_id, model.config_response_id)


class SettingsProtocolTest(unittest.TestCase):
    """The client against the demo BMS (which follows bms_settings.c) on a virtual bus"""

    def setUp(self):
        self.model = Model()
        self.db = cantools.database.load_file(self.model.dbc_path)
        channel = f"test-{time.monotonic_ns()}"
        self.demo = DemoBms(self.model, self.db, channel=channel).start()
        self.bus = can.Bus(interface="virtual", channel=channel)
        self.events = []
        self.client = SettingsClient(self.model, self._send, lambda kind, text: self.events.append((kind, text)))
        self.client.observe_identity(2,2)

    def tearDown(self):
        self.demo.stop()
        self.bus.shutdown()

    def _send(self, arbitration_id, data):
        self.bus.send(can.Message(arbitration_id=arbitration_id, data=data, is_extended_id=True))

    def _settle(self, timeout=3.0):
        end = time.monotonic() + timeout
        while self.client.busy() and time.monotonic() < end:
            msg = self.bus.recv(timeout=0.01)
            if msg is not None and msg.arbitration_id == self.model.config_response_id:
                self.client.handle(bytes(msg.data))
            if msg is not None and msg.arbitration_id==0xB208 and len(msg.data)==8:self.client.observe_identity(msg.data[0],msg.data[1])
            self.client.poll()
        self.assertFalse(self.client.busy(), "BMS did not answer")

    def test_read_write_save(self):
        by_name = {s.name: s for s in self.model.settings}
        self.client.read_all()
        self._settle()
        self.assertEqual(self.client.values, {s.id: s.default for s in self.model.settings})
        self.assertEqual(self.client.flags, 0)

        power = by_name["Power_Limit"]
        self.client.write(power.id, power.max + 1)                       # Above the built-in limit: refused
        self._settle()
        self.assertEqual(self.client.values[power.id], power.default)
        self.assertEqual(self.events[-1][0], "error")

        self.client.write(power.id, power.to_raw(60000))
        self._settle()
        self.assertEqual(self.client.values[power.id], 60000)
        self.assertTrue(self.client.unsaved and self.client.modified and not self.client.stored)

        self.client.save()
        self._settle()
        self.assertEqual(self.client.flags, FLAG_STORED | FLAG_MODIFIED)

        self.client.restore_defaults()
        self._settle()
        self.assertEqual(self.client.values[power.id], power.default)
        self.assertEqual(self.client.flags, FLAG_STORED | FLAG_UNSAVED)

    def test_save_refused_with_ts_active(self):
        self.demo.t0 = time.monotonic() - 20.0                            # Demo scenario: TS on from 15 s
        time.sleep(0.1)
        self.client.save()
        self._settle()
        self.assertEqual(self.events[-1][0], "error")
        self.assertIn("TS Active", self.events[-1][1])


class CellJudgingTest(unittest.TestCase):
    """Near-limit bars, past-limit tiles and the shading range"""

    @classmethod
    def setUpClass(cls):
        try:
            from bmslib import widgets
        except ImportError:
            raise unittest.SkipTest("needs PySide6 and pyqtgraph")
        cls.w = widgets
        cls.m = Model()

    def test_voltage(self):
        w, m = self.w, self.m
        self.assertEqual(w.judge_voltage(m, 3.8), (False, None))
        self.assertEqual(w.judge_voltage(m, m.cell_uv_v + w.NEAR_UV_V / 2), (False, "low"))
        self.assertEqual(w.judge_voltage(m, m.cell_ov_v - w.NEAR_OV_V / 2), (False, "high"))
        self.assertEqual(w.judge_voltage(m, m.cell_uv_v), (True, "low"))
        self.assertEqual(w.judge_voltage(m, m.cell_ov_v + 0.01), (True, "high"))

    def test_temperature_uses_the_charge_window_while_charging(self):
        w, m = self.w, self.m
        warm = m.cell_charge_temp_max_c - 1
        self.assertEqual(w.judge_temp(m, warm, charging=True), (False, "high"))
        self.assertEqual(w.judge_temp(m, warm, charging=False)[0], False)
        self.assertEqual(w.judge_temp(m, m.cell_charge_temp_max_c, charging=True), (True, "high"))
        self.assertEqual(w.judge_temp(m, m.cell_temp_min_c + 1, charging=False), (False, "low"))
        self.assertEqual(w.judge_temp(m, m.cell_temp_max_c, charging=False), (True, "high"))

    def test_shading_ignores_flagged_cells(self):
        w, m = self.w, self.m
        values = [3.80, 3.85, 3.83, m.cell_uv_v + 0.05]          # One cell near the under-limit
        self.assertEqual(w.shading_range(values, lambda v: w.judge_voltage(m, v)), (3.80, 3.85))
        self.assertIsNone(w.shading_range([], lambda v: w.judge_voltage(m, v)))


class WindowTest(unittest.TestCase):
    """The whole GUI (hidden): a demo session, and a much bigger pack"""

    @classmethod
    def setUpClass(cls):
        try:
            import pyqtgraph  # noqa: F401
            from PySide6.QtWidgets import QApplication
        except ImportError:
            raise unittest.SkipTest("needs PySide6 and pyqtgraph (pip install -r GUI/requirements.txt)")
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["BMS_GUI_PREFS"] = os.path.join(cls.tmp.name, "prefs.json")   # Leave the user's alone
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def window(self, argv):
        from PySide6.QtCore import Qt
        import bms_gui
        w = bms_gui.MainWindow(bms_gui.parse_args(argv))
        w.setAttribute(Qt.WA_DontShowOnScreen)
        w.log_check.setChecked(False)
        w.resize(1600, 1000)
        w.show()
        return w

    def pump(self, seconds, until=lambda: False):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not until():
            self.app.processEvents()
            time.sleep(0.01)

    def test_demo_session(self):
        from bmslib import theme
        w = self.window(["--demo", "--theme", "light"])
        try:
            m = w.model
            self.pump(6.0, lambda: len(w.settings.values) == len(m.settings) and
                      w.slave_cards[-1].state.temps and w.slave_cards[-1].state.temps[-1] is not None)
            self.assertTrue(w.link.connected)
            self.assertIsNotNone(w.slave_cards[0].state.cells[0].voltage)
            self.assertIsNotNone(w.slave_cards[-1].state.temps[-1])
            self.assertRegex(w.soc_rest.text(), r"^\d+\.\d %")
            self.assertEqual(len(w.settings.values), len(m.settings))
            power = m.settings[0]
            self.assertEqual(w.setting_widgets[power.id][0].text(), power.format(power.default))
            self.assertEqual(power.format(power.default), "80.0 kW")

            # A lower power limit from the form
            w.setting_widgets[power.id][1].setValue(50.0)                    # kW in the form
            w.write_setting(power)
            self.pump(2.0, lambda: w.settings.values.get(power.id) == 50000)
            self.assertEqual(w.settings.values.get(power.id), 50000)

            w.plot_signal("Fast_Pack_Voltage")
            w.plot_signal(m.cell_voltage(0, 0), add=True)
            points = lambda: w.plot.curves["Fast_Pack_Voltage"].getData()[0]          # noqa: E731
            self.pump(3.0, lambda: points() is not None and len(points()) > 10)
            self.assertEqual(len(w.plot.curves), 2)
            self.assertGreater(len(points()), 10)

            w.set_theme("dark")
            self.pump(0.3)
            self.assertIs(w.theme, theme.DARK)
            self.assertEqual(w.theme_box.currentText(), "Dark")

            # Daylight: bigger cards, pills restyled, values readable
            small = w.slave_cards[0].sizeHint()
            w.set_theme("daylight")
            self.pump(0.5)
            self.assertIs(w.theme, theme.DAYLIGHT)
            self.assertGreater(w.slave_cards[0].sizeHint().width(), small.width())
            self.assertGreater(w.side_area.width(), 400)
            self.assertIn(theme.DAYLIGHT.pill(theme.OK)[0].lower(), w.bms_pill.styleSheet().lower())
            self.assertFalse(os.path.exists(os.environ["BMS_GUI_PREFS"]) and
                             "virtual" in open(os.environ["BMS_GUI_PREFS"]).read(), "demo connection remembered")
        finally:
            w.close()

    def test_any_pack_size(self):
        """40 slaves of 16 cells: the cards wrap to the window and the view scrolls"""
        with open(Model().path, encoding="utf-8") as f:
            data = json.load(f)
        data.update(slaves=40, cells_per_slave=16, temps_per_slave=10)
        data["dbc"] = os.path.join(ROOT, "protocol", "bms_can1.dbc")
        path = os.path.join(self.tmp.name, "big_pack.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        w = self.window(["--model", path, "--theme", "light"])
        try:
            self.pump(0.5)
            self.assertEqual(len(w.slave_cards), 40)
            viewport = w.cards_area.viewport().width()
            self.assertTrue(all(c.geometry().right() <= viewport for c in w.slave_cards), "cards wider than the view")
            self.assertGreater(len({c.geometry().x() for c in w.slave_cards}), 1, "cards don't wrap into columns")
            self.assertGreater(w.cards_area.verticalScrollBar().maximum(), 0, "no scrolling for a big pack")
            card = w.slave_cards[0]
            self.assertGreaterEqual(card.height(), card.sizeHint().height())
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
