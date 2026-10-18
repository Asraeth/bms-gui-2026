"""
Runtime settings protocol (Core/Inc/bms_settings.h), CAN1, extended IDs, little-endian:

    BMS_Config_Request   byte 0 command, byte 1 setting, bytes 4-7 int32 value (write)
    BMS_Config_Response  byte 0 command, byte 1 setting, byte 2 result, byte 3 flags, bytes 4-7 int32 value

Requests go out one at a time: the next one only after the BMS has answered the last (or it timed out),
so the BMS's small request queue never overflows.
"""

import struct
import time
from collections import deque
from dataclasses import dataclass

FLAG_UNSAVED = 0x01
FLAG_MODIFIED = 0x02
FLAG_STORED = 0x04

TIMEOUT_S = 0.6         # The BMS answers between measurements: worst case ~50 ms, plus one response per 4 ms
RETRIES = 1


def encode_request(command, setting_id=0, value=0, version=2, token=0):
    return struct.pack("<BBBBi", command, setting_id, version, token, value)


@dataclass
class Response:
    command: int
    setting_id: int
    result: int
    flags: int
    value: int

    token: int = 0

    @classmethod
    def decode(cls, data):
        if len(data)!=8:raise ValueError("Configuration response must be exactly 8 bytes")
        command, setting_id, result, flags, value = struct.unpack("<BBBBi", bytes(data))
        return cls(command, setting_id, result, flags&7, value, flags>>3)


@dataclass
class _Request:
    command: int
    setting_id: int
    value: int
    expect: int             # Responses expected
    sent_at: float = 0.0
    tries: int = 0
    got: int = 0
    token: int = 0
    seen: set = None


class SettingsClient:
    """send(arbitration_id, data) puts a frame on CAN1. Feed every BMS_Config_Response to handle(); call
    poll() regularly for the pacing and timeouts. on_event(kind, text) reports results for the UI."""

    def __init__(self, model, send, on_event=None, clock=time.monotonic):
        self.model = model
        self.send = send
        self.on_event = on_event or (lambda kind, text: None)
        self.clock = clock
        self.values = {}            # setting id -> raw value in force on the BMS
        self.flags = None           # Last flags byte, None until the BMS has answered
        self.last_response_at = None
        self._queue = deque()
        self._active = None
        self._token=0
        self.identity_at=None
        self.compatible=False

    def observe_identity(self,protocol,config,slaves=None,cells=None,temps=None):
        self.identity_at=self.clock()
        self.compatible=protocol==self.model.protocol_version and config==self.model.config_version
        if slaves is not None:
            self.compatible = self.compatible and (slaves,cells,temps)==(self.model.slaves,self.model.cells_per_slave,self.model.temps_per_slave)
        if not self.compatible:self.cancel()

    # ---- Commands ------------------------------------------------------------------------------

    def read_all(self):
        self._enqueue(self.model.commands["Read_All"], 0, 0, len(self.model.settings))

    def read(self, setting_id):
        self._enqueue(self.model.commands["Read"], setting_id, 0, 1)

    def write(self, setting_id, raw):
        setting=self.model.settings_by_id.get(setting_id)
        if setting is None or not setting.in_range(raw):
            self.on_event("error","Setting outside documented bounds");return
        self._enqueue(self.model.commands["Write"], setting_id, int(raw), 1)

    def save(self):
        self._enqueue(self.model.commands["Save"], 0, 0, 1)

    def restore_defaults(self):
        self._enqueue(self.model.commands["Defaults"], 0, 0, 1)
        self.read_all()

    def busy(self):
        return self._active is not None or bool(self._queue)

    def cancel(self):
        self._queue.clear()
        self._active = None

    # ---- Flags ---------------------------------------------------------------------------------

    @property
    def unsaved(self):
        return self.flags is not None and bool(self.flags & FLAG_UNSAVED)

    @property
    def modified(self):
        return self.flags is not None and bool(self.flags & FLAG_MODIFIED)

    @property
    def stored(self):
        return self.flags is not None and bool(self.flags & FLAG_STORED)

    # ---- Traffic -------------------------------------------------------------------------------

    def _enqueue(self, command, setting_id, value, expect):
        if command in (self.model.commands["Write"],self.model.commands["Save"],self.model.commands["Defaults"]) and (not self.compatible or self.identity_at is None or self.clock()-self.identity_at>0.6):
            self.on_event("error","No fresh compatible BMS protocol identity; settings inhibited");return
        if len(self._queue)>=32:self.on_event("error","Settings queue full");return
        self._queue.append(_Request(command, setting_id, value, expect))
        self.poll()

    def _send(self, req):
        req.sent_at = self.clock()
        req.tries += 1
        req.got = 0
        req.seen=set()
        if not req.token:self._token=self._token%31+1;req.token=self._token
        self.send(self.model.config_request_id, encode_request(req.command, req.setting_id, req.value,self.model.config_version,req.token))

    def poll(self):
        now = self.clock()
        if self._active and now - self._active.sent_at > TIMEOUT_S:
            req = self._active
            if req.tries <= RETRIES:
                self._send(req)
            else:
                self._active = None
                self.on_event("error", f"No answer from the BMS to {self._describe(req.command, req.setting_id)}")
        if self._active is None and self._queue:
            self._active = self._queue.popleft()
            self._send(self._active)

    def handle(self, data):
        try:r = Response.decode(data)
        except (ValueError,struct.error):return None
        req=self._active
        if req is None or r.token!=req.token or r.command!=req.command or (req.setting_id and r.setting_id!=req.setting_id) or r.setting_id in req.seen:return None
        req.seen.add(r.setting_id)
        self.flags = r.flags
        self.last_response_at = self.clock()
        setting = self.model.settings_by_id.get(r.setting_id)
        if setting is not None:
            self.values[r.setting_id] = r.value

        result = self.model.results.get(r.result, f"result {r.result}")
        what = self._describe(r.command, r.setting_id)
        if r.result == 0:
            if r.command == self.model.commands["Write"] and setting is not None:
                self.on_event("ok", f"{setting.label} set to {setting.format(r.value)}")
            elif r.command == self.model.commands["Save"]:
                self.on_event("ok", "Settings saved in the BMS flash")
            elif r.command == self.model.commands["Defaults"]:
                self.on_event("ok", "Built-in values restored (not saved)")
        else:
            value = f" (stays {setting.format(r.value)})" if setting is not None else ""
            self.on_event("error", f"{what}: {result.replace('_', ' ')}{value}")

        req = self._active
        if req and r.command == req.command and (req.setting_id == 0 or r.setting_id == req.setting_id):
            req.got += 1
            if req.got >= req.expect:
                self._active = None
        self.poll()
        return r

    def _describe(self, command, setting_id):
        names = {v: k for k, v in self.model.commands.items()}
        setting = self.model.settings_by_id.get(setting_id)
        name = names.get(command, f"command {command}").replace("_", " ").lower()
        single = (self.model.commands["Read"], self.model.commands["Write"])
        return f"{name} {setting.label.lower()}" if setting is not None and command in single else name
