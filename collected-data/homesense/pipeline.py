"""Wires loading -> analysis -> outputs for both entry scripts."""

from __future__ import annotations

import glob
import os

import pandas as pd

from . import activity, io, metrics, plots, report, thermal
from .util import local_time

OUTPUT_PATTERNS = ["summary.md", "summary.json", "explorer.html", "data/*.csv", "plots/*.png",
                   "plots/weekly/*.png", "plots/days/*.png"]


def multi_feed_start(series: dict, cfg: dict) -> pd.Timestamp | None:
    if cfg.get("multi_feed_start"):
        return pd.Timestamp(cfg["multi_feed_start"]).tz_localize(cfg["timezone"])
    starts = [series[c].index[0] for c in ("led_lux", "sun_lux") if c in series]
    return min(starts).floor("min") if starts else None


def _clean_outputs(outdir: str):
    for pat in OUTPUT_PATTERNS:
        for f in glob.glob(os.path.join(outdir, pat)):
            os.remove(f)


def _week_starts(t0: pd.Timestamp, t1: pd.Timestamp, tz: str) -> list[pd.Timestamp]:
    first = t0.date() - pd.Timedelta(days=t0.weekday())
    return [local_time(d, 0, tz) for d in pd.date_range(first, t1.date(), freq="7D")]


def _week_subtitle(wk: pd.Series | None, cfg: dict) -> str:
    if wk is None:
        return ""
    parts = [f"Indoor avg {wk['indoor_mean_f']:.1f} °F (low {wk['indoor_min_f']:.1f}, high {wk['indoor_max_f']:.1f})",
             f"{wk['comfort_pct']:.0f}% of time in {cfg['comfort_low_f']:g}-{cfg['comfort_high_f']:g} °F"]
    if pd.notna(wk.get("outdoor_mean_f")):
        parts.append(f"outdoor avg {wk['outdoor_mean_f']:.1f} °F (low {wk['outdoor_min_f']:.1f})")
    if pd.notna(wk.get("lights_out_mean")) and wk.get("lights_out_mean"):
        parts.append(f"lights out ~{wk['lights_out_mean']}, lamp on ~{wk['wake_mean']} "
                     f"({wk['sleep_h_mean']:.1f} h)")
    parts.append(f"{int(wk['days_with_data'])} day(s) of data")
    return "   ·   ".join(parts)


