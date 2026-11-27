"""CSV, summary.md and summary.json writers.

summary.md is written to be pasted (with the PNGs) into an AI model: it states the
context, the headline numbers, the flags, and defines every CSV column.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

COLUMN_DOCS = {
    # daily_summary.csv
    "date": "local calendar date (America/New_York)",
    "coverage_pct": "% of the day with indoor temperature data",
    "indoor_mean_f / _min_f / _max_f": "indoor temperature stats, °F",
    "indoor_min_at / indoor_max_at": "local clock time of the day's low / high",
    "swing_f": "daily indoor max − min, °F",
    "hours_below_comfort / _in_comfort / _above_comfort": "hours spent below / inside / above the comfort band",
    "comfort_pct": "% of measured time inside the comfort band",
    "degree_hours_below / _above": "Σ(°F outside band × hours) - severity, not just duration",
    "night_mean_f, morning_mean_f, day_mean_f, evening_mean_f": "mean indoor °F per time-of-day period (see config periods)",
    "daytime_rise_f": "indoor max between 10:00-17:00 minus the 10:00 value: daytime (mostly solar) warm-up",
    "outdoor_mean_f / _min_f / _max_f": "outdoor °F (logged feed; Open-Meteo hourly where the feed is missing)",
    "in_out_gap_mean_f": "mean indoor − outdoor, °F",
    "outdoor_source": "logged | open-meteo | mixed",
    "sun_klux_h": "sun-channel light dose, thousand lux·hours (how sunny the room was)",
    "sun_peak_lux": "max sun-channel lux",
    "lamp_on_h": "hours with artificial light on while the sun was down",
    "dark_awake_h": "hours the living room was dark after sunset but before the lights-out run (out, or another room)",
    "lights_out": "start of the overnight dark run in the living room (bedtime proxy)",
    "wake_next_morning / wake": "end of that dark run: first lamp on (wake_source=lamp) or sunrise (wake_source=daylight)",
    "sleep_h": "length of the overnight dark run, hours (living-room proxy for sleep)",
    "bedtime_confidence": "low = room was already dark from dusk (no lamp switched off), so the start is not a bedtime",
    "partial_day": "True when coverage < 90 %",
    # nights.csv
    "night_of": "the evening date the night starts on",
    "sleep_interruptions": "lamp-on blips inside the dark run",
    "evening_lamp_h / evening_dark_h": "between sunset and lights-out: hours lamp on / room dark",
    "first_evening_lamp": "first lamp switched on after sunset",
    "morning_lamp_min": "minutes of lamp light between wake and sunrise",
    "start_f / end_f": "indoor °F at coast start (22:00) and coast end (06:00)",
    "min_f / min_at": "overnight indoor low and when",
    "drop_f / drop_rate_f_per_h": "overnight indoor fall and its average rate",
    "outdoor_mean_f / outdoor_min_f (nights)": "outdoor during the coast window",
    "mean_in_out_gap_f": "mean indoor − outdoor during the coast window",
    "effective_tau_h": "gap ÷ fall rate: a no-model time-constant estimate (biased high by internal gains)",
    "pred_end_f / pred_err_f": "thermal-model prediction of the 06:00 temperature and its error (pred − actual)",
    "warming_minutes / heat_suspected": "minutes the indoor temp rose overnight; ≥15 suggests heating or another heat source",
    # preheat_guide.csv
    "start_hour / coast_h": "hour the heat goes off and how many hours it coasts to 06:00",
    "target_f": "indoor °F wanted at 06:00",
    "settles_at_f": "where the apartment would settle with no heat and no sun (outdoor + model offset)",
    "required_start_f": "indoor °F needed at start_hour; empty = target is met without any heat",
    # hourly_timeseries.csv
    "ts": "local hour start",
    "state_mode": "most common activity state in the hour",
}


def _fmt(v):
    if v is None or v is pd.NaT or (isinstance(v, float) and not np.isfinite(v)):
        return ""
    if isinstance(v, pd.Timestamp):
        return "" if pd.isna(v) else f"{v:%Y-%m-%d %H:%M}"
    if isinstance(v, (float, np.floating)):
        return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 1e5 else f"{v:.0f}"
    return str(v)


def md_table(df: pd.DataFrame, cols: list[str] | None = None, max_rows: int | None = None) -> str:
    if df is None or df.empty:
        return "_none_\n"
    d = df[[c for c in (cols or df.columns) if c in df.columns]]
    if max_rows and len(d) > max_rows:
        d = d.tail(max_rows)
    lines = ["| " + " | ".join(d.columns) + " |", "|" + "---|" * len(d.columns)]
    for _, r in d.iterrows():
        lines.append("| " + " | ".join(_fmt(v).replace("|", "/") for v in r) + " |")
    return "\n".join(lines) + "\n"


def to_csv(df: pd.DataFrame, path: str):
    out = df.copy()
    for c in out.columns:
        if isinstance(out[c].dtype, pd.DatetimeTZDtype):
            out[c] = out[c].dt.strftime("%Y-%m-%d %H:%M")
        elif out[c].dtype == object and out[c].map(lambda v: isinstance(v, pd.Timestamp)).any():
            out[c] = out[c].map(lambda v: f"{v:%Y-%m-%d %H:%M}" if isinstance(v, pd.Timestamp) and pd.notna(v) else "")
        elif pd.api.types.is_float_dtype(out[c]):
            out[c] = out[c].round(3)
    out.to_csv(path, index=False)


def hourly_table(df: pd.DataFrame) -> pd.DataFrame:
    num = [c for c in ("indoor_f", "outdoor_f", "led_lux", "sun_lux", "humidity_pct", "pressure_hpa") if c in df]
    h = df[num].resample("1h").mean()
    h["indoor_min_f"] = df["indoor_f"].resample("1h").min()
    h["indoor_max_f"] = df["indoor_f"].resample("1h").max()
    if "state" in df:
        h["state_mode"] = df["state"].resample("1h").agg(lambda s: s.mode().iat[0] if len(s) else None)
    h = h.dropna(how="all", subset=num)
    h.index.name = "ts"
    return h.reset_index()


def write_summary(outdir: str, ctx: dict):
    """ctx keys: mode, cfg, df, daily, weekly, nights, model, guide, forecast, flags, health, files."""
    cfg, df, daily, weekly = ctx["cfg"], ctx["df"], ctx["daily"], ctx["weekly"]
    model, nights, guide, forecast, flags = ctx.get("model"), ctx.get("nights"), ctx.get("guide"), \
        ctx.get("forecast"), ctx["flags"]
    lo, hi = cfg["comfort_low_f"], cfg["comfort_high_f"]
    t0, t1 = df.index[0], df.index[-1]
    full = daily[daily["indoor_mean_f"].notna()]
    hrs = full[["hours_below_comfort", "hours_in_comfort", "hours_above_comfort"]].sum()

    head = {
        "range": f"{t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M} ({cfg['timezone']})",
        "days_with_data": int(len(full)),
        "feeds": sorted(c for c in ("indoor_f", "outdoor_f", "led_lux", "sun_lux", "humidity_pct",
                                    "pressure_hpa") if c in df),
        "indoor_mean_f": float(df["indoor_f"].mean()),
        "indoor_min_f": float(df["indoor_f"].min()), "indoor_min_at": f"{df['indoor_f'].idxmin():%Y-%m-%d %H:%M}",
        "indoor_max_f": float(df["indoor_f"].max()), "indoor_max_at": f"{df['indoor_f'].idxmax():%Y-%m-%d %H:%M}",
        "comfort_pct": float(100 * hrs["hours_in_comfort"] / hrs.sum()) if hrs.sum() else None,
        "comfort_band_f": [lo, hi],
    }
    if "outdoor_f" in df:
        head["outdoor_mean_f"] = float(df["outdoor_f"].mean())
        head["outdoor_min_f"] = float(df["outdoor_f"].min())
    if nights is not None and not nights.empty and "sleep_h" in nights:
        head["mean_dark_window_h"] = float(nights["sleep_h"].mean())

    L = []
    title = "Home environment summary" + (" - temperature-only history" if ctx["mode"] == "legacy" else "")
    L += [f"# {title}", "", f"**Data range:** {head['range']}  ", f"**Generated:** "
          f"{pd.Timestamp.now(tz=cfg['timezone']):%Y-%m-%d %H:%M}  ",
          f"**Feeds:** {', '.join(head['feeds'])}", ""]
    L += ["## Context for the reader", "",
          "- One-bedroom-style apartment in Boston, MA. A custom ESP32-C3 + BME280 sensor in the centre of the "
          "living room (not under a lamp) logs indoor temperature every 30 s"
          + (", plus a TSL2591 light sensor split into a lamp/LED channel and a sun channel, and outdoor "
             "temperature (Open-Meteo, 15 min)." if ctx["mode"] == "current" else "."),
          "- The occupant controls the thermostat. In autumn the apartment warms up enough during the day that "
          "heating has not been needed overnight; the occupant is interested in heating while away so the "
          "apartment's thermal mass carries it through the night.",
          f"- Comfort band: {lo:g}-{hi:g} °F. All times are local (America/New_York). Days are calendar days.",
          "- The light-based 'lights out' / 'wake' times describe the living room only - a bedtime proxy.",
          "- Plots: weekly dashboards in plots/weekly/, record-wide figures in plots/. Data: data/*.csv.", ""]

    L += ["## At a glance", ""]
    L.append(f"- **{head['days_with_data']} days** analysed.")
    L.append(f"- Indoor averaged **{head['indoor_mean_f']:.1f} °F** (low {head['indoor_min_f']:.1f} on "
             f"{head['indoor_min_at']}, high {head['indoor_max_f']:.1f} on {head['indoor_max_at']}).")
    if head["comfort_pct"] is not None:
        L.append(f"- **{head['comfort_pct']:.1f} %** of measured time inside {lo:g}-{hi:g} °F "
                 f"({hrs['hours_below_comfort']:.1f} h below, {hrs['hours_above_comfort']:.1f} h above).")
    if "outdoor_mean_f" in head:
        L.append(f"- Outdoor averaged {head['outdoor_mean_f']:.1f} °F (low {head['outdoor_min_f']:.1f}).")
    if "daytime_rise_f" in full and full["daytime_rise_f"].notna().any():
        L.append(f"- Typical daytime warm-up (10:00 → afternoon peak): **{full['daytime_rise_f'].median():+.1f} °F** "
                 f"(median; range {full['daytime_rise_f'].min():+.1f} to {full['daytime_rise_f'].max():+.1f}).")
    if model is not None:
        L.append(f"- Thermal time constant **τ ≈ {model.tau_h:.0f} h** (90 % CI {model.tau_h_ci[0]:.0f}-"
                 f"{model.tau_h_ci[1]:.0f} h). With no heat and no sun the apartment settles "
                 f"**{model.offset_f:.1f} °F above outdoor**.")
    if "mean_dark_window_h" in head:
        n = nights.dropna(subset=["sleep_h"])
        L.append(f"- Living-room lights-out window averaged **{head['mean_dark_window_h']:.1f} h** over {len(n)} nights.")
    L.append("")

    L += ["## Flags and action items", "",
          md_table(pd.DataFrame(flags), ["severity", "category", "date", "message"]), ""]

    if model is not None:
        L += ["## Thermal model", "",
              "`dT/dt = K_COUPLE·(T_out − T) + K_SOLAR·log10(1 + sun_lux) + K_BIAS`, fitted on passive "
              f"(|dT/dt| ≤ {cfg['fit_max_abs_slope']} °F/min) samples with a robust loss. "
              f"R² {model.r2:.2f}, residual RMS {model.rmse * 60:.2f} °F/h, {model.n} samples over {model.days} days.", "",
              "Firmware-ready (Level 2b `level2b_context_aware_inference.ino`):", "", "```cpp",
              model.firmware_snippet(), "```", ""]
        for n in model.notes:
            L.append(f"- Note: {n}")
        L.append("")
    if guide is not None and not guide.empty:
        L += ["## Pre-heat guide", "",
              f"Indoor °F needed when the heat goes off to still be at the target at "
              f"{cfg['coast_end_hour']:02d}:00, for a steady overnight outdoor temperature. Blank = met with no heat.", ""]
        p = guide.pivot_table(index="outdoor_f", columns=["start_hour", "target_f"], values="required_start_f",
                              aggfunc="first", dropna=False)
        p.columns = [f"off {h:02d}:00 → ≥{t:g}" for h, t in p.columns]
        L += [md_table(p.reset_index().rename(columns={"outdoor_f": "outdoor °F"})), ""]
    if forecast:
        L += ["## Tonight (Open-Meteo forecast)", "",
              f"- Coast {forecast['coast_from']:%a %H:%M} → {forecast['coast_to']:%a %H:%M}; forecast outdoor mean "
              f"{forecast['forecast_outdoor_mean_f']:.1f} °F, low {forecast['forecast_outdoor_min_f']:.1f} °F."]
        if np.isfinite(forecast["current_indoor_f"]):
            L.append(f"- Indoor now {forecast['current_indoor_f']:.1f} °F → predicted "
                     f"{forecast['predicted_morning_f_if_starting_now']:.1f} °F at {forecast['coast_to']:%H:%M} "
                     "if nothing changes.")
        for t, v in forecast["required_start_f"].items():
            L.append(f"- To wake at ≥{t:g} °F the apartment needs to be ≥{v:.1f} °F at {forecast['coast_from']:%H:%M}.")
        L.append("")

    if nights is not None and not nights.empty:
        L += ["## Nights", "", md_table(nights, ["night_of", "lights_out", "wake", "sleep_h", "bedtime_confidence",
                                                 "evening_lamp_h", "start_f", "end_f", "drop_f", "outdoor_mean_f",
                                                 "pred_err_f", "note"], max_rows=21), ""]
    L += ["## Weekly", "", md_table(weekly), ""]
    L += ["## Daily" + (" (last 21 days; full table in data/daily_summary.csv)" if len(daily) > 21 else ""), "",
          md_table(daily, ["date", "weekday", "coverage_pct", "indoor_mean_f", "indoor_min_f", "indoor_max_f",
                           "comfort_pct", "daytime_rise_f", "outdoor_mean_f", "in_out_gap_mean_f",
                           "sun_klux_h", "lamp_on_h", "sleep_h"], max_rows=21), ""]
    L += ["## Data health", "", md_table(ctx["health"]), ""]
    L += ["## Files", ""] + [f"- `{f}`" for f in ctx["files"]] + [""]
    L += ["## Column dictionary", "", "| column | meaning |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in COLUMN_DOCS.items()] + [""]
    L += ["## Method caveats", "",
          "- Lux split: the firmware's R_SUN/R_LED are uncalibrated placeholders, so daytime sunlight leaks into "
          "the lamp channel. Lamp use is therefore only judged while the sun is down.",
          "- The thermal model is single-time-constant. Right after a warm afternoon the air cools faster than "
          "the model (fast air mode vs slow thermal mass); see pred_err_f in nights.csv.",
          "- The HVAC state feed is not used (the firmware's heat-state inference is not working).",
          *(["- No light sensor in this period, so direct sun on the sensor cannot be detected. In October "
             "(multi-feed data) the sun reaches the sensor ~12:45-15:15 and reads up to ~19 °F high; afternoon "
             "peaks here may be partly sensor heating rather than room air."] if ctx["mode"] == "legacy" else []),
          f"- Indoor samples outside {cfg['valid_range']['indoor_f']} °F or > {cfg['spike_threshold']['indoor_f']} °F "
          "from their rolling median are dropped as sensor faults.", ""]

    with open(os.path.join(outdir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))

    js = {"mode": ctx["mode"], "headline": head, "flags": flags,
          "weekly": json.loads(weekly.to_json(orient="records", date_format="iso"))}
    if model is not None:
        js["thermal_model"] = {"k_couple_per_min": model.k_couple, "k_solar": model.k_solar,
                               "k_bias_f_per_min": model.k_bias, "tau_h": model.tau_h,
                               "tau_h_ci90": list(model.tau_h_ci), "offset_f": model.offset_f,
                               "r2": model.r2, "days": model.days, "notes": model.notes}
    if forecast:
        js["tonight"] = {k: (str(v) if isinstance(v, pd.Timestamp) else v) for k, v in forecast.items()}
        js["tonight"]["required_start_f"] = {str(k): v for k, v in forecast["required_start_f"].items()}
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(_json_safe(js), fh, indent=2, ensure_ascii=False)


def _json_safe(o):
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (float, np.floating)):
        return float(o) if np.isfinite(o) else None
    if isinstance(o, (np.integer, np.bool_)):
        return o.item()
    if o is None or isinstance(o, (str, int, bool)):
        return o
    return str(o)
