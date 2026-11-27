"""Sun position and light-based activity: lights-out (bedtime proxy), wake, evening lamp use.

The sensor sits in the living room, so "lights out" means the living room went
dark - a bedtime proxy, not a sleep measurement. The LED/sun lux split is not
calibrated yet (sunlight leaks into led_lux during the day), so artificial light
is only judged while the sun is down, where every lux is artificial.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .util import fill_short_false, local_time, runs

STATES = ["daylight", "lights on", "dark (awake/away)", "lights out (asleep)", "no data"]


def sun_elevation(idx: pd.DatetimeIndex, lat: float, lon: float) -> np.ndarray:
    """Solar elevation in degrees (low-precision almanac formula, ~0.5° - plenty for day/night)."""
    d = (idx.tz_convert("UTC") - pd.Timestamp("2000-01-01 12:00", tz="UTC")) / pd.Timedelta(days=1)
    d = np.asarray(d, dtype=float)
    rad = np.deg2rad
    g = rad((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    L = rad(q + 1.915 * np.sin(g) + 0.020 * np.sin(2 * g))
    e = rad(23.439 - 0.00000036 * d)
    ra = np.arctan2(np.cos(e) * np.sin(L), np.cos(L))
    dec = np.arcsin(np.sin(e) * np.sin(L))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = rad(gmst * 15 + lon) - ra
    la = rad(lat)
    return np.rad2deg(np.arcsin(np.sin(la) * np.sin(dec) + np.cos(la) * np.cos(dec) * np.cos(ha)))


def add_sun(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df["sun_elev_deg"] = sun_elevation(df.index, cfg["latitude"], cfg["longitude"])
    df["sun_up"] = df["sun_elev_deg"] > cfg["sun_down_elevation_deg"]
    return df


def dark_threshold(df: pd.DataFrame, cfg: dict) -> float | None:
    """Lux at or below which the living room counts as dark, from its own night-time floor."""
    if "led_lux" not in df.columns:
        return None
    led = df.loc[~df["sun_up"], "led_lux"].dropna()
    if len(led) < 120:
        return None
    floor = float(np.percentile(led, cfg["dark_floor_percentile"]))
    return max(floor * cfg["dark_floor_factor"], floor + cfg["dark_floor_margin_lux"])


def detect_activity(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, float | None]:
    """Adds df['state'] (one of STATES) and returns a one-row-per-night table."""
    thr = dark_threshold(df, cfg)
    if thr is None:
        return pd.DataFrame(), None
    tz = cfg["timezone"]
    led = df["led_lux"].to_numpy()
    sun_up = df["sun_up"].to_numpy()
    lit = np.nan_to_num(led, nan=-1) > thr
    nodata = np.isnan(led)

    state = np.where(sun_up, "daylight",
                     np.where(nodata, "no data", np.where(lit, "lights on", "dark (awake/away)")))
    state = state.astype(object)

    rows = []
    first, last = df.index[0], df.index[-1]
    for day in pd.date_range((first - pd.Timedelta(hours=12)).date(), last.date(), freq="D"):
        w0, w1 = local_time(day, 18, tz), local_time(day, 36, tz)
        i0, i1 = df.index.searchsorted(w0), df.index.searchsorted(w1)
        if i1 - i0 < 60 or np.mean(~nodata[i0:i1]) < 0.5:
            continue
        s_up, s_lit = sun_up[i0:i1], lit[i0:i1]
        dark = ~s_up & ~s_lit & ~nodata[i0:i1]
        dark_m = fill_short_false(dark, cfg["light_blip_merge_min"])
        ts = df.index[i0:i1]
        anchor = ts.searchsorted(local_time(day, 24 + cfg["night_anchor_hour"], tz))
        if anchor >= len(ts) - 1 or anchor == 0:     # record doesn't span this night's anchor
            continue
        sun_down_idx = np.flatnonzero(~s_up)
        sunset = ts[sun_down_idx[0]] if len(sun_down_idx) else pd.NaT
        row = {"night_of": day.date(), "sunset": sunset}

        run = next(((s, e) for s, e in runs(dark_m) if s <= anchor <= e), None)
        if run is None:
            row["note"] = (f"living room lit at {cfg['night_anchor_hour']:02d}:00 - lights left on "
                           "or a late night") if s_lit[anchor] else "no dark stretch around the anchor hour"
            rows.append(row)
            continue
        s, e = run
        lights_out, wake = ts[s], ts[min(e + 1, len(ts) - 1)]
        dusk_dark = s == 0 or s_up[s - 1] or (len(sun_down_idx) and s <= sun_down_idx[0] + 5)
        woke_by_sun = e + 1 < len(ts) and s_up[e + 1]
        interruptions = runs(s_lit[s:e + 1])

        ev = slice(sun_down_idx[0] if len(sun_down_idx) else s, s)
        mr = slice(e + 1, e + 1 + int(np.argmax(s_up[e + 1:])) if s_up[e + 1:].any() else len(ts))
        lit_ev = np.flatnonzero(s_lit[ev])
        row.update({
            "lights_out": lights_out,
            "wake": wake,
            "sleep_h": (wake - lights_out) / pd.Timedelta(hours=1),
            "bedtime_confidence": "low" if dusk_dark else "high",
            "wake_source": "daylight" if woke_by_sun else "lamp",
            "sleep_interruptions": len(interruptions),
            "evening_lamp_h": s_lit[ev].sum() / 60.0,
            "evening_dark_h": dark[ev].sum() / 60.0,
            "first_evening_lamp": ts[ev.start + lit_ev[0]] if len(lit_ev) else pd.NaT,
            "morning_lamp_min": float(s_lit[mr].sum()),
            "note": ("dark since dusk - no lamp switched off (away, or living room unused)"
                     if dusk_dark else "") +
                    ("; woke after sunrise - wake = daylight, not a lamp" if woke_by_sun else ""),
        })
        if row["sleep_h"] < cfg["min_sleep_h"]:
            row["note"] = (row["note"] + "; very short dark run").lstrip("; ")
        state[i0 + s:i0 + e + 1] = "lights out (asleep)"
        rows.append(row)

    df["state"] = state
    nights = pd.DataFrame(rows)
    if not nights.empty:
        nights["note"] = nights["note"].fillna("").str.lstrip("; ")
    print(f"[act]  dark threshold {thr:.2f} lux; {int(nights.get('lights_out', pd.Series()).notna().sum())} "
          f"nights with a lights-out run")
    return nights, thr