def run(mode: str, args, cfg: dict, script_dir: str) -> int:
    plots.style()
    raw_dir = os.path.abspath(args.raw_dir or os.path.join(script_dir, "raw-data"))
    outdir = os.path.abspath(args.out or os.path.join(script_dir, "analysis_output", mode))
    print(f"[cfg]  raw data: {raw_dir}\n[cfg]  output:   {outdir}")

    series, health = io.load_feeds(raw_dir, cfg)
    boundary = multi_feed_start(series, cfg)
    t_first, t_last = series["indoor_f"].index[0], series["indoor_f"].index[-1]
    if mode == "current":
        if boundary is None and not args.since:
            raise SystemExit("No light-feed exports found, so there is no multi-feed period yet. "
                             "Use analyze_legacy.py for temperature-only data.")
        start = pd.Timestamp(args.since).tz_localize(cfg["timezone"]) if args.since else boundary
        end = t_last
    else:
        start = t_first
        end = pd.Timestamp(args.until).tz_localize(cfg["timezone"]) if args.until else (boundary or t_last)
    if end <= start:
        raise SystemExit(f"Empty window {start} -> {end}.")
    print(f"[cfg]  {mode} window: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}")

    df = io.build_frame(series, start, end)
    df = io.backfill_outdoor(df, cfg, os.path.join(raw_dir, "_cache", "open-meteo-hourly.csv")) \
        if not args.no_weather else df
    df = io.mask_sunlit_sensor(df, cfg)
    df = activity.add_sun(df, cfg)
    df = thermal.add_slope(df, cfg)

    nights, dark_thr, model, coast, guide, forecast = pd.DataFrame(), None, None, pd.DataFrame(), None, None
    if mode == "current":
        nights, dark_thr = activity.detect_activity(df, cfg)
        model = thermal.fit_drift(df, cfg, use_solar="sun_lux" in df)
        coast = thermal.overnight_coast(df, model, cfg)
        if model is not None:
            guide = thermal.preheat_guide(model, cfg)
            if args.forecast:
                forecast = thermal.forecast_tonight(df, model, io.fetch_forecast_hourly(cfg), cfg)

    daily = metrics.daily_table(df, nights, cfg)
    weekly = metrics.weekly_table(daily)
    health_df = metrics.health_table(health, df)
    flags = metrics.build_flags(daily, nights, coast, model, forecast, health_df, df, cfg)

    # one row per night: light-based activity joined with the thermal coast
    if not nights.empty and not coast.empty:
        nights_all = nights.merge(coast, on="night_of", how="outer").sort_values("night_of")
    else:
        nights_all = nights if not nights.empty else coast

    # ---- outputs ----
    for sub in ("data", "plots", "plots/weekly"):
        os.makedirs(os.path.join(outdir, sub), exist_ok=True)
    _clean_outputs(outdir)
    files = []

    def csv(name, table):
        if table is not None and not table.empty:
            report.to_csv(table, os.path.join(outdir, "data", name))
            files.append(f"data/{name}")

    csv("daily_summary.csv", daily)
    csv("weekly_summary.csv", weekly)
    csv("nights.csv", nights_all)
    csv("preheat_guide.csv", guide)
    csv("hourly_timeseries.csv", report.hourly_table(df))
    csv("data_health.csv", health_df)
    csv("flags.csv", pd.DataFrame(flags))

    label = "temperature-only history" if mode == "legacy" else "multi-feed"
    weekly_by_start = {pd.Timestamp(r["week_start"]): r for _, r in weekly.iterrows()}
    for wk0 in _week_starts(df.index[0], df.index[-1], cfg["timezone"]):
        wk1 = local_time(wk0.date(), 24 * 7, cfg["timezone"])
        if df.loc[wk0:wk1, "indoor_f"].notna().sum() < 60:
            continue
        iso = wk0.isocalendar()
        name = f"plots/weekly/week_{iso.year}-W{iso.week:02d}.png"
        plots.plot_dashboard(df, cfg, wk0, wk1, f"Week of {wk0:%a %d %b %Y}  ({iso.year}-W{iso.week:02d})",
                             _week_subtitle(weekly_by_start.get(pd.Timestamp(wk0.date())), cfg),
                             os.path.join(outdir, name), dark_thr)
        files.append(name)
    for day in args.day or []:
        d0 = local_time(day, 0, cfg["timezone"])
        os.makedirs(os.path.join(outdir, "plots", "days"), exist_ok=True)
        name = f"plots/days/day_{day}.png"
        plots.plot_dashboard(df, cfg, d0, local_time(day, 24, cfg["timezone"]), f"{d0:%A %d %B %Y}", "",
                             os.path.join(outdir, name), dark_thr)
        files.append(name)

    plots.plot_calendar_heatmap(df, cfg, os.path.join(outdir, "plots", "calendar_heatmap.png"),
                                f"Temperature calendar - {label}")
    files.append("plots/calendar_heatmap.png")
    plots.plot_daily_trends(daily, cfg, os.path.join(outdir, "plots", "daily_trends.png"),
                            f"Daily temperature and comfort - {label}")
    files.append("plots/daily_trends.png")
    if plots.plot_day_hour_maps(df, cfg, os.path.join(outdir, "plots", "day_hour_maps.png"),
                                f"Day × time-of-day maps - {label}"):
        files.append("plots/day_hour_maps.png")
    if plots.plot_delta_map(df, cfg, os.path.join(outdir, "plots", "indoor_outdoor_delta.png"),
                            f"Indoor − outdoor differential - {label}", model):
        files.append("plots/indoor_outdoor_delta.png")
    if plots.plot_spectrograms(df, cfg, os.path.join(outdir, "plots", "spectrograms.png"),
                               f"Spectrograms - {label}"):
        files.append("plots/spectrograms.png")
    if model is not None:
        plots.plot_thermal_model(df, model, coast, cfg, os.path.join(outdir, "plots", "thermal_model.png"))
        files.append("plots/thermal_model.png")
    if not coast.empty:
        plots.plot_overnight(df, coast, guide, cfg, os.path.join(outdir, "plots", "overnight_coast.png"))
        files.append("plots/overnight_coast.png")
    if not nights.empty:
        plots.plot_activity(df, nights, cfg, os.path.join(outdir, "plots", "nightly_activity.png"))
        files.append("plots/nightly_activity.png")
    if not args.no_html:
        plots.write_explorer(df, nights, cfg, os.path.join(outdir, "explorer.html"),
                             f"Home sensor explorer - {label}")
        files.append("explorer.html")

    report.write_summary(outdir, {"mode": mode, "cfg": cfg, "df": df, "daily": daily, "weekly": weekly,
                                  "nights": nights_all, "model": model, "guide": guide, "forecast": forecast,
                                  "flags": flags, "health": health_df, "files": ["summary.md", "summary.json"] + files})
    print(f"[out]  {len(files) + 2} files written to {outdir}")
    for f in flags:
        if f["severity"] in ("critical", "warning"):
            print(f"[flag] {f['severity']:<8} {f['category']:<10} {f['date']:<11} {f['message']}")
    return 0
