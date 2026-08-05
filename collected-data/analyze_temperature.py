#!/usr/bin/env python3
"""
analyze_temperature.py — Apartment HVAC / thermal analysis from BME280 temperature logs.

Reads Adafruit IO CSV exports (id,value,feed_id,created_at,lat,lon,ele) produced by the
SmartHome Monitor firmware, segments the record into 3AM-to-3AM local days, detects AC
cycling events, quantifies cooling and recovery-heating slopes by time-of-day period, and
estimates the apartment's thermal time constant (tau).

Design goals
------------
* Self-reliant: auto-discovers input CSVs, needs only numpy/pandas/matplotlib.
  No scipy, no API keys. Outdoor-weather cross-check is strictly optional and
  degrades gracefully when the network is unavailable.
* Idempotent + append-safe: outputs are stamped with the DATA's datetime range, not the
  run time, so re-running on the same export overwrites the same files, and running on a
  new export two weeks later produces a new, non-colliding set.
* Multi-file: pass several CSVs (or let it glob the directory) and overlapping rows are
  de-duplicated by Adafruit IO record id.

Usage
-----
    python analyze_temperature.py                    # glob *.csv in raw-data/
    python analyze_temperature.py data1.csv data2.csv
    python analyze_temperature.py --outdir results --weather
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import glob
import warnings
from dataclasses import dataclass, field, asdict
from datetime import timedelta

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

warnings.filterwarnings("ignore", category=RuntimeWarning)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# CSVs live in raw-data/ now (used to sit next to the script). Fall back to
# SCRIPT_DIR itself so this doesn't break again if someone flattens it back out.
RAW_DATA_DIR = os.path.join(SCRIPT_DIR, "raw-data")
if not os.path.isdir(RAW_DATA_DIR):
    RAW_DATA_DIR = SCRIPT_DIR
print(f"[cfg]  Script directory: {SCRIPT_DIR}")
print(f"[cfg]  Raw data directory: {RAW_DATA_DIR}")

# ---- Configuration ----

DEFAULT_CONFIG = {
    # ── Locale / calendar ──────────────────────────────────────────────────────
    "timezone": "America/New_York",   # CSV timestamps are UTC; all analysis is local
    "day_start_hour": 3,              # analysis "day" runs 03:00 -> 03:00 next day

    # Time-of-day periods, [start_hour, end_hour) in local time. Wrapping allowed.
    "periods": {
        "night":     [22, 5],         # 10PM - 5AM  (wraps midnight)
        "morning":   [5, 10],         # 5AM  - 10AM
        "afternoon": [10, 18],        # 10AM - 6PM  (setpoint raised to ~86F)
        "evening":   [18, 22],        # 6PM  - 10PM
    },

    # ── Resampling / smoothing ────────────────────────────────────────────────
    "grid_seconds": 30,               # uniform resample grid (matches publish interval)
    "max_interp_gap_min": 5.0,        # gaps longer than this stay NaN (no fake data)
    "median_filter_samples": 3,       # spike-robust pre-filter
    "smooth_window_min": 1.5,         # rolling-mean smoothing window

    # ── Slope estimation ──────────────────────────────────────────────────────
    "slope_window_min": 2.5,          # centered least-squares window for dT/dt

    # ── AC event detection (all rates in degF/min) ────────────────────────────
    "cool_slope_on": 0.15,            # slope < -this  => actively cooling
    "merge_gap_min": 1.5,             # merge cooling runs separated by less than this
    "min_cool_duration_min": 1.5,     # reject runs shorter than this
    "min_drop_f": 0.50,               # reject runs with total drop smaller than this
    "boundary_search_min": 6.0,       # how far to search for the local peak/trough
    # Cap on the recovery-heating window. A straight-line rate is only a fair
    # description of the recovery while the curve is still roughly linear; over
    # multi-hour coasts the exponential curvature dominates and the "slope" stops
    # meaning anything. Events whose recovery hits the cap are flagged.
    "max_recovery_min": 60.0,

    # ── Transient / artifact rejection ────────────────────────────────────────
    # A local disturbance (door, sun, body heat, sensor bump) looks like a sharp
    # spike that decays straight back to where it started. A compressor cycle
    # also starts from a fast-rising recovery leg, so rise rate ALONE cannot
    # separate them. The discriminator is what the event leaves behind: real
    # cooling ends meaningfully below the pre-event baseline, a disturbance
    # merely returns to it. Both conditions must hold to reject.
    "artifact_rise_f_per_min": 0.50,   # fast pre-event rise, °F/min
    "artifact_lookback_min": 2.0,      # how far back to look for that rise
    "artifact_baseline_min": 5.0,      # baseline window before the rise
    "artifact_min_net_below_f": 0.75,  # trough must beat baseline by this much

    # ── Thermal time constant ─────────────────────────────────────────────────
    "tau_min_segment_min": 45.0,      # minimum passive (AC-off) segment length to fit
    "tau_min_rise_f": 1.0,            # segment must drift at least this much
    "tau_min_r2": 0.50,               # reject poor fits
    "tau_edge_trim_min": 2.0,         # drop samples adjacent to an AC event
    # A single first-order fit assumes ONE asymptote. A stretch that warms and then
    # cools is being pulled by a moving outdoor temperature, so it is split into
    # monotone runs before fitting; otherwise tau is biased low and R^2 collapses.
    "tau_trend_window_min": 25.0,     # window for the coarse trend used to split
    "tau_min_monotone_min": 45.0,     # minimum length of a monotone run
    # Identifiability: over a window of length L a first-order response closes
    # (1 - e^{-L/tau}) of its gap. If that fraction is small the exponential is
    # indistinguishable from a straight line, tau runs off to infinity and T_inf
    # becomes meaningless — even though R^2 looks excellent. Reject those.
    "tau_min_gap_closed_frac": 0.35,  # segment must span >= ~0.43 tau
    "tau_min_curve_r2": 0.90,

    # ── Setpoint-raise detection (the long afternoon warm-up) ─────────────────
    "setpoint_raise_min_duration_min": 90.0,
    "setpoint_raise_min_rise_f": 4.0,

    # ── Partial-day handling ──────────────────────────────────────────────────
    "full_day_coverage_pct": 90.0,    # below this a day is flagged PARTIAL

    # ── Optional outdoor weather cross-check ──────────────────────────────────
    "weather_enabled": False,
    "latitude": 42.3505,              # Boston, MA (Boston University area)
    "longitude": -71.1054,
    "weather_timeout_s": 15,
}


def load_config(outdir_hint: str) -> dict:
    """Merge DEFAULT_CONFIG with an optional config.json sitting next to this script."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    path = os.path.join(SCRIPT_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path) as fh:
                user = json.load(fh)
            cfg.update(user)
            print(f"[cfg]  Loaded overrides from {path}")
        except Exception as exc:  # pragma: no cover
            print(f"[cfg]  WARNING: could not parse {path}: {exc}. Using defaults.")
    return cfg


# ---- Loading & preprocessing ----

REQUIRED_COLS = {"value", "created_at"}


def discover_inputs(args_paths: list[str]) -> list[str]:
    if args_paths:
        out = []
        for p in args_paths:
            out.extend(sorted(glob.glob(p)) if any(c in p for c in "*?[") else [p])
        return out
    # Auto-discover: any CSV in raw-data/ that looks like a feed export.
    cands = sorted(glob.glob(os.path.join(RAW_DATA_DIR, "*.csv")))
    keep = []
    for c in cands:
        try:
            head = pd.read_csv(c, nrows=1)
        except Exception:
            continue
        if REQUIRED_COLS.issubset(set(head.columns)):
            keep.append(c)
    return keep


def load_data(paths: list[str], tz: str) -> pd.DataFrame:
    frames = []
    for p in paths:
        df = pd.read_csv(p)
        missing = REQUIRED_COLS - set(df.columns)
        if missing:
            print(f"[load] SKIP {os.path.basename(p)} — missing columns {sorted(missing)}")
            continue
        df["_src"] = os.path.basename(p)
        frames.append(df)
        print(f"[load] {os.path.basename(p)}: {len(df)} rows")
    if not frames:
        raise SystemExit("No usable input CSVs found.")

    df = pd.concat(frames, ignore_index=True)

    # De-duplicate across overlapping exports.
    before = len(df)
    if "id" in df.columns:
        df = df.drop_duplicates(subset="id")
    df = df.drop_duplicates(subset=["created_at", "value"])
    if len(df) != before:
        print(f"[load] de-duplicated {before - len(df)} overlapping rows")

    ts = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df = df.assign(ts_utc=ts).dropna(subset=["ts_utc"])
    df["ts"] = df["ts_utc"].dt.tz_convert(tz)
    df["temp_f"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["temp_f"]).sort_values("ts").reset_index(drop=True)

    print(f"[load] {len(df)} usable samples, "
          f"{df['ts'].iloc[0]:%Y-%m-%d %H:%M %Z} -> {df['ts'].iloc[-1]:%Y-%m-%d %H:%M %Z}")
    return df[["ts", "temp_f", "_src"]]


