"""
CAN connection through python-can, on Windows or Linux.

Received frames go into a queue (python-can's notifier thread) and the GUI thread drains it on a timer.
Every frame is also logged to GUI/logs/ in candump format (python-can's LogReader and can-utils read it).
"""

import datetime
import glob
import os
import queue
import sys

import can

from .model import GUI_DIR

# python-can interface name, what it's for, default channel
INTERFACES = [
    ("slcan", "Serial USB-CAN adapter (slcan: WeAct USB2CAN, CANable, ...)", ""),
    ("gs_usb", "candleLight / gs_usb firmware", "0"),
    ("pcan", "PEAK PCAN-USB", "PCAN_USBBUS1"),
    ("kvaser", "Kvaser", "0"),
    ("vector", "Vector", "0"),
    ("ixxat", "IXXAT", "0"),
    ("socketcan", "Linux SocketCAN (can0)", "can0"),
    ("virtual", "Virtual bus (demo BMS, no hardware)", "bms-demo"),
]

BITRATES = [1000000, 500000, 250000, 125000]


def list_serial_ports():
    """[(device, description)] of the serial ports, for slcan adapters"""
    try:
        from serial.tools import list_ports
        return [(p.device, p.description or p.device) for p in sorted(list_ports.comports(), key=lambda p: p.device)]
    except ImportError:
        pass
    ports = []
    if sys.platform.startswith("win"):
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                    except OSError:
                        break
                    ports.append((value, name.split("\\")[-1]))
                    i += 1
        except OSError:
            pass
    else:
        for pattern in ("/dev/serial/by-id/*", "/dev/ttyACM*", "/dev/ttyUSB*"):
            ports += [(p, os.path.basename(p)) for p in sorted(glob.glob(pattern))]
    return sorted(ports)


class _QueueListener(can.Listener):
    def __init__(self, q):
        self.q = q

    def on_message_received(self, msg):
        try:self.q.put_nowait(msg)
        except queue.Full:pass

    def on_error(self, exc):
        try:self.q.put_nowait(exc)
        except queue.Full:pass


class CanLink:
    def __init__(self):
        self.queue = queue.Queue(maxsize=20000)
        self.bus = None
        self.notifier = None
        self.log_file = None
        self.log_path = None
        self.description = ""
        self.tx_errors = 0

    @property
    def connected(self):
        return self.bus is not None

    def connect(self, interface, channel, bitrate, log=True):
        self.disconnect()
        kwargs = dict(interface=interface, channel=channel or None, bitrate=int(bitrate))
        if interface == "virtual":
            kwargs.pop("bitrate")
        elif interface == "socketcan":
            kwargs.pop("bitrate")                       # Set with ip link
        self.bus = can.Bus(**kwargs)
        listeners = [_QueueListener(self.queue)]
        if log:
            os.makedirs(os.path.join(GUI_DIR, "logs"), exist_ok=True)
            self.log_path = os.path.join(GUI_DIR, "logs",
                                         f"can_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
            self.log_file = open(self.log_path, "w", encoding="utf-8", newline="\n")
            listeners.append(can.CanutilsLogWriter(self.log_file))
        self.notifier = can.Notifier(self.bus, listeners, timeout=0.1)
        self.description = f"{interface} {channel} @ {int(bitrate) // 1000} kbit/s".replace(" @ 0 kbit/s", "")

    def send(self, arbitration_id, data, extended=True):
        if self.bus is None:
            raise RuntimeError("Not connected")
        try:
            self.bus.send(can.Message(arbitration_id=arbitration_id, data=bytes(data), is_extended_id=extended),
                          timeout=0.1)
        except can.CanError:
            self.tx_errors += 1
            raise

    def disconnect(self):
        if self.notifier is not None:
            self.notifier.stop()
            self.notifier = None
        if self.bus is not None:
            try:
                self.bus.shutdown()
            finally:
                self.bus = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None
        self.description = ""
