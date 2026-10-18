"""
Latest value of every decoded signal, plus a short history for plotting (at most one point per
MIN_INTERVAL_S per signal, the last MAX_POINTS of them).
"""

from collections import deque

MIN_INTERVAL_S = 0.05
MAX_POINTS = 1200


class SignalStore:
    def __init__(self):
        self.latest = {}            # name -> (time, value)
        self._t = {}
        self._v = {}

    def update(self, t, decoded):
        for name, value in decoded.items():
            self.latest[name] = (t, value)
            times = self._t.get(name)
            if times is None:
                times = self._t[name] = deque(maxlen=MAX_POINTS)
                self._v[name] = deque(maxlen=MAX_POINTS)
            if not times or t - times[-1] >= MIN_INTERVAL_S:
                times.append(t)
                self._v[name].append(value)

    def get(self, name, max_age=None, now=None):
        entry = self.latest.get(name)
        if entry is None:
            return None
        if max_age is not None and now is not None and now - entry[0] > max_age:
            return None
        return entry[1]

    def time(self, name):
        entry = self.latest.get(name)
        return entry[0] if entry else None

    def series(self, name):
        return list(self._t.get(name, ())), list(self._v.get(name, ()))

    def clear(self):
        self.latest.clear()
        self._t.clear()
        self._v.clear()