def to_uniform_grid(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Resample onto a uniform grid. Short gaps are interpolated; long gaps stay NaN
    so that slope estimates never bridge a data outage."""
    step = f"{int(cfg['grid_seconds'])}s"
    s = df.set_index("ts")["temp_f"]
    s = s[~s.index.duplicated(keep="first")]
    grid = pd.date_range(s.index[0].ceil(step), s.index[-1].floor(step), freq=step)
    g = s.reindex(s.index.union(grid)).interpolate(method="time").reindex(grid)

    # Blank out anything that fell inside a real outage, so no slope is ever
    # computed across interpolated-from-nothing data.
    gap_limit = pd.Timedelta(minutes=cfg["max_interp_gap_min"])
    obs = s.index
    deltas = obs[1:] - obs[:-1]
    gi = np.flatnonzero(deltas > gap_limit)
    gx = grid.to_numpy()
    bad = np.zeros(len(grid), dtype=bool)
    for i in gi:
        bad |= (gx > obs[i].to_numpy()) & (gx < obs[i + 1].to_numpy())
    g[bad] = np.nan
    if len(gi):
        print(f"[grid] {len(gi)} outage(s) > {cfg['max_interp_gap_min']:g} min "
              f"(longest {deltas.max().total_seconds()/60:.1f} min) left as NaN")

    out = pd.DataFrame({"ts": grid, "temp_f": g.values})
    n_nan = int(out["temp_f"].isna().sum())
    if n_nan:
        print(f"[grid] {len(out)} grid points, {n_nan} blanked by gaps "
              f"(> {cfg['max_interp_gap_min']:g} min)")
    else:
        print(f"[grid] {len(out)} grid points on a {cfg['grid_seconds']}s grid, no long gaps")
    return out


def smooth_and_slope(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Median-filter, rolling-mean smooth, then compute a centered least-squares slope.

    The slope uses a closed-form OLS on a fixed-width window:
        slope = sum_i w_i * T_i,   w_i = (t_i - tbar) / sum_j (t_j - tbar)^2
    which is exact and far faster than rolling.apply.
    """
    dt_min = cfg["grid_seconds"] / 60.0

    med = max(1, int(cfg["median_filter_samples"]) | 1)  # force odd
    t = df["temp_f"].rolling(med, center=True, min_periods=1).median()

    sm_n = max(1, int(round(cfg["smooth_window_min"] / dt_min)))
    t = t.rolling(sm_n, center=True, min_periods=max(1, sm_n // 2)).mean()
    df["temp_smooth"] = t

    n = max(3, int(round(cfg["slope_window_min"] / dt_min)))
    if n % 2 == 0:
        n += 1
    tt = (np.arange(n) - (n - 1) / 2.0) * dt_min          # minutes, centered
    w = tt / np.sum(tt ** 2)                               # OLS weights -> degF/min

    vals = t.to_numpy(dtype=float)
    slope = np.full(vals.shape, np.nan)
    half = n // 2
    if len(vals) > n:
        # Sliding windows; any window containing a NaN yields NaN (correct behaviour).
        win = np.lib.stride_tricks.sliding_window_view(vals, n)
        slope[half:len(vals) - half] = win @ w
    df["slope_f_per_min"] = slope
    return df


# ---- Day segmentation & period assignment ----

def assign_days(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Label each sample with the 3AM-to-3AM analysis day it belongs to.

    A sample at 01:30 on Jul 29 belongs to analysis day 'Jul 28' (the 03:00 Jul 28 ->
    03:00 Jul 29 window)."""
    h = cfg["day_start_hour"]
    shifted = df["ts"] - pd.Timedelta(hours=h)
    df["analysis_day"] = shifted.dt.date
    return df


def period_of(hour_float: float, periods: dict) -> str:
    for name, (a, b) in periods.items():
        if a <= b:
            if a <= hour_float < b:
                return name
        else:  # wraps midnight
            if hour_float >= a or hour_float < b:
                return name
    return "unassigned"


def add_period(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    hf = df["ts"].dt.hour + df["ts"].dt.minute / 60.0 + df["ts"].dt.second / 3600.0
    df["period"] = [period_of(x, cfg["periods"]) for x in hf]
    return df


# ---- AC event detection ----

@dataclass
class ACEvent:
    analysis_day: str
    period: str
    start: pd.Timestamp          # local peak — compressor start
    trough: pd.Timestamp         # local minimum — compressor stop
    recovery_end: pd.Timestamp   # next compressor start (or end of passive window)
    t_start_f: float
    t_trough_f: float
    t_recovery_end_f: float
    cool_duration_min: float
    cool_drop_f: float
    cool_slope_f_per_min: float
    cool_r2: float
    heat_duration_min: float
    heat_rise_f: float
    heat_slope_f_per_min: float
    heat_r2: float
    heat_capped: bool
    is_artifact: bool
    artifact_reason: str
    pre_rise_f_per_min: float
    pre_baseline_f: float
    net_below_baseline_f: float
    confidence: str = "high"


def _ols(x_min: np.ndarray, y: np.ndarray) -> tuple[float, float, float]:
    """Return (slope, intercept, r2). x in minutes, y in degF."""
    ok = np.isfinite(x_min) & np.isfinite(y)
    if ok.sum() < 3:
        return (np.nan, np.nan, np.nan)
    x, y = x_min[ok], y[ok]
    xm, ym = x.mean(), y.mean()
    sxx = np.sum((x - xm) ** 2)
    if sxx <= 0:
        return (np.nan, np.nan, np.nan)
    b = np.sum((x - xm) * (y - ym)) / sxx
    a = ym - b * xm
    ss_res = np.sum((y - (a + b * x)) ** 2)
    ss_tot = np.sum((y - ym) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan
    return (b, a, r2)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs as inclusive (start, end) index pairs."""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return []
    d = np.diff(m.astype(np.int8))
    starts = list(np.flatnonzero(d == 1) + 1)
    ends = list(np.flatnonzero(d == -1))
    if m[0]:
        starts = [0] + starts
    if m[-1]:
        ends = ends + [len(m) - 1]
    return list(zip(starts, ends))


def detect_events(day: pd.DataFrame, cfg: dict, day_label: str) -> list[ACEvent]:
    dt_min = cfg["grid_seconds"] / 60.0
    n = len(day)
    if n < 10:
        return []

    ts = day["ts"].to_numpy()
    T = day["temp_smooth"].to_numpy(dtype=float)
    slope = day["slope_f_per_min"].to_numpy(dtype=float)
    tmin = (day["ts"] - day["ts"].iloc[0]).dt.total_seconds().to_numpy() / 60.0

    cooling = slope < -cfg["cool_slope_on"]
    cooling = np.nan_to_num(cooling, nan=False)

    runs = _runs(cooling)

    # Merge runs separated by a short interruption.
    merge_gap = cfg["merge_gap_min"]
    merged: list[list[int]] = []
    for s, e in runs:
        if merged and (tmin[s] - tmin[merged[-1][1]]) <= merge_gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])

    search = int(round(cfg["boundary_search_min"] / dt_min))
    events: list[ACEvent] = []

    # How much the slope must still be steepening (°F/min per sample) for the
    # walk-back to keep going. Below this we have reached the knee.
    knee_eps = 0.01

    for s, e in merged:
        # ── Refine the START to the "knee": the point where the trace stops
        # drifting and the compressor visibly grabs it. Taking a plain argmax
        # over a lookback window is wrong here — during the afternoon coast the
        # trace is already gently falling, so the window maximum sits at its far
        # edge and the fitted cooling rate comes out biased shallow.
        lo = max(0, s - search)
        i_peak = s
        while i_peak > lo:
            p, c = slope[i_peak - 1], slope[i_peak]
            if not (np.isfinite(p) and np.isfinite(c)):
                break
            if p >= 0:                      # reached the warming/recovery leg
                break
            if (p - c) <= knee_eps:         # slope has stopped steepening
                break
            if T[i_peak - 1] < T[i_peak]:   # went past a local minimum
                break
            i_peak -= 1

        # ── Refine the END to the nearest local minimum (well-defined: the
        # recovery leg turns the trace around sharply).
        hi = min(n - 1, e + search)
        i_trough = e
        while i_trough < hi and np.isfinite(T[i_trough + 1]) and T[i_trough + 1] < T[i_trough]:
            i_trough += 1

        if i_trough <= i_peak:
            continue

        dur = tmin[i_trough] - tmin[i_peak]
        drop = T[i_peak] - T[i_trough]
        if dur < cfg["min_cool_duration_min"] or drop < cfg["min_drop_f"]:
            continue

        b_c, _, r2_c = _ols(tmin[i_peak:i_trough + 1], T[i_peak:i_trough + 1])

        # ── Transient / artifact test ───────────────────────────────────────
        # (a) was the room warming implausibly fast just before the peak, and
        # (b) did the cooling fail to leave it below its pre-event baseline?
        # Both must hold. Condition (a) alone would also reject genuine cycles
        # that start off a fast post-cycle recovery leg.
        n_lb = int(round(cfg["artifact_lookback_min"] / dt_min))
        n_bl = int(round(cfg["artifact_baseline_min"] / dt_min))
        lb = max(0, i_peak - n_lb)
        pre_rise = np.nan
        if i_peak - lb >= 2:
            pre_rise, _, _ = _ols(tmin[lb:i_peak + 1], T[lb:i_peak + 1])
            # Also take the steepest short-window rate inside the lookback, so a
            # narrow spike is not diluted by averaging over the whole window.
            w = slope[lb:i_peak + 1]
            if np.isfinite(w).any():
                pre_rise = max(pre_rise, float(np.nanmax(w)))

        b0 = max(0, lb - n_bl)
        baseline = float(np.nanmedian(T[b0:lb])) if lb - b0 >= 3 else np.nan
        net_below = baseline - T[i_trough] if np.isfinite(baseline) else np.nan

        fast_rise = bool(np.isfinite(pre_rise) and pre_rise > cfg["artifact_rise_f_per_min"])
        no_net_cooling = bool(np.isfinite(net_below)
                              and net_below < cfg["artifact_min_net_below_f"])
        short_history = bool(not np.isfinite(baseline))

        is_art = (fast_rise and no_net_cooling) or (short_history and fast_rise)
        if is_art and short_history:
            reason = (f"spike of {pre_rise:+.2f} °F/min with no pre-event baseline "
                      f"(too close to the start of the record to verify)")
        elif is_art:
            reason = (f"spike of {pre_rise:+.2f} °F/min then returned to baseline "
                      f"(trough only {net_below:+.2f} °F below the pre-event "
                      f"{baseline:.2f} °F) — local disturbance, not a compressor cycle")
        else:
            reason = ""

        events.append(ACEvent(
            analysis_day=day_label,
            period=period_of(pd.Timestamp(ts[i_peak]).hour
                             + pd.Timestamp(ts[i_peak]).minute / 60.0, cfg["periods"]),
            start=pd.Timestamp(ts[i_peak]), trough=pd.Timestamp(ts[i_trough]),
            recovery_end=pd.NaT,
            t_start_f=float(T[i_peak]), t_trough_f=float(T[i_trough]),
            t_recovery_end_f=np.nan,
            cool_duration_min=float(dur), cool_drop_f=float(drop),
            cool_slope_f_per_min=float(b_c), cool_r2=float(r2_c),
            heat_duration_min=np.nan, heat_rise_f=np.nan,
            heat_slope_f_per_min=np.nan, heat_r2=np.nan, heat_capped=False,
            is_artifact=is_art, artifact_reason=reason,
            pre_rise_f_per_min=float(pre_rise) if np.isfinite(pre_rise) else np.nan,
            pre_baseline_f=float(baseline) if np.isfinite(baseline) else np.nan,
            net_below_baseline_f=float(net_below) if np.isfinite(net_below) else np.nan,
            # Kept but flagged: a shallow cycle that barely dips below its own
            # baseline is real-looking but not separable from a disturbance on
            # this data alone. Surfaced rather than silently decided.
            confidence=("low" if (no_net_cooling and not is_art) else "high"),
        ))

    # Recovery (passive heating) leg: trough -> next event's peak.
    idx_of = {pd.Timestamp(t): i for i, t in enumerate(ts)}
    for k, ev in enumerate(events):
        i_tr = idx_of[ev.trough]
        if k + 1 < len(events):
            i_end = idx_of[events[k + 1].start]
        else:
            i_end = n - 1
        cap = int(round(cfg["max_recovery_min"] / dt_min))
        capped = i_end > i_tr + cap
        i_end = min(i_end, i_tr + cap)
        if i_end - i_tr < 3:
            continue
        ev.heat_capped = bool(capped)
        b_h, _, r2_h = _ols(tmin[i_tr:i_end + 1], T[i_tr:i_end + 1])
        ev.recovery_end = pd.Timestamp(ts[i_end])
        ev.t_recovery_end_f = float(T[i_end])
        ev.heat_duration_min = float(tmin[i_end] - tmin[i_tr])
        ev.heat_rise_f = float(T[i_end] - T[i_tr])
        ev.heat_slope_f_per_min = float(b_h)
        ev.heat_r2 = float(r2_h)

    return events


# ---- Setpoint-raise detection (the long afternoon coast to ~86F) ----

def detect_setpoint_raise(day: pd.DataFrame, events: list[ACEvent], cfg: dict):
    """Find the longest AC-free warming stretch — the window where the thermostat was
    parked near 86F and the apartment simply coasted upward."""
    real = [e for e in events if not e.is_artifact]
    ts = day["ts"]
    busy = pd.Series(False, index=day.index)
    for e in real:
        busy |= (ts >= e.start) & (ts <= e.trough)
    free = (~busy).to_numpy() & np.isfinite(day["temp_smooth"].to_numpy())
    dt_min = cfg["grid_seconds"] / 60.0
    best = None
    for s, e in _runs(free):
        dur = (e - s) * dt_min
        if dur < cfg["setpoint_raise_min_duration_min"]:
            continue
        rise = day["temp_smooth"].iloc[e] - day["temp_smooth"].iloc[s]
        if rise < cfg["setpoint_raise_min_rise_f"]:
            continue
        # Score by how far the apartment was allowed to drift, not by duration:
        # a long flat overnight stretch is not a setpoint raise.
        if best is None or rise > best["rise_f"]:
            best = {
                "start": ts.iloc[s], "end": ts.iloc[e],
                "duration_min": dur, "rise_f": float(rise),
                "t_start_f": float(day["temp_smooth"].iloc[s]),
                "t_end_f": float(day["temp_smooth"].iloc[e]),
                "peak_f": float(day["temp_smooth"].iloc[s:e + 1].max()),
            }
    return best


# ---- Thermal time constant ----
#
# Newton / first-order lumped-capacitance model for the indoor air with the HVAC off:
#
#       dT/dt = (T_inf - T) / tau
#
# Rewritten as a plain linear regression of the measured rate on the measured
# temperature:
#
#       dT/dt = a + b*T,      b = -1/tau,      T_inf = -a/b
#
# This recovers BOTH the time constant and the effective driving (asymptote)
# temperature from indoor data alone — no outdoor sensor, no nonlinear solver,
# no scipy. tau is the e-folding time: the apartment closes ~63% of the gap to
# T_inf in one tau.
#
# With outdoor temperature available the same model is fit through the origin on
# the driving potential:  dT/dt = (T_out - T)/tau, giving a physically anchored tau.

@dataclass
class TauFit:
    method: str
    analysis_day: str
    label: str
    tau_hours: float
    t_inf_f: float
    t0_f: float
    r2: float
    n_samples: int
    duration_min: float
    start: pd.Timestamp
    end: pd.Timestamp
    gap_closed_frac: float = np.nan   # 1 - exp(-duration/tau); identifiability
    accepted: bool = False
    note: str = ""


def passive_segments(day: pd.DataFrame, events: list[ACEvent], cfg: dict):
    """AC-free stretches long enough and warm-trending enough to fit."""
    real = [e for e in events if not e.is_artifact]
    ts = day["ts"]
    busy = pd.Series(False, index=day.index)
    trim = pd.Timedelta(minutes=cfg["tau_edge_trim_min"])
    for e in real:
        busy |= (ts >= e.start - trim) & (ts <= e.trough + trim)
    # Artifacts are local disturbances — exclude their neighbourhood too.
    for e in events:
        if e.is_artifact:
            busy |= (ts >= e.start - trim) & (ts <= e.trough + trim)

    free = (~busy).to_numpy() & np.isfinite(day["temp_smooth"].to_numpy())
    dt_min = cfg["grid_seconds"] / 60.0

    # Coarse trend, used only to split a passive stretch into monotone runs.
    n_tr = max(3, int(round(cfg["tau_trend_window_min"] / dt_min)))
    trend = (day["temp_smooth"].rolling(n_tr, center=True, min_periods=n_tr // 2)
             .mean().diff().to_numpy())

    segs = []
    for s, e in _runs(free):
        if (e - s) * dt_min < cfg["tau_min_segment_min"]:
            continue
        sign = np.sign(np.nan_to_num(trend[s:e + 1]))
        # Absorb momentary sign flips by taking the sign of a smoothed indicator.
        sign = np.sign(pd.Series(sign).rolling(n_tr, center=True, min_periods=1)
                       .mean().to_numpy())
        for a, b in _runs(sign > 0) + _runs(sign < 0):
            i0, i1 = s + a, s + b
            dur = (i1 - i0) * dt_min
            if dur < cfg["tau_min_monotone_min"]:
                continue
            rise = day["temp_smooth"].iloc[i1] - day["temp_smooth"].iloc[i0]
            if abs(rise) < cfg["tau_min_rise_f"]:
                continue
            segs.append((i0, i1, dur, float(rise)))
    segs.sort(key=lambda x: x[0])
    return segs


METHOD_CURVE = "indoor curve fit  T(t)=T∞−(T∞−T0)e^(−t/τ)"
METHOD_RATE = "indoor rate regression  dT/dt = a + b·T"


def _curve_fit_tau(t_min: np.ndarray, T: np.ndarray):
    """Least-squares fit of the first-order response, without scipy.

    For a FIXED tau the model is linear in (T_inf, T0):
        T(t) = T_inf*(1 - e^{-t/tau}) + T0*e^{-t/tau}
    so we solve those two coefficients in closed form on a log-spaced grid of tau
    and keep the grid point with the lowest residual sum of squares. Fitting the
    temperature CURVE rather than its derivative is far better conditioned: the
    derivative of a slowly-drifting signal is dominated by sensor noise, which is
    why the rate regression alone gives an unreliable tau.
    """
    ok = np.isfinite(t_min) & np.isfinite(T)
    t, y = t_min[ok], T[ok]
    if len(t) < 20:
        return (np.nan,) * 4
    t = t - t[0]
    span = t[-1]
    taus = np.exp(np.linspace(np.log(max(2.0, span / 50)), np.log(span * 12), 700))
    best = None
    for tau in taus:
        e = np.exp(-t / tau)
        X = np.column_stack([1.0 - e, e])
        try:
            c, *_ = np.linalg.lstsq(X, y, rcond=None)
        except np.linalg.LinAlgError:
            continue
        r = y - X @ c
        sse = float(r @ r)
        if best is None or sse < best[0]:
            best = (sse, tau, float(c[0]), float(c[1]))
    if best is None:
        return (np.nan,) * 4
    sse, tau, t_inf, t0 = best
    sst = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - sse / sst if sst > 0 else np.nan
    return tau / 60.0, t_inf, t0, r2


def fit_tau_indoor(day: pd.DataFrame, segs, cfg: dict, day_label: str) -> list[TauFit]:
    dt_min = cfg["grid_seconds"] / 60.0
    n_tr = max(3, int(round(cfg["tau_trend_window_min"] / dt_min)))
    # A wide-window rate for the tau regression. The 2.5-min detection slope is far
    # too noisy for a drift of ~0.05 F/min.
    rate_wide = (day["temp_smooth"].rolling(n_tr, center=True, min_periods=n_tr // 2)
                 .mean().diff().to_numpy() / dt_min)

    out = []
    for k, (s, e, dur, rise) in enumerate(segs, 1):
        sub = day.iloc[s:e + 1]
        label = f"{'warming' if rise > 0 else 'cooling'} run {k}"
        tmin = (sub["ts"] - sub["ts"].iloc[0]).dt.total_seconds().to_numpy() / 60.0
        T = sub["temp_smooth"].to_numpy(dtype=float)

        # ── Primary: fit the temperature curve itself ────────────────────────
        tau_h, t_inf, t0, r2 = _curve_fit_tau(tmin, T)
        if np.isfinite(tau_h):
            frac = 1.0 - math.exp(-(dur / 60.0) / tau_h) if tau_h > 0 else np.nan
            notes = []
            ok = True
            if frac < cfg["tau_min_gap_closed_frac"]:
                ok = False
                notes.append(
                    f"REJECTED — segment only closes {frac*100:.0f}% of the gap "
                    f"(τ={tau_h:.1f} h ≫ {dur/60:.1f} h window); the response is still "
                    f"linear here so τ and T∞ are not separately identifiable")
            if r2 < cfg["tau_min_curve_r2"]:
                ok = False
                notes.append(f"REJECTED — R²={r2:.3f} below {cfg['tau_min_curve_r2']:.2f}")
            if not (0.05 <= tau_h <= 48):
                ok = False
                notes.append("REJECTED — τ outside plausible 0.05–48 h range")
            if ok and abs(t0 - T[np.isfinite(T)][0]) > 1.5:
                notes.append(
                    f"fitted T0={t0:.1f} vs measured {T[np.isfinite(T)][0]:.1f} °F — "
                    "fast initial transient (air vs thermal mass: 2-pole behaviour)")
            out.append(TauFit(
                method=METHOD_CURVE, analysis_day=day_label, label=label,
                tau_hours=float(tau_h), t_inf_f=float(t_inf), t0_f=float(t0), r2=float(r2),
                n_samples=int(np.isfinite(T).sum()), duration_min=float(dur),
                start=sub["ts"].iloc[0], end=sub["ts"].iloc[-1],
                gap_closed_frac=float(frac), accepted=ok, note="; ".join(notes)))

        # ── Secondary: Newton-cooling rate regression (independent check) ────
        b, a, r2r = _ols(T, rate_wide[s:e + 1])
        if np.isfinite(b) and b < 0:
            tau_r = (-1.0 / b) / 60.0
            frac_r = 1.0 - math.exp(-(dur / 60.0) / tau_r) if tau_r > 0 else np.nan
            ok = (r2r >= cfg["tau_min_r2"] and 0.05 <= tau_r <= 48
                  and frac_r >= cfg["tau_min_gap_closed_frac"])
            note = "" if ok else (
                "REJECTED — low R² (derivative noise dominates)" if r2r < cfg["tau_min_r2"]
                else "REJECTED — τ not identifiable over this window")
            out.append(TauFit(
                method=METHOD_RATE, analysis_day=day_label, label=label,
                tau_hours=float(tau_r), t_inf_f=float(-a / b),
                t0_f=float(T[np.isfinite(T)][0]), r2=float(r2r),
                n_samples=int(np.isfinite(T).sum()), duration_min=float(dur),
                start=sub["ts"].iloc[0], end=sub["ts"].iloc[-1],
                gap_closed_frac=float(frac_r), accepted=bool(ok), note=note))
    return out


def fit_tau_weather(day: pd.DataFrame, segs, outdoor: pd.Series | None,
                    cfg: dict, day_label: str) -> list[TauFit]:
    if outdoor is None or outdoor.empty:
        return []
    out = []
    for k, (s, e, dur, rise) in enumerate(segs, 1):
        sub = day.iloc[s:e + 1].copy()
        tout = outdoor.reindex(outdoor.index.union(sub["ts"])).interpolate(
            method="time").reindex(sub["ts"])
        drive = tout.to_numpy(dtype=float) - sub["temp_smooth"].to_numpy(dtype=float)
        dTdt = sub["slope_f_per_min"].to_numpy(dtype=float)
        b, a, r2 = _ols(drive, dTdt)
        if not np.isfinite(b) or b <= 0:
            continue
        tau_h = (1.0 / b) / 60.0
        out.append(TauFit(
            method="weather-anchored (dT/dt vs Tout-T)", analysis_day=day_label,
            label=f"{'warming' if rise > 0 else 'cooling'} run {k}", tau_hours=float(tau_h),
            t_inf_f=float(np.nanmean(tout.to_numpy(dtype=float))),
            t0_f=float(sub["temp_smooth"].iloc[0]), r2=float(r2),
            n_samples=int(np.isfinite(drive).sum()), duration_min=float(dur),
            start=sub["ts"].iloc[0], end=sub["ts"].iloc[-1],
            gap_closed_frac=float(1.0 - math.exp(-(dur / 60.0) / tau_h)) if tau_h > 0 else np.nan,
            accepted=bool(r2 >= cfg["tau_min_r2"] and 0.05 <= tau_h <= 48),
            note=f"intercept {a:+.4f} °F/min = residual internal heat gain"))
    return out


def fetch_outdoor(df: pd.DataFrame, cfg: dict) -> pd.Series | None:
    """Optional Open-Meteo hourly outdoor temperature. Fails soft, always."""
    if not cfg.get("weather_enabled"):
        return None
    try:
        import urllib.request
        import urllib.parse
        tz = cfg["timezone"]
        d0 = df["ts"].iloc[0].date()
        d1 = df["ts"].iloc[-1].date()
        base_q = {
            "latitude": cfg["latitude"], "longitude": cfg["longitude"],
            "hourly": "temperature_2m", "temperature_unit": "fahrenheit",
            "timezone": tz,
        }
        span_days = (pd.Timestamp(d1) - pd.Timestamp(d0)).days + 2
        urls = [
            # Recent data lives on the forecast endpoint (archive lags ~5 days).
            "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
                {**base_q, "past_days": min(92, max(2, span_days)), "forecast_days": 1}),
            "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(
                {**base_q, "start_date": str(d0), "end_date": str(d1)}),
        ]
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=cfg["weather_timeout_s"]) as r:
                    payload = json.loads(r.read().decode())
                h = payload.get("hourly") or {}
                times, temps = h.get("time"), h.get("temperature_2m")
                if not times or not temps:
                    continue
                s = pd.Series(temps, index=pd.to_datetime(times).tz_localize(tz),
                              dtype="float64").dropna()
                s = s[(s.index >= df["ts"].iloc[0] - pd.Timedelta(hours=2)) &
                      (s.index <= df["ts"].iloc[-1] + pd.Timedelta(hours=2))]
                if len(s) >= 3:
                    print(f"[wx]   Outdoor temperature: {len(s)} hourly points "
                          f"({s.min():.1f}-{s.max():.1f} F)")
                    return s
            except Exception:
                continue
        print("[wx]   Outdoor temperature unavailable — indoor-only tau reported.")
    except Exception as exc:
        print(f"[wx]   Weather lookup skipped ({exc}).")
    return None


# ---- Plotting ----

PERIOD_COLORS = {"night": "#2c3e70", "morning": "#e8a33d",
                 "afternoon": "#c0392b", "evening": "#7d3c98"}


def _shade_periods(ax, cfg, x0_hours=0.0):
    """Shade period bands on an axis whose x-axis is 'hours since day start'."""
    h0 = cfg["day_start_hour"]
    for name, (a, b) in cfg["periods"].items():
        spans = []
        if a <= b:
            spans.append((a, b))
        else:
            spans.append((a, 24))
            spans.append((0, b))
        for (sa, sb) in spans:
            xa = (sa - h0) % 24
            xb = (sb - h0) % 24
            if xb == 0:
                xb = 24
            if xb < xa:
                ax.axvspan(xa, 24, color=PERIOD_COLORS[name], alpha=0.07, lw=0)
                ax.axvspan(0, xb, color=PERIOD_COLORS[name], alpha=0.07, lw=0)
            else:
                ax.axvspan(xa, xb, color=PERIOD_COLORS[name], alpha=0.07, lw=0)


def plot_full_timeseries(df, all_events, cfg, path):
    fig, ax = plt.subplots(figsize=(16, 5.5))
    ax.plot(df["ts"], df["temp_f"], lw=0.6, color="#b0b7c3", label="raw (published EMA)")
    ax.plot(df["ts"], df["temp_smooth"], lw=1.2, color="#1f4e79", label="smoothed")
    for ev in all_events:
        c = "#c0392b" if not ev.is_artifact else "#999999"
        ax.axvspan(ev.start, ev.trough, color=c, alpha=0.30 if not ev.is_artifact else 0.18, lw=0)
    h = cfg["day_start_hour"]
    for d in sorted(df["analysis_day"].unique()):
        bnd = pd.Timestamp(d, tz=cfg["timezone"]) + pd.Timedelta(hours=h)
        ax.axvline(bnd, color="k", ls="--", lw=1.0, alpha=0.6)
        ax.text(bnd, ax.get_ylim()[1], f" day start {h:02d}:00", fontsize=8,
                va="top", rotation=90, alpha=0.7)
    ax.set_ylabel("Temperature (°F)")
    ax.set_title("Full record — shaded bands are detected AC cooling events "
                 "(grey = rejected transient)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d\n%H:%M", tz=df["ts"].dt.tz))
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def plot_days_overlaid(df, cfg, path):
    fig, axes = plt.subplots(2, 1, figsize=(14, 9), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]})
    ax, ax2 = axes
    h0 = cfg["day_start_hour"]
    days = sorted(df["analysis_day"].unique())
    cmap = plt.get_cmap("viridis", max(2, len(days)))
    for i, d in enumerate(days):
        sub = df[df["analysis_day"] == d]
        x = ((sub["ts"] - (pd.Timestamp(d, tz=cfg["timezone"])
                           + pd.Timedelta(hours=h0))).dt.total_seconds() / 3600.0)
        ax.plot(x, sub["temp_f"], lw=1.1, color=cmap(i), label=str(d))
        ax2.plot(x, sub["slope_f_per_min"], lw=0.8, color=cmap(i))
    _shade_periods(ax, cfg)
    _shade_periods(ax2, cfg)
    ax2.axhline(0, color="k", lw=0.7)
    ax2.axhline(-cfg["cool_slope_on"], color="#c0392b", ls="--", lw=1.0,
                label=f"AC-on threshold ({-cfg['cool_slope_on']:+.2f} °F/min)")
    ax.set_ylabel("Temperature (°F)")
    ax2.set_ylabel("dT/dt (°F/min)")
    ax2.set_xlabel(f"Hours since {h0:02d}:00 local")
    ax.set_title("Analysis days overlaid (03:00 → 03:00 local). "
                 "Shaded bands = time-of-day periods")
    ax.legend(title="analysis day", fontsize=9)
    ax2.legend(fontsize=9)
    for a in axes:
        a.grid(alpha=0.25)
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 2))
    ax.set_xticklabels([f"{(h0 + t) % 24:02d}:00" for t in range(0, 25, 2)])
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def plot_day_diagnostic(day, events, setpoint, taufits, cfg, day_label, path):
    fig, axes = plt.subplots(2, 1, figsize=(16, 9), sharex=True,
                             gridspec_kw={"height_ratios": [2.2, 1]})
    ax, ax2 = axes
    ax.plot(day["ts"], day["temp_f"], lw=0.6, color="#c8ccd4", label="raw")
    ax.plot(day["ts"], day["temp_smooth"], lw=1.3, color="#1f4e79", label="smoothed")

    for ev in events:
        if ev.is_artifact:
            ax.axvspan(ev.start, ev.trough, color="#9e9e9e", alpha=0.25, lw=0)
            ax.plot([ev.start], [ev.t_start_f], marker="x", color="#555", ms=9)
            continue
        ax.axvspan(ev.start, ev.trough, color="#c0392b", alpha=0.22, lw=0)
        # Draw the fitted cooling leg.
        xs = [ev.start, ev.trough]
        ax.plot(xs, [ev.t_start_f, ev.t_start_f + ev.cool_slope_f_per_min * ev.cool_duration_min],
                color="#c0392b", lw=2.0)
        if pd.notna(ev.recovery_end):
            ax.plot([ev.trough, ev.recovery_end],
                    [ev.t_trough_f,
                     ev.t_trough_f + ev.heat_slope_f_per_min * ev.heat_duration_min],
                    color="#e67e22", lw=2.0)

    if setpoint:
        ax.axvspan(setpoint["start"], setpoint["end"], color="#f1c40f", alpha=0.15, lw=0)
        ax.annotate(f"setpoint-raise coast: {setpoint['duration_min']/60:.1f} h, "
                    f"+{setpoint['rise_f']:.1f} °F → peak {setpoint['peak_f']:.1f} °F",
                    xy=(setpoint["start"] + (setpoint["end"] - setpoint["start"]) / 2,
                        0.02), xycoords=("data", "axes fraction"),
                    fontsize=9, ha="center", va="bottom",
                    bbox=dict(fc="#fef9e7", ec="#f1c40f", alpha=0.95))

    from matplotlib.lines import Line2D
    ax.legend(handles=[
        Line2D([], [], color="#1f4e79", lw=1.3, label="smoothed temperature"),
        Line2D([], [], color="#c0392b", lw=2.0, label="fitted cooling slope (AC on)"),
        Line2D([], [], color="#e67e22", lw=2.0, label="fitted heating slope (recovery)"),
        Line2D([], [], color="#9e9e9e", lw=6, alpha=.4, label="rejected transient"),
    ], fontsize=9, loc="upper left")

    ax.set_ylabel("Temperature (°F)")
    n_real = sum(1 for e in events if not e.is_artifact)
    ax.set_title(f"Analysis day {day_label} (03:00→03:00 local) — "
                 f"{n_real} AC events detected")

    ax2.plot(day["ts"], day["slope_f_per_min"], lw=0.9, color="#34495e")
    ax2.axhline(0, color="k", lw=0.7)
    ax2.axhline(-cfg["cool_slope_on"], color="#c0392b", ls="--", lw=1.0)
    ax2.fill_between(day["ts"], -cfg["cool_slope_on"], day["slope_f_per_min"],
                     where=day["slope_f_per_min"] < -cfg["cool_slope_on"],
                     color="#c0392b", alpha=0.35, interpolate=True)
    ax2.set_ylabel("dT/dt (°F/min)")
    ax2.set_xlabel("local time")
    ax2.text(0.005, 0.05, f"detection threshold {-cfg['cool_slope_on']:+.2f} °F/min",
             transform=ax2.transAxes, fontsize=8, color="#c0392b")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=day["ts"].dt.tz))
    ax2.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    for a in axes:
        a.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def plot_cycle_detail(day, events, cfg, day_label, path):
    """Zoom on the deepest real cycle to show exactly how the slopes are measured."""
    real = [e for e in events if not e.is_artifact and pd.notna(e.recovery_end)]
    if not real:
        return False
    ev = max(real, key=lambda e: e.cool_drop_f)
    pad = pd.Timedelta(minutes=8)
    sub = day[(day["ts"] >= ev.start - pad) & (day["ts"] <= ev.recovery_end + pad)]
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(sub["ts"], sub["temp_f"], "o", ms=2.5, color="#b0b7c3", label="raw samples")
    ax.plot(sub["ts"], sub["temp_smooth"], lw=1.6, color="#1f4e79", label="smoothed")

    ax.plot([ev.start, ev.trough],
            [ev.t_start_f, ev.t_start_f + ev.cool_slope_f_per_min * ev.cool_duration_min],
            color="#c0392b", lw=2.5,
            label=f"cooling fit  {ev.cool_slope_f_per_min:+.3f} °F/min (R²={ev.cool_r2:.3f})")
    ax.plot([ev.trough, ev.recovery_end],
            [ev.t_trough_f, ev.t_trough_f + ev.heat_slope_f_per_min * ev.heat_duration_min],
            color="#e67e22", lw=2.5,
            label=f"heating fit  {ev.heat_slope_f_per_min:+.3f} °F/min (R²={ev.heat_r2:.3f})")
    ax.plot([ev.start], [ev.t_start_f], "v", ms=11, color="#c0392b", zorder=5)
    ax.plot([ev.trough], [ev.t_trough_f], "^", ms=11, color="#e67e22", zorder=5)
    ax.annotate("compressor ON\n(knee)", xy=(ev.start, ev.t_start_f),
                xytext=(-70, 14), textcoords="offset points", ha="center", fontsize=9,
                arrowprops=dict(arrowstyle="->", color="#c0392b"))
    ax.annotate("compressor OFF\n(local trough)", xy=(ev.trough, ev.t_trough_f),
                xytext=(0, -38), textcoords="offset points", ha="center", fontsize=9,
                arrowprops=dict(arrowstyle="->", color="#e67e22"))
    ax.annotate("", xy=(ev.start, ev.t_trough_f), xytext=(ev.start, ev.t_start_f),
                arrowprops=dict(arrowstyle="<->", color="#555"))
    ax.text(ev.start, (ev.t_start_f + ev.t_trough_f) / 2, f"  Δ {ev.cool_drop_f:.2f} °F",
            fontsize=9, va="center")
    ax.set_title(f"{day_label} — anatomy of the deepest AC cycle "
                 f"({ev.period}, {ev.start:%H:%M})")
    ax.set_ylabel("Temperature (°F)")
    ax.set_xlabel("local time")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=day["ts"].dt.tz))
    ax.grid(alpha=0.25)
    ax.legend(fontsize=9, loc="best")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def plot_period_slopes(events_df, cfg, path):
    real = events_df[~events_df["is_artifact"]]
    if real.empty:
        return False
    order = [p for p in ["night", "morning", "afternoon", "evening"]
             if p in set(real["period"])]
    if not order:
        return False
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    for ax, col, title, color in [
        (axes[0], "cool_slope_f_per_min", "Cooling slope (AC on)", "#c0392b"),
        (axes[1], "heat_slope_f_per_min", "Heating slope (recovery)", "#e67e22"),
    ]:
        data = [real.loc[real["period"] == p, col].dropna().to_numpy() for p in order]
        data = [d for d in data]
        bp = ax.boxplot(data, labels=order, patch_artist=True, widths=0.55)
        for b in bp["boxes"]:
            b.set(facecolor=color, alpha=0.35)
        for i, d in enumerate(data, 1):
            if len(d):
                ax.plot(np.full(len(d), i) + np.random.uniform(-.10, .10, len(d)), d,
                        "o", ms=4, color=color, alpha=0.75)
        ax.axhline(0, color="k", lw=0.7)
        ax.set_title(title)
        ax.set_ylabel("°F/min")
        ax.grid(alpha=0.25, axis="y")

    ax = axes[2]
    counts = real.groupby(["analysis_day", "period"]).size().unstack(fill_value=0)
    counts = counts.reindex(columns=order, fill_value=0)
    bottom = np.zeros(len(counts))
    for p in order:
        ax.bar(range(len(counts)), counts[p].to_numpy(), bottom=bottom,
               label=p, color=PERIOD_COLORS.get(p, "#888"))
        bottom += counts[p].to_numpy()
    ax.set_xticks(range(len(counts)))
    ax.set_xticklabels([str(i) for i in counts.index], rotation=20, fontsize=8)
    ax.set_title("AC events per day by period")
    ax.set_ylabel("event count")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25, axis="y")
    fig.suptitle("Cooling and recovery rates by time-of-day period", fontsize=13)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def plot_tau(day, segs, fits, cfg, day_label, path):
    fits_in = [f for f in fits if f.method == METHOD_CURVE]
    if not segs or not fits_in:
        return False
    n = min(len(segs), len(fits_in))
    fig, axes = plt.subplots(2, n, figsize=(6.0 * n, 8.5), squeeze=False)
    for j in range(n):
        s, e, dur, rise = segs[j]
        f = fits_in[j]
        sub = day.iloc[s:e + 1]
        tmin = (sub["ts"] - sub["ts"].iloc[0]).dt.total_seconds().to_numpy() / 60.0
        T = sub["temp_smooth"].to_numpy(dtype=float)

        # Top: measured curve + fitted first-order exponential.
        ax = axes[0][j]
        ax.plot(tmin / 60.0, T, lw=1.5, color="#1f4e79", label="measured")
        tau_min = f.tau_hours * 60.0
        model = f.t_inf_f - (f.t_inf_f - f.t0_f) * np.exp(-tmin / tau_min)
        col = "#c0392b" if f.accepted else "#909497"
        ax.plot(tmin / 60.0, model, lw=2.0, ls="--", color=col,
                label=f"fit: τ={f.tau_hours:.2f} h, T∞={f.t_inf_f:.1f} °F")
        ax.axhline(f.t_inf_f, color=col, lw=0.9, ls=":", alpha=0.7)
        ax.text(0.02, f.t_inf_f, " asymptote T∞", color=col, fontsize=8, va="bottom")
        if 0 < tau_min <= tmin[-1]:
            ax.axvline(f.tau_hours, color="#7f8c8d", ls=":", lw=1.2)
            ax.text(f.tau_hours, T[np.isfinite(T)].min(), " 1τ (63% of gap closed)",
                    fontsize=8, rotation=90, va="bottom", color="#7f8c8d")
        ax.set_xlabel("hours into passive segment")
        ax.set_ylabel("Temperature (°F)")
        ax.set_title(f"{day_label} — {f.label}\n{f.start:%H:%M}–{f.end:%H:%M} "
                     f"({dur/60:.1f} h, {rise:+.1f} °F)",
                     color="black" if f.accepted else "#909497")
        if not f.accepted:
            ax.text(0.5, 0.5, "NOT IDENTIFIABLE\nτ ≫ window — excluded",
                    transform=ax.transAxes, ha="center", va="center", fontsize=13,
                    color="#c0392b", alpha=0.35, weight="bold", rotation=18)
            for sp in ax.spines.values():
                sp.set_color("#c0392b")
                sp.set_linestyle("--")
        ax.legend(fontsize=9)
        ax.grid(alpha=0.25)

        # Bottom: goodness of fit — residuals plus the tau sensitivity curve.
        ax = axes[1][j]
        resid = T - model
        ax.plot(tmin / 60.0, resid, lw=1.0, color="#34495e")
        ax.axhline(0, color="#c0392b", lw=1.2)
        ax.fill_between(tmin / 60.0, 0, resid, color="#34495e", alpha=0.20)
        rms = float(np.sqrt(np.nanmean(resid ** 2)))
        ax.set_xlabel("hours into passive segment")
        ax.set_ylabel("residual (°F)")
        ax.set_title(f"Fit residuals — RMS {rms:.3f} °F, R²={f.r2:.4f}, "
                     f"gap closed {f.gap_closed_frac*100:.0f}%")
        mate = next((x for x in fits if x.method == METHOD_RATE
                     and x.label == f.label), None)
        txt = (f"τ  (curve fit)      = {f.tau_hours:>6.2f} h\n"
               f"T∞ (curve fit)      = {f.t_inf_f:>6.1f} °F")
        if mate is not None:
            txt += (f"\nτ  (rate regression) = {mate.tau_hours:>5.2f} h"
                    f"\nT∞ (rate regression) = {mate.t_inf_f:>5.1f} °F")
        txt += f"\n{'ACCEPTED' if f.accepted else 'EXCLUDED from summary'}"
        ax.text(0.99, 0.03, txt, transform=ax.transAxes, ha="right", va="bottom",
                fontsize=9, family="monospace",
                bbox=dict(fc="white", ec="#bbb", alpha=0.9))
        ax.grid(alpha=0.25)
    fig.suptitle("Thermal time-constant estimation", fontsize=13)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


