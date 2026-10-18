"""
Open-circuit state of charge, the same estimate the BMS takes at start-up (Core/Src/bms_soc.c):

    mean of the cell voltages strictly between cell_min_v and cell_max_v (as the Battery Tracker's Pack SoC)
    SoC % = 100 * sum_k D_k * T_k(t),  t = 2 (v - vmin) / (vmax - vmin) - 1, clamped to [-1, 1], result 0..100
    remaining energy = SoC x pack energy

T_k are Chebyshev polynomials ("Supporting Docs/Battery Tracker.xlsx", sheet Model). Only meaningful with
the pack at rest: under load the cells sag and the estimate reads low. No coulomb counting here.
"""

from dataclasses import dataclass


def soc_from_cell_voltage(v, model_soc):
    vmin, vmax, coeffs = model_soc["vmin"], model_soc["vmax"], model_soc["coeffs"]
    t = 2.0 * (v - vmin) / (vmax - vmin) - 1.0
    t = max(-1.0, min(1.0, t))
    t_prev, t_cur = 1.0, t
    total = coeffs[0] * t_prev + coeffs[1] * t_cur
    for c in coeffs[2:]:
        t_prev, t_cur = t_cur, 2.0 * t * t_cur - t_prev
        total += c * t_cur
    return max(0.0, min(100.0, 100.0 * total))


def mean_cell_voltage(voltages, cell_min_v, cell_max_v):
    """Mean of the cells inside the model's range (bms_meanCellVoltage). Returns (mean, cells used)."""
    used = [v for v in voltages if v is not None and cell_min_v < v < cell_max_v]
    return (sum(used) / len(used), len(used)) if used else (None, 0)


@dataclass
class RestEstimate:
    soc_percent: float = None       # None: no usable cell voltages
    energy_wh: float = None
    mean_v: float = None
    cells_used: int = 0
    cells_seen: int = 0
    at_rest: bool = None            # None: no current measurement
    current_a: float = None


def estimate(voltages, current_a, model_soc, ts_active=None):
    """voltages: every cell voltage received (None for missing). current_a: IVT current, None if unknown.
    ts_active: False when no current can flow (TS off) - then the pack is at rest whatever the current reads."""
    seen = [v for v in voltages if v is not None]
    mean, used = mean_cell_voltage(seen, model_soc["cell_min_v"], model_soc["cell_max_v"])
    e = RestEstimate(mean_v=mean, cells_used=used, cells_seen=len(seen), current_a=current_a)
    if current_a is not None:
        e.at_rest = abs(current_a) < model_soc["no_load_current_a"]
    if mean is not None:
        e.soc_percent = soc_from_cell_voltage(mean, model_soc)
        e.energy_wh = e.soc_percent / 100.0 * model_soc["pack_energy_wh"]
    return e
