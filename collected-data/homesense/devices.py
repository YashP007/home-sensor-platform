"""Board health and light-split calibration from the status text feeds.

Each Level 2 board publishes "up=.. rssi=.. heap=.." every 5 min; the living-room
board adds raw TSL2591 counts (full, ir, gain, lux). io.load_status() parses them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .activity import sun_elevation


def health(status: pd.DataFrame) -> pd.DataFrame:
    """One row per board: messages, restarts (uptime went backwards), WiFi RSSI, free heap."""
    rows = []
    for room, g in status.groupby("room"):
        up = g["up"] if "up" in g else pd.Series(dtype=float)
        rows.append({"room": room, "messages": len(g), "first": g["ts"].iloc[0], "last": g["ts"].iloc[-1],
                     "restarts": int((up.diff() < 0).sum()),
                     "rssi_median_dbm": g["rssi"].median() if "rssi" in g else np.nan,
                     "rssi_min_dbm": g["rssi"].min() if "rssi" in g else np.nan,
                     "heap_min_b": g["heap"].min() if "heap" in g else np.nan})
    return pd.DataFrame(rows)


def light_calibration(status: pd.DataFrame, cfg: dict) -> dict:
    """Median ir/full ratio in strong daylight (-> R_SUN) and lamp light after dusk (-> R_LED)."""
    s = status[status["room"] == "living"] if not status.empty else status
    if s.empty or "ir_ratio" not in s:
        return {}
    s = s[s["full"] >= cfg["calib_min_counts"]].copy()
    s["elev"] = sun_elevation(pd.DatetimeIndex(s["ts"]), cfg["latitude"], cfg["longitude"])
    out = {}
    for key, sel in (("R_SUN", (s["elev"] > cfg["calib_sun_min_elev_deg"]) & (s["lux"] >= cfg["calib_sun_min_lux"])),
                     ("R_LED", (s["elev"] < cfg["calib_led_max_elev_deg"]) & (s["lux"] >= cfg["calib_led_min_lux"]))):
        r = s.loc[sel, "ir_ratio"].dropna()
        out[key] = {"n": int(len(r)),
                    "median": float(r.median()) if len(r) >= cfg["calib_min_samples"] else None,
                    "iqr": [float(r.quantile(.25)), float(r.quantile(.75))] if len(r) >= cfg["calib_min_samples"] else None}
    return out