# ---- Reporting ----

def fmt(x, spec=".3f", na="—"):
    return na if x is None or (isinstance(x, float) and not np.isfinite(x)) else format(x, spec)


def build_period_table(events_df: pd.DataFrame, coverage: dict, cfg: dict) -> pd.DataFrame:
    rows = []
    order = ["night", "morning", "afternoon", "evening"]
    for day, g_day in events_df.groupby("analysis_day"):
        for p in order:
            g = g_day[(g_day["period"] == p) & (~g_day["is_artifact"])]
            art = g_day[(g_day["period"] == p) & (g_day["is_artifact"])]
            cs = g["cool_slope_f_per_min"].dropna()
            hs = g["heat_slope_f_per_min"].dropna()
            rows.append({
                "analysis_day": day, "period": p,
                "coverage_pct": coverage.get(day, {}).get("coverage_pct", np.nan),
                "is_partial_day": coverage.get(day, {}).get("is_partial", True),
                "ac_events": len(g),
                "low_confidence_events": int((g["confidence"] == "low").sum()),
                "rejected_transients": len(art),
                "duty_cycle_pct": (g["cool_duration_min"].sum()
                                   / max(1e-9, _period_minutes(p, cfg)) * 100.0),
                "mean_cool_slope_f_per_min": cs.mean() if len(cs) else np.nan,
                "median_cool_slope_f_per_min": cs.median() if len(cs) else np.nan,
                "std_cool_slope_f_per_min": cs.std() if len(cs) > 1 else np.nan,
                "min_cool_slope_f_per_min": cs.min() if len(cs) else np.nan,
                "mean_heat_slope_f_per_min": hs.mean() if len(hs) else np.nan,
                "median_heat_slope_f_per_min": hs.median() if len(hs) else np.nan,
                "std_heat_slope_f_per_min": hs.std() if len(hs) > 1 else np.nan,
                "max_heat_slope_f_per_min": hs.max() if len(hs) else np.nan,
                "mean_cool_duration_min": g["cool_duration_min"].mean() if len(g) else np.nan,
                "mean_cool_drop_f": g["cool_drop_f"].mean() if len(g) else np.nan,
                "mean_cycle_period_min": ((g["cool_duration_min"] + g["heat_duration_min"]).mean()
                                          if len(g) else np.nan),
            })
    return pd.DataFrame(rows)


