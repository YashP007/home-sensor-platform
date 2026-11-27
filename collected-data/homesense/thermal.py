"""Passive thermal model of the apartment and what it implies overnight.

Model (identical to the Level 2b firmware's passiveDriftFPerMin):

    dT/dt = K_COUPLE*(T_out - T) + K_SOLAR*log10(1 + sun_lux) + K_BIAS      [°F/min]

K_COUPLE is 1/tau (tau = how fast the apartment follows outdoors), K_SOLAR is the
sun's warming per decade of window light, K_BIAS is the always-on internal gain
(fridge, electronics, people, neighbours' heat through the walls). K_BIAS/K_COUPLE
is how far above outdoor the apartment settles with no heating and no sun.

It is a single-time-constant (lumped) model: air plus furniture/walls treated as
one mass. Real apartments also have a fast air mode, so right after heating stops
the first ~30 min falls faster than the model says. The nightly validation column
(pred_err_f) shows how much that matters in practice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from .util import local_time, ols, rolling_slope


@dataclass
class DriftModel:
    k_couple: float
    k_solar: float
    k_bias: float
    r2: float
    rmse: float
    n: int
    days: int
    uses_solar: bool
    tau_h_ci: tuple[float, float] = (np.nan, np.nan)
    notes: list[str] = field(default_factory=list)

    @property
    def tau_h(self) -> float:
        return 1.0 / self.k_couple / 60.0

    @property
    def offset_f(self) -> float:
        """Steady-state indoor - outdoor with no heating and no sun."""
        return self.k_bias / self.k_couple

    def simulate(self, t0: float, t_out: np.ndarray, sun: np.ndarray | None = None) -> np.ndarray:
        """Forward-integrate on a 1-min grid. Exact per-step exponential, so any tau is stable."""
        a = math.exp(-self.k_couple)
        sun_term = np.zeros(len(t_out)) if sun is None or not self.uses_solar \
            else self.k_solar * np.log10(1 + np.nan_to_num(sun))
        T = np.empty(len(t_out) + 1)
        T[0] = t0
        for k in range(len(t_out)):
            target = t_out[k] + (self.k_bias + sun_term[k]) / self.k_couple
            T[k + 1] = target + (T[k] - target) * a
        return T[1:]

    def firmware_snippet(self) -> str:
        return (f"constexpr float K_COUPLE = {self.k_couple:.6f}f;   // 1/min  (tau = {self.tau_h:.1f} h)\n"
                f"constexpr float K_SOLAR  = {self.k_solar if self.uses_solar else 0:.6f}f;   "
                f"// °F/min per log10(1 + sun_lux)\n"
                f"constexpr float K_BIAS   = {self.k_bias:.6f}f;   // °F/min")


def add_slope(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    df["dTdt"] = rolling_slope(df["indoor_f"].to_numpy(), cfg["slope_window_min"])
    return df


def _design(sub: pd.DataFrame, use_solar: bool) -> np.ndarray:
    cols = [sub["outdoor_f"] - sub["indoor_f"]]
    if use_solar:
        cols.append(np.log10(1 + sub["sun_lux"]))
    cols.append(np.ones(len(sub)))
    return np.column_stack(cols)


def fit_drift(df: pd.DataFrame, cfg: dict, use_solar: bool) -> DriftModel | None:
    need = ["indoor_f", "outdoor_f", "dTdt"] + (["sun_lux"] if use_solar else [])
    if any(c not in df.columns for c in need):
        return None
    sub = df.iloc[::cfg["fit_step_min"]][need].dropna()
    sub = sub[sub["dTdt"].abs() <= cfg["fit_max_abs_slope"]]
    if len(sub) < cfg["fit_min_samples"]:
        print(f"[thm]  only {len(sub)} usable samples - drift model not fitted")
        return None
    X, y = _design(sub, use_solar), sub["dTdt"].to_numpy()
    p0, _ = ols(X, y)
    # soft-L1 loss: cooking, showers, open windows and HVAC leftovers are heavy-tailed outliers
    p = least_squares(lambda q: X @ q - y, p0, loss="soft_l1", f_scale=0.01).x
    res = y - X @ p
    r2 = 1 - res @ res / np.sum((y - y.mean()) ** 2)

    k_c, k_b = p[0], p[-1]
    k_s = p[1] if use_solar else 0.0
    if k_c <= 0:
        print("[thm]  fitted K_COUPLE <= 0 (not physical) - need more / more varied data")
        return None
    m = DriftModel(k_c, k_s, k_b, float(r2), float(np.sqrt(np.mean(res ** 2))), len(sub),
                   len(set(sub.index.date)), use_solar)

    # Day-block bootstrap for tau: neighbouring minutes are not independent samples.
    days = np.array([d.toordinal() for d in sub.index.date])
    uniq = np.unique(days)
    if len(uniq) >= 3:
        rng = np.random.default_rng(0)
        taus = []
        for _ in range(300):
            pick = rng.choice(uniq, len(uniq))
            idx = np.concatenate([np.flatnonzero(days == d) for d in pick])
            c, _ = ols(X[idx], y[idx])
            if c[0] > 0:
                taus.append(1 / c[0] / 60)
        if len(taus) > 50:
            m.tau_h_ci = (float(np.percentile(taus, 5)), float(np.percentile(taus, 95)))
    if m.days < 7:
        m.notes.append(f"fitted on only {m.days} days - treat as preliminary, refit after 1-2 weeks")
    print(f"[thm]  drift model: tau {m.tau_h:.1f} h (90% CI {m.tau_h_ci[0]:.1f}-{m.tau_h_ci[1]:.1f}), "
          f"settles {m.offset_f:+.1f} °F vs outdoor, R² {m.r2:.2f}, n={m.n}")
    return m


def _at(df: pd.DataFrame, col: str, t: pd.Timestamp, half_min: int = 5) -> float:
    w = df.loc[t - pd.Timedelta(minutes=half_min):t + pd.Timedelta(minutes=half_min), col]
    return float(w.mean()) if w.notna().any() else np.nan


def overnight_coast(df: pd.DataFrame, model: DriftModel | None, cfg: dict) -> pd.DataFrame:
    """One row per night: how far the apartment fell between coast_start and coast_end."""
    tz, h0, h1 = cfg["timezone"], cfg["coast_start_hour"], cfg["coast_end_hour"]
    rows = []
    for day in pd.date_range(df.index[0].date(), df.index[-1].date(), freq="D"):
        t0, t1 = local_time(day, h0, tz), local_time(day, 24 + h1, tz)
        sub = df.loc[t0:t1]
        if len(sub) < 0.8 * (t1 - t0) / pd.Timedelta(minutes=1) or sub["indoor_f"].notna().mean() < 0.8:
            continue
        T0, T1 = _at(df, "indoor_f", t0), _at(df, "indoor_f", t1)
        if not (np.isfinite(T0) and np.isfinite(T1)):
            continue
        hours = (t1 - t0) / pd.Timedelta(hours=1)
        row = {"night_of": day.date(), "start_f": T0, "end_f": T1,
               "min_f": sub["indoor_f"].min(), "min_at": sub["indoor_f"].idxmin(),
               "drop_f": T0 - T1, "drop_rate_f_per_h": (T0 - T1) / hours,
               "warming_minutes": int((sub["dTdt"] > 0.02).sum()) if "dTdt" in sub else 0}
        if "outdoor_f" in sub and sub["outdoor_f"].notna().mean() > 0.8:
            tout = sub["outdoor_f"].interpolate(limit_direction="both")
            row.update({"outdoor_mean_f": tout.mean(), "outdoor_min_f": tout.min(),
                        "outdoor_src": sub["outdoor_src"].mode().iat[0]})
            gap = (sub["indoor_f"] - tout).mean()
            row["mean_in_out_gap_f"] = gap
            rate = row["drop_rate_f_per_h"]
            row["effective_tau_h"] = gap / rate if rate > 0.05 else np.nan
            if model is not None:
                sun = sub["sun_lux"].to_numpy() if "sun_lux" in sub else None
                pred = model.simulate(T0, tout.to_numpy()[1:], None if sun is None else sun[1:])
                row["pred_end_f"] = pred[-1]
                row["pred_err_f"] = pred[-1] - T1
        row["heat_suspected"] = row["warming_minutes"] >= 15
        rows.append(row)
    return pd.DataFrame(rows)


def preheat_guide(model: DriftModel, cfg: dict) -> pd.DataFrame:
    """Indoor temperature needed at each start hour to still be >= target at coast_end_hour,
    for a constant overnight outdoor temperature, heat off, no sun."""
    lo, hi, step = cfg["preheat_outdoor_range_f"]
    rows = []
    for h_start in cfg["preheat_start_hours"]:
        L = (24 + cfg["coast_end_hour"] - h_start) * 60.0
        decay = math.exp(-model.k_couple * L)
        for tout in np.arange(lo, hi + 0.1, step):
            t_inf = tout + model.offset_f
            for target in cfg["morning_targets_f"]:
                need = np.nan if target <= t_inf else t_inf + (target - t_inf) / decay
                rows.append({"start_hour": h_start, "coast_h": L / 60, "outdoor_f": float(tout),
                             "target_f": target, "settles_at_f": t_inf, "required_start_f": need})
    return pd.DataFrame(rows)


def forecast_tonight(df: pd.DataFrame, model: DriftModel, fc: pd.Series, cfg: dict) -> dict | None:
    """Use tonight's Open-Meteo forecast to say how warm the apartment must be at coast start."""
    if fc is None or len(fc) < 6:
        return None
    tz = cfg["timezone"]
    now = pd.Timestamp.now(tz=tz)
    day = (now - pd.Timedelta(hours=cfg["coast_end_hour"])).date()
    t0 = local_time(day, cfg["coast_start_hour"], tz)
    t1 = local_time(day, 24 + cfg["coast_end_hour"], tz)
    if now >= t1:
        return None
    grid = pd.date_range(max(now.ceil("min"), t0), t1, freq="1min")
    tout = fc.reindex(fc.index.union(grid)).interpolate(method="time", limit_direction="both").reindex(grid)
    if tout.isna().any():
        return None
    # The model is linear in T0, so two runs give T_end = a*T0 + b exactly.
    b = model.simulate(0.0, tout.to_numpy())[-1]
    a = model.simulate(1.0, tout.to_numpy())[-1] - b
    last = df["indoor_f"].dropna()
    current = float(last.iat[-1]) if len(last) and now - last.index[-1] < pd.Timedelta(hours=3) else np.nan
    out = {
        "coast_from": grid[0], "coast_to": t1,
        "forecast_outdoor_mean_f": float(tout.mean()), "forecast_outdoor_min_f": float(tout.min()),
        "current_indoor_f": current,
        "predicted_morning_f_if_starting_now": a * current + b if np.isfinite(current) else np.nan,
        "required_start_f": {t: (t - b) / a for t in cfg["morning_targets_f"]},
    }
    return out
