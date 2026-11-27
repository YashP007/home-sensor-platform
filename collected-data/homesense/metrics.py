"""Daily / weekly summary tables, data-health table and auto-generated flags."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .config import period_of_hour
from .util import local_time


def _clock_h(ts: pd.Series, origin_h: float = 18.0) -> pd.Series:
    """Clock time as hours after origin_h (so 23:30 -> 5.5, 01:00 -> 7.0); averages cleanly across midnight."""
    h = ts.dt.hour + ts.dt.minute / 60.0
    return (h - origin_h) % 24


def _fmt_clock(h_after: float, origin_h: float = 18.0) -> str:
    if not np.isfinite(h_after):
        return ""
    h = (h_after + origin_h) % 24
    return f"{int(h):02d}:{int(round((h % 1) * 60)) % 60:02d}"


def daily_table(df: pd.DataFrame, nights: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    lo, hi = cfg["comfort_low_f"], cfg["comfort_high_f"]
    tz = cfg["timezone"]
    hour = df.index.hour + df.index.minute / 60.0
    period = pd.Series([period_of_hour(h, cfg["periods"]) for h in np.round(hour, 4)], index=df.index)
    rows = []
    for day, g in df.groupby(df.index.date):
        day_len = (local_time(day, 24, tz) - local_time(day, 0, tz)) / pd.Timedelta(minutes=1)
        t = g["indoor_f"]
        n = t.notna().sum()
        raw = g["indoor_raw_f"] if "indoor_raw_f" in g else t
        row = {"date": day, "weekday": pd.Timestamp(day).day_name()[:3],
               "coverage_pct": 100 * raw.notna().sum() / day_len}
        if n == 0:
            rows.append(row)
            continue
        row.update({
            "indoor_mean_f": t.mean(), "indoor_min_f": t.min(), "indoor_max_f": t.max(),
            "indoor_min_at": f"{t.idxmin():%H:%M}", "indoor_max_at": f"{t.idxmax():%H:%M}",
            "swing_f": t.max() - t.min(),
            "hours_below_comfort": (t < lo).sum() / 60, "hours_in_comfort": ((t >= lo) & (t <= hi)).sum() / 60,
            "hours_above_comfort": (t > hi).sum() / 60,
            "degree_hours_below": (lo - t).clip(lower=0).sum() / 60,
            "degree_hours_above": (t - hi).clip(lower=0).sum() / 60,
        })
        row["comfort_pct"] = 100 * row["hours_in_comfort"] / (n / 60)
        for p in cfg["periods"]:
            v = t[period.loc[g.index] == p]
            row[f"{p}_mean_f"] = v.mean() if v.notna().any() else np.nan
        t10 = t.loc[local_time(day, 10, tz):local_time(day, 10.25, tz)]
        t_day = t.loc[local_time(day, 10, tz):local_time(day, 17, tz)]
        if t10.notna().any() and t_day.notna().any():
            row["daytime_rise_f"] = t_day.max() - t10.mean()
        if "outdoor_f" in g and g["outdoor_f"].notna().any():
            o = g["outdoor_f"]
            srcs = set(g["outdoor_src"].dropna())
            row.update({"outdoor_mean_f": o.mean(), "outdoor_min_f": o.min(), "outdoor_max_f": o.max(),
                        "in_out_gap_mean_f": (t - o).mean(),
                        "outdoor_source": srcs.pop() if len(srcs) == 1 else "mixed"})
        for col, name in [("humidity_pct", "humidity"), ("pressure_hpa", "pressure")]:
            if col in g and g[col].notna().any():
                row.update({f"{name}_mean": g[col].mean(), f"{name}_min": g[col].min(),
                            f"{name}_max": g[col].max()})
        if "sun_lux" in g and g["sun_lux"].notna().any():
            row["sun_klux_h"] = g["sun_lux"].sum() / 60 / 1000
            row["sun_peak_lux"] = g["sun_lux"].max()
        if "sensor_sunlit" in g and g["sensor_sunlit"].any():
            row["sensor_sunlit_h"] = g["sensor_sunlit"].sum() / 60
            row["indoor_max_raw_f"] = g["indoor_raw_f"].max()
        if "state" in g:
            row["lamp_on_h"] = (g["state"] == "lights on").sum() / 60
            row["dark_awake_h"] = (g["state"] == "dark (awake/away)").sum() / 60
        rows.append(row)
    daily = pd.DataFrame(rows)
    if not nights.empty and "lights_out" in nights:
        n = nights[["night_of", "lights_out", "wake", "sleep_h", "bedtime_confidence"]].rename(
            columns={"night_of": "date", "wake": "wake_next_morning"})
        daily = daily.merge(n, on="date", how="left")
    daily["partial_day"] = daily["coverage_pct"] < 90
    return daily


def weekly_table(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily[daily["coverage_pct"] > 0].copy()
    d["week_start"] = pd.to_datetime(d["date"]).dt.to_period("W-SUN").dt.start_time.dt.date
    rows = []
    for wk, g in d.groupby("week_start"):
        hrs = g[["hours_below_comfort", "hours_in_comfort", "hours_above_comfort"]].sum()
        row = {"week_start": wk, "iso_week": f"{pd.Timestamp(wk).isocalendar().year}-W"
                                             f"{pd.Timestamp(wk).isocalendar().week:02d}",
               "days_with_data": len(g), "full_days": int((~g["partial_day"]).sum()),
               "indoor_mean_f": np.average(g["indoor_mean_f"], weights=g["coverage_pct"]),
               "indoor_min_f": g["indoor_min_f"].min(), "indoor_max_f": g["indoor_max_f"].max(),
               "mean_daily_swing_f": g["swing_f"].mean(),
               "comfort_pct": 100 * hrs["hours_in_comfort"] / hrs.sum() if hrs.sum() else np.nan,
               "hours_below_comfort": hrs["hours_below_comfort"],
               "hours_above_comfort": hrs["hours_above_comfort"],
               "degree_hours_below": g["degree_hours_below"].sum(),
               "degree_hours_above": g["degree_hours_above"].sum()}
        for c in ["outdoor_mean_f", "in_out_gap_mean_f", "daytime_rise_f", "humidity_mean",
                  "lamp_on_h", "sun_klux_h", "sleep_h"]:
            if c in g:
                row[c if c != "sleep_h" else "sleep_h_mean"] = g[c].mean()
        if "outdoor_min_f" in g:
            row["outdoor_min_f"] = g["outdoor_min_f"].min()
        if "lights_out" in g and g["lights_out"].notna().any():
            lo = pd.to_datetime(g["lights_out"].dropna())
            wk_ = pd.to_datetime(g["wake_next_morning"].dropna())
            row["lights_out_mean"] = _fmt_clock(_clock_h(lo).mean())
            row["lights_out_sd_h"] = _clock_h(lo).std()
            row["wake_mean"] = _fmt_clock(_clock_h(wk_).mean())
        rows.append(row)
    return pd.DataFrame(rows)


def health_table(health: dict, df: pd.DataFrame, gap_report_min: float = 30) -> pd.DataFrame:
    rows = []
    t0, t1 = df.index[0], df.index[-1]
    for col, h in health.items():
        gaps = [(a, b) for a, b in h.gaps if b > t0 and a < t1 and (b - a) > pd.Timedelta(minutes=gap_report_min)]
        longest = max(gaps, key=lambda g: g[1] - g[0]) if gaps else None
        rows.append({
            "feed": col, "files": len(h.files), "raw_rows": h.raw_rows, "duplicates": h.duplicates,
            "rejected_out_of_range": h.out_of_range, "rejected_spikes": h.spikes,
            "rejected_warmup": h.warmup,
            "first_sample": h.first, "last_sample": h.last,
            "coverage_pct_in_window": 100 * df[col].notna().mean() if col in df else 0.0,
            f"gaps_over_{int(gap_report_min)}min": len(gaps),
            "longest_gap_h": (longest[1] - longest[0]) / pd.Timedelta(hours=1) if longest else 0.0,
            "longest_gap_start": longest[0] if longest else pd.NaT,
        })
    return pd.DataFrame(rows)


def heating_balance_outdoor(model, start_f: float, target_f: float, coast_h: float) -> float:
    """Overnight-mean outdoor °F below which coasting from start_f no longer holds target_f."""
    d = math.exp(-model.k_couple * coast_h * 60)
    return (target_f - start_f * d) / (1 - d) - model.offset_f


def build_flags(daily, nights, coast, model, forecast, health_df, df, cfg) -> list[dict]:
    lo, hi = cfg["comfort_low_f"], cfg["comfort_high_f"]
    F = []

    def add(sev, cat, msg, date=""):
        F.append({"severity": sev, "category": cat, "date": str(date), "message": msg})

    # Comfort - per day for short records, rolled up per week for long ones
    if len(daily) <= 14:
        for _, r in daily.iterrows():
            if r.get("hours_below_comfort", 0) > 0.25:
                add("warning", "comfort", f"{r['hours_below_comfort']:.1f} h below {lo:g} °F "
                    f"(low {r['indoor_min_f']:.1f} °F at {r['indoor_min_at']})", r["date"])
            if r.get("hours_above_comfort", 0) > 0.25:
                add("warning", "comfort", f"{r['hours_above_comfort']:.1f} h above {hi:g} °F "
                    f"(high {r['indoor_max_f']:.1f} °F at {r['indoor_max_at']})", r["date"])
    elif "hours_below_comfort" in daily:
        d = daily.dropna(subset=["indoor_mean_f"]).copy()
        d["week"] = pd.to_datetime(d["date"]).dt.to_period("W-SUN").dt.start_time.dt.date
        for wk, g in d.groupby("week"):
            for col, word, ext, fn in (("hours_below_comfort", f"below {lo:g}", "indoor_min_f", "idxmin"),
                                       ("hours_above_comfort", f"above {hi:g}", "indoor_max_f", "idxmax")):
                bad = g[g[col] > 0.25]
                if len(bad):
                    pk = g.loc[getattr(g[ext], fn)()]
                    add("warning", "comfort", f"week of {wk}: {len(bad)} day(s) {word} °F, {bad[col].sum():.1f} h "
                        f"in total (extreme {pk[ext]:.1f} °F on {pk['date']})", wk)
    full = daily[~daily["partial_day"] & daily["indoor_mean_f"].notna()] if "indoor_mean_f" in daily else daily.iloc[:0]
    if len(full) and full["hours_below_comfort"].sum() + full["hours_above_comfort"].sum() < 0.25:
        add("good", "comfort", f"every full day stayed inside {lo:g}-{hi:g} °F")

    # Overnight coast + heating outlook
    if not coast.empty:
        for _, r in coast.iterrows():
            if r["end_f"] < lo:
                add("warning", "overnight", f"fell to {r['end_f']:.1f} °F by {cfg['coast_end_hour']:02d}:00 "
                    f"(from {r['start_f']:.1f} °F at {cfg['coast_start_hour']:02d}:00)", r["night_of"])
            if r.get("heat_suspected"):
                add("info", "overnight", f"indoor temperature rose for {r['warming_minutes']} min "
                    f"between {cfg['coast_start_hour']:02d}:00 and {cfg['coast_end_hour']:02d}:00 "
                    "(heater, cooking, shower or a closed window?)", r["night_of"])
        if model is not None and "pred_err_f" in coast and coast["pred_err_f"].notna().any():
            mae = coast["pred_err_f"].abs().mean()
            add("info" if mae <= 1.5 else "warning", "model",
                f"thermal model predicts the {cfg['coast_end_hour']:02d}:00 temperature to within "
                f"{mae:.1f} °F on average over {coast['pred_err_f'].notna().sum()} nights")
        if model is not None:
            start = float(coast["start_f"].tail(7).median())
            coast_h = 24 + cfg["coast_end_hour"] - cfg["coast_start_hour"]
            caveat = (f" (preliminary: {model.days}-day model, τ 90% CI {model.tau_h_ci[0]:.0f}-"
                      f"{model.tau_h_ci[1]:.0f} h)") if model.days < 7 else ""
            for target in cfg["morning_targets_f"]:
                thr = heating_balance_outdoor(model, start, target, coast_h)
                add("info", "heating outlook",
                    f"starting the night at your typical {start:.1f} °F, you wake above {target:g} °F "
                    f"without heat as long as the overnight outdoor average stays above {thr:.0f} °F{caveat}")
    if forecast:
        req = forecast["required_start_f"]
        when = f"{forecast['coast_from']:%H:%M}"
        parts = ", ".join(f"≥{v:.1f} °F for {k:g} °F" for k, v in req.items())
        sev = "warning" if np.isfinite(forecast["current_indoor_f"]) and \
            forecast["current_indoor_f"] < req[min(req)] else "info"
        add(sev, "tonight", f"forecast overnight outdoor mean {forecast['forecast_outdoor_mean_f']:.0f} °F "
            f"(low {forecast['forecast_outdoor_min_f']:.0f}). Indoor needed at {when}: {parts} "
            f"at {cfg['coast_end_hour']:02d}:00")

    # Sleep / activity
    if not nights.empty and "sleep_h" in nights:
        ok = nights.dropna(subset=["sleep_h"])
        for _, r in ok.iterrows():
            if r["sleep_h"] < 6 and r["bedtime_confidence"] == "high":
                add("info", "sleep", f"lights out {r['lights_out']:%H:%M} -> first lamp {r['wake']:%H:%M}: "
                    f"{r['sleep_h']:.1f} h dark window", r["night_of"])
        if len(ok) >= 3:
            lo_h = _clock_h(pd.to_datetime(ok["lights_out"]))
            if lo_h.std() > 1.0:
                add("info", "sleep", f"lights-out time varies by ±{lo_h.std():.1f} h night to night "
                    f"(earliest {_fmt_clock(lo_h.min())}, latest {_fmt_clock(lo_h.max())})")

    # Sensor placement
    if "sensor_sunlit" in df and df["sensor_sunlit"].any():
        sl = df[df["sensor_sunlit"]]
        days = sorted(set(sl.index.date))
        bump = (df["indoor_raw_f"] - df["indoor_f"].interpolate(limit_area="inside"))[df["sensor_sunlit"]]
        hours = sl.index.hour + sl.index.minute / 60
        add("warning", "sensor",
            f"direct sun reaches the sensor on {len(days)} of {len(set(df.index.date))} days, around "
            f"{int(hours.min()):02d}:{int(hours.min() % 1 * 60):02d}-{int(hours.max()):02d}:{int(hours.max() % 1 * 60):02d}, "
            f"reading up to {bump.max():.0f} °F above the room ({sl['indoor_raw_f'].max():.1f} °F raw). "
            f"Those {len(sl) / 60:.1f} h are excluded from all statistics. Shade or move the sensor "
            "(also skews the firmware's slope-based HVAC detection)")

    # Light sensor
    if {"led_lux", "sun_up"} <= set(df.columns):
        leak = df.loc[df["sun_up"], "led_lux"]
        if (leak > 1000).any():
            add("info", "sensor", f"led_lux reaches {leak.max():.0f} lux in daylight - sunlight is leaking into "
                "the LED channel. Calibrate R_SUN / R_LED (LIGHT_CALIBRATION=1); until then daytime "
                "lamp use cannot be separated from sun")

    # Data health
    for _, r in daily[daily["partial_day"]].iterrows():
        add("info", "data", f"only {r['coverage_pct']:.0f}% of the day has indoor data", r["date"])
    for _, r in health_df.iterrows():
        if r["longest_gap_h"] >= 1:
            add("info", "data", f"{r['feed']}: longest gap {r['longest_gap_h']:.1f} h starting "
                f"{r['longest_gap_start']:%Y-%m-%d %H:%M}")
        if r["rejected_out_of_range"] + r["rejected_spikes"] + r["rejected_warmup"] > 0:
            add("info", "data", f"{r['feed']}: dropped {r['rejected_out_of_range']} out-of-range, "
                f"{r['rejected_spikes']} spike and {r['rejected_warmup']} power-up warm-up samples (whole history)")

    order = {"critical": 0, "warning": 1, "info": 2, "good": 3}
    F.sort(key=lambda f: (order[f["severity"]], f["category"], f["date"]))
    return F