def _period_minutes(p: str, cfg: dict) -> float:
    a, b = cfg["periods"][p]
    return ((b - a) % 24 or 24) * 60.0


def print_report(lines: list[str]):
    for ln in lines:
        print(ln)


# ---- Main ----

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="CSV file(s) or glob(s). Default: *.csv in raw-data/.")
    ap.add_argument("--outdir", default=os.path.join(SCRIPT_DIR, "analysis_output"))
    ap.add_argument("--weather", action="store_true",
                    help="attempt the optional outdoor-temperature cross-check")
    ap.add_argument("--no-plots", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.outdir)
    if args.weather:
        cfg["weather_enabled"] = True

    paths = discover_inputs(args.paths)
    if not paths:
        raise SystemExit(f"No CSVs found. Looked in {RAW_DATA_DIR}")

    raw = load_data(paths, cfg["timezone"])
    df = to_uniform_grid(raw, cfg)
    df = smooth_and_slope(df, cfg)
    df = assign_days(df, cfg)
    df = add_period(df, cfg)

    # ── Output layout, stamped from the DATA's datetime range ────────────────
    stamp = (f"{raw['ts'].iloc[0]:%Y%m%d-%H%M}_to_{raw['ts'].iloc[-1]:%Y%m%d-%H%M}")
    out_root = args.outdir
    d_rep = os.path.join(out_root, "reports")
    d_plt = os.path.join(out_root, "plots")
    for d in (d_rep, d_plt):
        os.makedirs(d, exist_ok=True)

    outdoor = fetch_outdoor(raw, cfg)

    # ── Per-day analysis ─────────────────────────────────────────────────────
    all_events: list[ACEvent] = []
    coverage: dict = {}
    tau_rows: list[TauFit] = []
    setpoints: list[dict] = []
    expected = 24 * 60 / (cfg["grid_seconds"] / 60.0)

    for day_key, day in df.groupby("analysis_day"):
        day = day.reset_index(drop=True)
        label = str(day_key)
        n_valid = int(day["temp_f"].notna().sum())
        cov = 100.0 * n_valid / expected
        coverage[label] = {
            "coverage_pct": cov,
            "is_partial": cov < cfg["full_day_coverage_pct"],
            "n_samples": n_valid,
            "first": day["ts"].iloc[0], "last": day["ts"].iloc[-1],
            "t_min": float(day["temp_f"].min()), "t_max": float(day["temp_f"].max()),
            "t_mean": float(day["temp_f"].mean()),
            "t_range": float(day["temp_f"].max() - day["temp_f"].min()),
        }

        events = detect_events(day, cfg, label)
        all_events.extend(events)

        sp = detect_setpoint_raise(day, events, cfg)
        if sp:
            sp["analysis_day"] = label
            setpoints.append(sp)

        segs = passive_segments(day, events, cfg)
        fits = fit_tau_indoor(day, segs, cfg, label)
        fits += fit_tau_weather(day, segs, outdoor, cfg, label)
        tau_rows.extend(fits)

        if not args.no_plots:
            plot_day_diagnostic(day, events, sp, fits, cfg, label,
                                os.path.join(d_plt, f"day_{label}_diagnostic.png"))
            plot_cycle_detail(day, events, cfg, label,
                              os.path.join(d_plt, f"day_{label}_cycle_detail.png"))
            plot_tau(day, segs, fits, cfg, label,
                     os.path.join(d_plt, f"day_{label}_thermal_tau.png"))

    events_df = pd.DataFrame([asdict(e) for e in all_events])
    if events_df.empty:
        events_df = pd.DataFrame(columns=[f.name for f in ACEvent.__dataclass_fields__.values()])
    tau_df = pd.DataFrame([asdict(t) for t in tau_rows])
    period_df = build_period_table(events_df, coverage, cfg)

    day_df = pd.DataFrame([{"analysis_day": k, **{kk: vv for kk, vv in v.items()}}
                           for k, v in coverage.items()])
    if not events_df.empty:
        real = events_df[~events_df["is_artifact"]]
        agg = real.groupby("analysis_day").agg(
            ac_events=("period", "size"),
            total_cooling_min=("cool_duration_min", "sum"),
            mean_cool_slope=("cool_slope_f_per_min", "mean"),
            mean_heat_slope=("heat_slope_f_per_min", "mean"),
            mean_cool_drop_f=("cool_drop_f", "mean"),
        )
        rej = events_df[events_df["is_artifact"]].groupby("analysis_day").size().rename(
            "rejected_transients")
        day_df = day_df.merge(agg, on="analysis_day", how="left").merge(
            rej, on="analysis_day", how="left")
    for c in ["ac_events", "rejected_transients", "total_cooling_min"]:
        if c in day_df:
            day_df[c] = day_df[c].fillna(0)
    if "total_cooling_min" in day_df:
        day_df["hvac_duty_cycle_pct"] = day_df["total_cooling_min"] / (24 * 60) * 100

    if not tau_df.empty:
        ind = tau_df[(tau_df["method"] == METHOD_CURVE) & tau_df["accepted"]]
        if not ind.empty:
            w = ind.groupby("analysis_day").apply(
                lambda g: np.average(g["tau_hours"], weights=g["duration_min"]),
                include_groups=False).rename("tau_hours_weighted")
            day_df = day_df.merge(w, on="analysis_day", how="left")

    sp_df = pd.DataFrame(setpoints)

    # ── Plots that span all days ─────────────────────────────────────────────
    if not args.no_plots:
        plot_full_timeseries(df, all_events, cfg, os.path.join(d_plt, "raw_full_record.png"))
        plot_days_overlaid(df, cfg, os.path.join(d_plt, "days_overlaid.png"))
        if not events_df.empty:
            plot_period_slopes(events_df, cfg, os.path.join(d_plt, "period_slope_summary.png"))

    # ── CSV exports ──────────────────────────────────────────────────────────
    written = []
    for name, frame in [("daily_summary", day_df), ("ac_events", events_df),
                        ("period_stats", period_df), ("thermal_tau", tau_df),
                        ("setpoint_raise", sp_df)]:
        p = os.path.join(d_rep, f"hvac_analysis_{stamp}_{name}.csv")
        frame.to_csv(p, index=False)
        written.append(p)

    # ── Terminal report ──────────────────────────────────────────────────────
    L: list[str] = []
    A = L.append
    bar = "═" * 88
    A("")
    A(bar)
    A("  APARTMENT HVAC / THERMAL ANALYSIS")
    A(bar)
    A(f"  Input files          : {', '.join(os.path.basename(p) for p in paths)}")
    A(f"  Samples (raw / grid) : {len(raw)} / {len(df)}")
    A(f"  Record span (local)  : {raw['ts'].iloc[0]:%Y-%m-%d %H:%M %Z}"
      f"  →  {raw['ts'].iloc[-1]:%Y-%m-%d %H:%M %Z}")
    A(f"  Duration             : "
      f"{(raw['ts'].iloc[-1] - raw['ts'].iloc[0]).total_seconds()/3600:.1f} h")
    A(f"  Timezone             : {cfg['timezone']}   (analysis day "
      f"{cfg['day_start_hour']:02d}:00 → {cfg['day_start_hour']:02d}:00)")
    A(f"  Temperature range    : {raw['temp_f'].min():.2f} – {raw['temp_f'].max():.2f} °F")
    A("")

    A("─" * 88)
    A("  PER-DAY OVERVIEW")
    A("─" * 88)
    hdr = (f"  {'day':<12}{'cov%':>7}{'flag':>10}{'AC ev':>7}{'rej':>5}"
           f"{'duty%':>8}{'Tmin':>8}{'Tmax':>8}{'range':>8}{'tau(h)':>9}")
    A(hdr)
    for _, r in day_df.sort_values("analysis_day").iterrows():
        A(f"  {str(r['analysis_day']):<12}{r['coverage_pct']:>7.1f}"
          f"{('PARTIAL' if r['is_partial'] else 'full'):>10}"
          f"{int(r.get('ac_events', 0)):>7}{int(r.get('rejected_transients', 0) or 0):>5}"
          f"{r.get('hvac_duty_cycle_pct', float('nan')):>8.1f}"
          f"{r['t_min']:>8.2f}{r['t_max']:>8.2f}{r['t_range']:>8.2f}"
          f"{r.get('tau_hours_weighted', float('nan')):>9.2f}")
    A("")

    A("─" * 88)
    A("  AC EVENTS & SLOPES BY PERIOD    (cooling < 0, recovery-heating > 0, °F/min)")
    A("─" * 88)
    for day in sorted(period_df["analysis_day"].unique()):
        g = period_df[period_df["analysis_day"] == day]
        flag = "PARTIAL" if coverage[day]["is_partial"] else "full"
        A(f"  ── {day}  [{flag}, {coverage[day]['coverage_pct']:.0f}% coverage]")
        A(f"     {'period':<11}{'window':<14}{'events':>7}{'low':>5}{'rej':>5}"
          f"{'cool mean':>11}{'cool med':>10}{'cool min':>10}"
          f"{'heat mean':>11}{'heat med':>10}{'dur min':>9}{'drop F':>8}")
        for _, r in g.iterrows():
            a, b = cfg["periods"][r["period"]]
            A(f"     {r['period']:<11}{f'{a:02d}:00-{b:02d}:00':<14}"
              f"{int(r['ac_events']):>7}{int(r['low_confidence_events']):>5}"
              f"{int(r['rejected_transients']):>5}"
              f"{fmt(r['mean_cool_slope_f_per_min'], '+.3f'):>11}"
              f"{fmt(r['median_cool_slope_f_per_min'], '+.3f'):>10}"
              f"{fmt(r['min_cool_slope_f_per_min'], '+.3f'):>10}"
              f"{fmt(r['mean_heat_slope_f_per_min'], '+.3f'):>11}"
              f"{fmt(r['median_heat_slope_f_per_min'], '+.3f'):>10}"
              f"{fmt(r['mean_cool_duration_min'], '.1f'):>9}"
              f"{fmt(r['mean_cool_drop_f'], '.2f'):>8}")
        A("")

    if not events_df.empty and (events_df["confidence"] == "low").any():
        low = events_df[(events_df["confidence"] == "low") & (~events_df["is_artifact"])]
        if not low.empty:
            A("─" * 88)
            A("  LOW-CONFIDENCE EVENTS  (counted, but flagged)")
            A("─" * 88)
            A(f"  These cycles end less than {cfg['artifact_min_net_below_f']:.2f} °F below")
            A("  their own pre-event baseline. That is what you expect from short")
            A("  near-setpoint cycling late in the evening, but it is also what a local")
            A("  disturbance looks like — this data alone cannot separate the two.")
            A("")
            for _, r in low.iterrows():
                A(f"     {r['start']:%Y-%m-%d %H:%M}  {r['period']:<10} "
                  f"drop {r['cool_drop_f']:.2f} °F, "
                  f"only {r['net_below_baseline_f']:+.2f} °F below baseline "
                  f"{r['pre_baseline_f']:.2f} °F, pre-rise {r['pre_rise_f_per_min']:+.2f} °F/min")
            A("")

    if not events_df.empty and events_df["is_artifact"].any():
        A("─" * 88)
        A("  REJECTED TRANSIENTS  (sharp spikes inconsistent with compressor behaviour)")
        A("─" * 88)
        for _, r in events_df[events_df["is_artifact"]].iterrows():
            A(f"     {r['start']:%Y-%m-%d %H:%M}  {r['period']:<10} "
              f"drop {r['cool_drop_f']:.2f} °F in {r['cool_duration_min']:.1f} min")
            A(f"        {r['artifact_reason']}")
        A("")

    if not sp_df.empty:
        A("─" * 88)
        A("  SETPOINT-RAISE COAST  (longest AC-free warming stretch — the ~86 °F window)")
        A("─" * 88)
        for _, r in sp_df.iterrows():
            A(f"     {r['analysis_day']}:  {r['start']:%H:%M} → {r['end']:%H:%M}  "
              f"({r['duration_min']/60:.1f} h)   "
              f"{r['t_start_f']:.1f} → {r['t_end_f']:.1f} °F "
              f"(+{r['rise_f']:.1f}, peak {r['peak_f']:.1f} °F)   "
              f"mean rise {r['rise_f']/r['duration_min']:+.3f} °F/min")
        A("")

    A("─" * 88)
    A("  THERMAL TIME CONSTANT  τ    (first-order model: dT/dt = (T∞ − T)/τ)")
    A("─" * 88)
    if tau_df.empty:
        A("     No passive segment long enough to fit "
          f"(need ≥ {cfg['tau_min_segment_min']:.0f} min AC-free with "
          f"≥ {cfg['tau_min_rise_f']:.1f} °F drift).")
    else:
        for method in [METHOD_CURVE, METHOD_RATE, "weather-anchored (dT/dt vs Tout-T)"]:
            g = tau_df[tau_df["method"] == method]
            if g.empty:
                continue
            A(f"     Method: {method}"
              + ("   [PRIMARY]" if method == METHOD_CURVE else "   [cross-check]"))
            A(f"       {'day':<12}{'segment':<16}{'window':<14}{'hours':>7}"
              f"{'τ (h)':>9}{'T∞ (°F)':>10}{'R²':>9}{'gap%':>7}  use")
            for _, r in g.iterrows():
                A(f"       {r['analysis_day']:<12}{r['label']:<16}"
                  f"{f'{r.start:%H:%M}-{r.end:%H:%M}':<14}"
                  f"{r['duration_min']/60:>7.1f}{r['tau_hours']:>9.2f}"
                  f"{r['t_inf_f']:>10.1f}{r['r2']:>9.4f}"
                  f"{r['gap_closed_frac']*100:>7.0f}"
                  f"  {'✓' if r['accepted'] else '✗'}")
                if r["note"]:
                    for chunk in str(r["note"]).split("; "):
                        A(f"           · {chunk}")
            good = g[g["accepted"]]
            if not good.empty:
                tw = np.average(good["tau_hours"], weights=good["duration_min"])
                A(f"       → duration-weighted τ = {tw:.2f} h ({tw*60:.0f} min)"
                  f"   from {len(good)}/{len(g)} usable fit(s)")
            else:
                A("       → no usable fit from this method on this record")
            A("")
        good = tau_df[(tau_df["method"] == METHOD_CURVE) & tau_df["accepted"]]
        if not good.empty:
            tw = float(np.average(good["tau_hours"], weights=good["duration_min"]))
            tinf = float(np.average(good["t_inf_f"], weights=good["duration_min"]))
            A("     INTERPRETATION")
            A(f"       τ ≈ {tw:.2f} h ({tw*60:.0f} min). With the HVAC off the apartment")
            A(f"       closes ~63% of the gap to its driving temperature every {tw:.2f} h,")
            A(f"       and is ~95% equilibrated after 3τ = {3*tw:.1f} h.")
            A(f"       Effective driving temperature T∞ ≈ {tinf:.1f} °F over these segments.")
            A(f"       Practical read: cutting cooling at 75 °F against that pull puts the")
            A(f"       apartment at ~{75 + (tinf-75)*0.632:.0f} °F after {tw:.1f} h and "
              f"~{75 + (tinf-75)*0.865:.0f} °F after {2*tw:.1f} h.")
            A(f"       Recovery cost: reaching 75 °F again from {tinf:.0f} °F is roughly")
            A(f"       the integral the compressor has to remove — this is why the")
            A(f"       afternoon setback is only a net saving if the coast is long")
            A(f"       relative to τ.")
    A("")

    A("─" * 88)
    A("  OUTPUTS")
    A("─" * 88)
    for p in written:
        A(f"     CSV  {os.path.relpath(p, SCRIPT_DIR)}")
    if not args.no_plots:
        for p in sorted(glob.glob(os.path.join(d_plt, "*.png"))):
            A(f"     PNG  {os.path.relpath(p, SCRIPT_DIR)}")
    A(bar)
    A("")

    print_report(L)

    txt = os.path.join(d_rep, f"hvac_analysis_{stamp}_report.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    print(f"[out]  Text report: {os.path.relpath(txt, SCRIPT_DIR)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
