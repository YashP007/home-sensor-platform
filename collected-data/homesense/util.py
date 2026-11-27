"""Small numeric helpers shared across modules."""

from __future__ import annotations

import numpy as np
import pandas as pd


def runs(mask) -> list[tuple[int, int]]:
    """Contiguous True runs of a boolean array as inclusive (start, end) index pairs."""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return []
    d = np.diff(m.astype(np.int8))
    starts = list(np.flatnonzero(d == 1) + 1)
    ends = list(np.flatnonzero(d == -1))
    if m[0]:
        starts.insert(0, 0)
    if m[-1]:
        ends.append(len(m) - 1)
    return list(zip(starts, ends))


def fill_short_false(mask: np.ndarray, max_len: int) -> np.ndarray:
    """Set False runs of length < max_len to True when bounded by True on both sides."""
    m = np.asarray(mask, dtype=bool).copy()
    for s, e in runs(~m):
        if s > 0 and e < len(m) - 1 and (e - s + 1) < max_len:
            m[s:e + 1] = True
    return m


def rolling_slope(values: np.ndarray, n: int, dt_min: float = 1.0) -> np.ndarray:
    """Centered least-squares slope per minute over an n-sample window.
    Any window containing NaN yields NaN, so slopes never bridge a data gap."""
    n = max(3, n | 1)
    out = np.full(len(values), np.nan)
    if len(values) <= n:
        return out
    t = (np.arange(n) - (n - 1) / 2.0) * dt_min
    w = t / np.sum(t ** 2)
    win = np.lib.stride_tricks.sliding_window_view(np.asarray(values, dtype=float), n)
    out[n // 2:len(values) - n // 2] = win @ w
    return out


def local_time(day, hour: float, tz: str) -> pd.Timestamp:
    """Wall-clock time on a calendar day (DST-safe; hour may exceed 24)."""
    d = pd.Timestamp(day) + pd.Timedelta(days=int(hour // 24))
    h = hour % 24
    return pd.Timestamp(f"{d:%Y-%m-%d} {int(h):02d}:{int(round((h % 1) * 60)):02d}").tz_localize(
        tz, ambiguous=True, nonexistent="shift_forward")


def ols(X: np.ndarray, y: np.ndarray):
    """Plain least squares; returns (coef, r2)."""
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    res = y - X @ coef
    sst = np.sum((y - y.mean()) ** 2)
    return coef, (1 - res @ res / sst) if sst > 0 else np.nan
