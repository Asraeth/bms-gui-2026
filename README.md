# Integrated 2026 BMS GUI

This fork keeps its own Git history and uses the lead Qt GUI as the integration starting point.
The final protocol is generated in the separate `g474-bms-2026` firmware repository.

```sh
python -m pip install -r requirements.txt
python bms_gui.py --demo
python bms_gui.py --interface slcan --channel COM5 --bitrate 1000000 --connect
python -m unittest discover -s tests -v
```

Select the actual adapter/channel in the window. Supported python-can backends include slcan,
gs_usb, PCAN, Kvaser, Vector, IXXAT, SocketCAN and virtual. SocketCAN bitrate is configured by the OS.
The GUI connects to TSCAN (CAN1), normally 1 Mbit/s. IVT and charger buses are physically separate.

The overview supports 14 boards × 8 populated cells and 8 logical temperature inputs (112 each),
pack current/U1/U2, independent cell sum and voltage disagreement, SoC/energy, inverter limits,
precharge, balancing and AMS state. Raw AUX and board-health tables show acquisition validity,
calibration validity, PEC/counter counts and transport/stop faults separately. A BENCH firmware identity
advertises one board. Select `--model bms_model_bench.json` for eight cells and eight raw AUX inputs. The default 14-board view inhibits settings writes to a different topology.

`bms_model.json` and `protocol/` are generated release artifacts. To update or check them, run from
firmware:

```sh
python tools/generate.py
python tools/sync_gui_protocol.py ../bms-gui-2026
python tools/sync_gui_protocol.py ../bms-gui-2026 --check
```

Protocol/configuration version 2 is required. Writes require a fresh compatible BMS identity,
documented bounds and matching transaction token. Short, remote and CAN-FD packets are rejected.
GUI availability has no role in firmware protection. Device profiles and safety thresholds cannot be
arbitrarily uploaded; runtime requests can lower electrical limits or select bounded documented settings.

Raw CAN logs are written to `logs/` in candump format. `python log-reader.py FILE --output decoded.csv`
decodes logs with the shipped DBC; the traffic tab and live plots use the same definitions.

The historical `main.py` … `main6.py` launch this application. Old seven-board DBC generators and
ADBMS2950 schemas are retired; Git history retains them. `can-test.py` starts the virtual demo.
Production temperature calibration and physical hardware verification remain pending. The demo is a
software fixture and does not validate contactors, charger behavior, sensor calibration or balancing heat.
