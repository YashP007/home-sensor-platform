"""Static PNG figures (matplotlib) and the zoomable plotly explorer.

Colour roles are fixed across every figure so a reader learns them once:
indoor (living room) = blue, bedroom = magenta, outdoor = orange, LED/lamp light = violet, sunlight = yellow.
"""

from __future__ import annotations

import logging
import math
import warnings

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm, to_rgb
from matplotlib.patches import Patch

from .activity import STATES
from .util import local_time, runs

# ---- palette (validated default categorical slots + chrome) ----
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
INDOOR, OUTDOOR, LED, SUN = "#2a78d6", "#eb6834", "#6250d6", "#eda100"
HUMID, PRESS, BEDROOM = "#1baf7a", "#e87ba4", "#b5338a"
COMFORT_FILL, NIGHT_FILL = "#ebe9e1", "#dde5f0"
COLD, NEUTRAL, HOT = "#1c5cab", "#d6d4cb", "#e34948"
STATE_COLORS = {"daylight": "#fbe6a6", "lights on": LED, "dark (awake/away)": AXIS,
                "lights out (asleep)": "#0d366b", "no data": "#ffffff"}
BLUE_SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

TEMP_CMAP = LinearSegmentedColormap.from_list(
    "temp_div", ["#104281", "#3987e5", "#b7d3f6", "#f0efec", "#f5c4c2", "#e34948", "#9e2a2a"])
OUT_CMAP = LinearSegmentedColormap.from_list(
    "outdoor_seq", ["#fde3d6", "#f6b08f", "#eb6834", "#b8461c", "#7a2c10"])
COOL_CMAP = LinearSegmentedColormap.from_list("cool_seq", BLUE_SEQ[::-1])


def style():
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "font.family": "sans-serif", "font.size": 9.5,
        "font.sans-serif": ["Segoe UI", "Arial", "Helvetica", "DejaVu Sans"],
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.titlecolor": INK,
        "axes.titlesize": 11, "axes.titleweight": "semibold", "axes.titlelocation": "left",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
        "legend.frameon": False, "legend.fontsize": 8.5, "lines.linewidth": 1.6,
        "lines.solid_capstyle": "round",
    })


def _header(fig, title: str, subtitle: str = ""):
    """Title + subtitle pinned in inches from the top, so short and tall figures look the same."""
    h = fig.get_figheight()
    fig.text(0.01, 1 - 0.08 / h, title, ha="left", va="top", fontsize=14, weight="semibold", color=INK)
    if subtitle:
        fig.text(0.01, 1 - 0.36 / h, subtitle, ha="left", va="top", fontsize=9.5, color=INK2)
    fig.get_layout_engine().set(rect=(0, 0, 1, 1 - (0.62 if subtitle else 0.4) / h))


def _save(fig, path):
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _shade_nights(ax, df: pd.DataFrame):
    if "sun_up" not in df:
        return
    idx = df.index
    for s, e in runs(~df["sun_up"].to_numpy()):
        ax.axvspan(idx[s], idx[e], color=NIGHT_FILL, alpha=0.55, lw=0, zorder=0)


def _comfort_band(ax, cfg, label=True):
    lo, hi = cfg["comfort_low_f"], cfg["comfort_high_f"]
    ax.axhspan(lo, hi, color=COMFORT_FILL, alpha=0.7, lw=0, zorder=0,
               label=f"comfort band {lo:g}-{hi:g} °F" if label else None)
    for v in (lo, hi):
        ax.axhline(v, color=AXIS, lw=0.8, ls=(0, (4, 3)), zorder=1)


def _outdoor_lines(ax, w: pd.DataFrame):
    if "outdoor_f" not in w or w["outdoor_f"].isna().all():
        return
    logged = w["outdoor_f"].where(w["outdoor_src"] == "logged")
    model = w["outdoor_f"].where(w["outdoor_src"] == "open-meteo")
    if logged.notna().any():
        ax.plot(w.index, logged, color=OUTDOOR, lw=1.4, label="outdoor (logged)", zorder=3)
    if model.notna().any():
        ax.plot(w.index, model, color=OUTDOOR, lw=1.3, ls=(0, (3, 2)),
                label="outdoor (Open-Meteo hourly)", zorder=3)


def _state_strip(ax, w: pd.DataFrame):
    st = w["state"].to_numpy()
    idx = w.index
    change = np.flatnonzero(st[1:] != st[:-1]) + 1
    bounds = np.concatenate([[0], change, [len(st)]])
    one_min = pd.Timedelta(minutes=1)
    for a, b in zip(bounds[:-1], bounds[1:]):
        x0 = mdates.date2num(idx[a])
        x1 = mdates.date2num(idx[b - 1] + one_min)
        ax.broken_barh([(x0, x1 - x0)], (0, 1), color=STATE_COLORS[st[a]], lw=0,
                       hatch="////" if st[a] == "no data" else None, edgecolor=AXIS)
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.grid(False)
    ax.set_ylabel("activity", rotation=0, ha="right", va="center")


def plot_dashboard(df: pd.DataFrame, cfg: dict, t0, t1, title: str, subtitle: str, path: str,
                   dark_thr: float | None = None):
    """Stacked panels over [t0, t1): temperature, light, activity, humidity, pressure."""
    w = df.loc[t0:t1 - pd.Timedelta(minutes=1)]
    panels = ["temp"]
    if {"led_lux", "sun_lux"} & set(w.columns) and w[[c for c in ("led_lux", "sun_lux") if c in w]].notna().any().any():
        panels.append("light")
    if "state" in w and (w["state"] != "no data").any():
        panels.append("state")
    if any(c in w and w[c].notna().any() for c in ("humidity_pct", "bedroom_humidity_pct")):
        panels.append("humidity_pct")
    if "pressure_hpa" in w and w["pressure_hpa"].notna().any():
        panels.append("pressure_hpa")
    hr = {"temp": 3.4, "light": 1.7, "state": 0.32, "humidity_pct": 1.2, "pressure_hpa": 1.0}
    fig, axes = plt.subplots(len(panels), 1, figsize=(15, 1.4 + sum(hr[p] for p in panels)),
                             sharex=True, gridspec_kw={"height_ratios": [hr[p] for p in panels]},
                             layout="constrained")
    axes = np.atleast_1d(axes)
    _header(fig, title, subtitle)

    for ax, p in zip(axes, panels):
        if p != "state":
            _shade_nights(ax, w)
        if p == "temp":
            _comfort_band(ax, cfg)
            _outdoor_lines(ax, w)
            if "sensor_sunlit" in w and w["sensor_sunlit"].any():
                idx = w.index
                for k, (s, e) in enumerate(runs(w["sensor_sunlit"].to_numpy())):
                    ax.axvspan(idx[s], idx[e], color=SUN, alpha=0.22, lw=0, zorder=0,
                               label="sun on sensor (excluded)" if k == 0 else None)
                ax.plot(idx, w["indoor_raw_f"].where(w["sensor_sunlit"]), color=INDOOR, lw=1.1,
                        ls=(0, (1.5, 1.5)), zorder=4, label="indoor raw (sunlit sensor)")
            has_bed = "bedroom_f" in w and w["bedroom_f"].notna().any()
            if has_bed:
                ax.plot(w.index, w["bedroom_f"], color=BEDROOM, lw=1.4, label="bedroom", zorder=4)
            ax.plot(w.index, w["indoor_f"], color=INDOOR, lw=1.7, label="living room" if has_bed else "indoor",
                    zorder=4)
            # selective direct labels: each day's indoor high and low
            for day, g in w["indoor_f"].groupby(w.index.date):
                if g.notna().sum() < 120:
                    continue
                for t, v, va, off in ((g.idxmax(), g.max(), "bottom", 4), (g.idxmin(), g.min(), "top", -4)):
                    ax.plot(t, v, "o", ms=4, color=INDOOR, mec=SURFACE, mew=1.2, zorder=5)
                    ax.annotate(f"{v:.1f}", (t, v), xytext=(0, off), textcoords="offset points",
                                ha="center", va=va, fontsize=7.5, color=INK2)
            ax.set_ylabel("°F")
            ax.legend(loc="lower left", ncol=6, bbox_to_anchor=(0, 1.0))
            raw = w.get("indoor_raw_f", w["indoor_f"])
            lo = np.nanmin([raw.min(), w.get("outdoor_f", raw).min(), w.get("bedroom_f", raw).min(),
                            cfg["comfort_low_f"]])
            hi = np.nanmax([raw.max(), w.get("outdoor_f", raw).max(), w.get("bedroom_f", raw).max(),
                            cfg["comfort_high_f"]])
            ax.set_ylim(lo - 3, hi + 3)
        elif p == "light":
            for col, c, lab in (("led_lux", LED, "lamp / LED channel"), ("sun_lux", SUN, "sun channel")):
                if col in w:
                    ax.plot(w.index, w[col].clip(lower=0.3), color=c, lw=1.2, label=lab)
            if dark_thr:
                ax.axhline(dark_thr, color=MUTED, lw=0.8, ls=":")
                ax.annotate(f"dark ≤ {dark_thr:.1f} lux", (t1, dark_thr), xytext=(-4, 3), ha="right",
                            textcoords="offset points", fontsize=7.5, color=MUTED)
            ax.set_yscale("log")
            ax.set_ylim(0.3, max(10, np.nanmax(w[[c for c in ("led_lux", "sun_lux") if c in w]].to_numpy())) * 2)
            ax.set_ylabel("lux (log)")
            ax.legend(loc="upper left", ncol=2)
        elif p == "state":
            _state_strip(ax, w)
        elif p == "humidity_pct":
            for col, c, lab in (("humidity_pct", HUMID, "living room"), ("bedroom_humidity_pct", BEDROOM, "bedroom")):
                if col in w and w[col].notna().any():
                    ax.plot(w.index, w[col], color=c, lw=1.3, label=lab)
            ax.axhspan(30, 50, color=COMFORT_FILL, alpha=0.7, lw=0, zorder=0)
            if "bedroom_humidity_pct" in w and w["bedroom_humidity_pct"].notna().any():
                ax.legend(loc="upper left", ncol=2)
            ax.set_ylabel("%RH")
        elif p == "pressure_hpa":
            ax.plot(w.index, w[p], color=PRESS, lw=1.3)
            ax.set_ylabel("hPa")

    midnights = [local_time(d, 0, str(t0.tz)) for d in pd.date_range(t0.date(), t1.date(), freq="D")]
    for ax in axes:
        for d in midnights:
            ax.axvline(d, color=AXIS, lw=0.8, zorder=1)
    ax = axes[-1]
    ax.set_xlim(t0, t1)
    tz = t0.tz
    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=[12], tz=tz))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d %b", tz=tz))
    ax.xaxis.set_minor_locator(mdates.HourLocator(byhour=[0, 6, 18], tz=tz))
    ax.tick_params(axis="x", which="major", length=0, labelsize=9.5)
    handles = [Patch(color=NIGHT_FILL, label="sun down")]
    if "state" in panels:
        handles += [Patch(facecolor=STATE_COLORS[s], edgecolor=AXIS if s == "no data" else "none",
                          hatch="////" if s == "no data" else None, label=s)
                    for s in STATES if (w["state"] == s).any()]
    fig.legend(handles=handles, loc="upper right", ncol=len(handles), bbox_to_anchor=(0.995, 0.997),
               fontsize=8, handlelength=1.4)
    _save(fig, path)


def plot_calendar_heatmap(df: pd.DataFrame, cfg: dict, path: str, title: str):
    hourly = df[["indoor_f"] + (["outdoor_f"] if "outdoor_f" in df else [])].resample("1h").mean()
    hourly["date"] = hourly.index.date
    hourly["hour"] = hourly.index.hour
    ind = hourly.pivot_table(index="date", columns="hour", values="indoor_f", aggfunc="mean", dropna=False)
    ind = ind.reindex(columns=range(24))
    n = len(ind)
    has_out = "outdoor_f" in hourly and hourly["outdoor_f"].notna().any()
    fig, axes = plt.subplots(1, 2 if has_out else 1, figsize=(13 if has_out else 8, 1.9 + 0.2 * n),
                             layout="constrained", squeeze=False)
    lo, hi = cfg["comfort_low_f"], cfg["comfort_high_f"]
    _header(fig, title, "Hourly mean temperature. Each row is one calendar day, read left to right (local time). "
                        "White = no data.")
    mid = (lo + hi) / 2
    norm = TwoSlopeNorm(vcenter=mid, vmin=min(lo - 5, mid - 1), vmax=max(hi + 5, mid + 1))
    cmap = TEMP_CMAP.copy()
    cmap.set_bad("#ffffff")

    def draw(ax, mat, cmap, norm, label):
        m = ax.pcolormesh(np.arange(25), np.arange(n + 1), np.ma.masked_invalid(mat.to_numpy()),
                          cmap=cmap, norm=norm, edgecolors=SURFACE, linewidth=0.4)
        ax.set_ylim(n, 0)
        ax.set_xticks(np.arange(0, 25, 3), [f"{h:02d}" for h in range(0, 25, 3)])
        step = 1 if n <= 35 else 7
        days = list(mat.index)
        ticks = [i for i, d in enumerate(days) if step == 1 or pd.Timestamp(d).weekday() == 0]
        ax.set_yticks(np.array(ticks) + 0.5, [f"{pd.Timestamp(days[i]):%a %d %b}" for i in ticks])
        ax.grid(False)
        ax.set_xlabel("hour of day")
        ax.set_title(label)
        return m

    m = draw(axes[0, 0], ind, cmap, norm, "Indoor")
    cb = fig.colorbar(m, ax=axes[0, 0], shrink=min(0.8, 5 / fig.get_figheight()), pad=0.01, extend="both")
    cb.set_label("°F (centre = middle of comfort band)")
    for v in (lo, hi):
        cb.ax.axhline(v, color=INK, lw=1)
    cb.outline.set_visible(False)
    if has_out:
        out = hourly.pivot_table(index="date", columns="hour", values="outdoor_f", aggfunc="mean",
                                 dropna=False).reindex(index=ind.index, columns=range(24))
        oc = OUT_CMAP.copy()
        oc.set_bad("#ffffff")
        vals = out.to_numpy()
        m2 = draw(axes[0, 1], out, oc, plt.Normalize(np.nanmin(vals), np.nanmax(vals)),
                  "Outdoor (logged, else Open-Meteo)")
        axes[0, 1].set_yticklabels([])
        cb2 = fig.colorbar(m2, ax=axes[0, 1], shrink=min(0.8, 5 / fig.get_figheight()), pad=0.01)
        cb2.set_label("°F")
        cb2.outline.set_visible(False)
    _save(fig, path)


def plot_daily_trends(daily: pd.DataFrame, cfg: dict, path: str, title: str):
    # one row per calendar day, so data gaps break the lines instead of being bridged
    d = daily.set_index(pd.to_datetime(daily["date"]))
    d = d.reindex(pd.date_range(d.index.min(), d.index.max(), freq="D"))
    d["partial_day"] = d["partial_day"].astype("boolean").fillna(False).astype(bool)
    for c in ("hours_below_comfort", "hours_in_comfort", "hours_above_comfort"):
        d[c] = d[c].fillna(0.0)
    x = d.index
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(14, 7.2), sharex=True, layout="constrained",
                                 gridspec_kw={"height_ratios": [3, 1.6]})
    _header(fig, title, "Top: daily range (band) and mean (line). Bottom: hours per day below / inside / "
                        "above the comfort band. Hatched bars = partial day.")
    _comfort_band(a1, cfg)
    if "outdoor_mean_f" in d:
        a1.fill_between(x, d["outdoor_min_f"], d["outdoor_max_f"], color=OUTDOOR, alpha=0.14, lw=0)
        a1.plot(x, d["outdoor_mean_f"], color=OUTDOOR, marker="o", ms=3.5, label="outdoor daily mean")
    a1.fill_between(x, d["indoor_min_f"], d["indoor_max_f"], color=INDOOR, alpha=0.18, lw=0,
                    label="indoor daily min-max")
    a1.plot(x, d["indoor_mean_f"], color=INDOOR, marker="o", ms=3.5, label="indoor daily mean")
    a1.set_ylabel("°F")
    a1.legend(loc="upper left", ncol=4, bbox_to_anchor=(0, 1.1))

    w = 0.8
    bottom = np.zeros(len(d))
    for col, c, lab in (("hours_below_comfort", COLD, f"below {cfg['comfort_low_f']:g} °F"),
                        ("hours_in_comfort", NEUTRAL, "in band"),
                        ("hours_above_comfort", HOT, f"above {cfg['comfort_high_f']:g} °F")):
        v = d[col].to_numpy()
        a2.bar(x, v, w, bottom=bottom, color=c, edgecolor=SURFACE, lw=1, label=lab,
               hatch=[("///" if p else None) for p in d["partial_day"]])
        bottom += v
    a2.set_ylim(0, 24.5)
    a2.set_yticks([0, 6, 12, 18, 24])
    a2.set_ylabel("hours")
    a2.legend(loc="upper left", ncol=3, bbox_to_anchor=(0, 1.22))
    loc = mdates.AutoDateLocator(minticks=6, maxticks=16)
    a2.xaxis.set_major_locator(loc)
    a2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    _save(fig, path)


def _day_hour_grid(s: pd.Series, cfg: dict) -> pd.DataFrame:
    """Reshape a series into rows = days, columns = time-of-day bins, each row starting at
    dayhour_start_hour so a whole night sits on one row. Indexed by the day the row starts on."""
    h0, b = cfg["dayhour_start_hour"], cfg["dayhour_bin_min"]
    wall = s.index.tz_localize(None) - pd.Timedelta(hours=h0)   # wall clock, so DST days stay aligned
    g = pd.DataFrame({"v": s.to_numpy(), "day": wall.normalize(),
                      "bin": (wall.hour * 60 + wall.minute) // b})
    grid = g.pivot_table(index="day", columns="bin", values="v", aggfunc="mean", dropna=False)
    days = pd.date_range(g["day"].min(), g["day"].max(), freq="D")
    return grid.reindex(index=days, columns=range(24 * 60 // b))


def _day_hour_axes(ax, days: pd.DatetimeIndex, cfg: dict, xlabel=True, yticks=True):
    h0, per_h = cfg["dayhour_start_hour"], 60 / cfg["dayhour_bin_min"]
    ax.set_xticks(np.arange(0, 25, 3) * per_h, [f"{(h0 + h) % 24:02d}" for h in range(0, 25, 3)])
    ax.set_xlim(0, 24 * per_h)
    ax.grid(False)
    if xlabel:
        ax.set_xlabel(f"time of day (row = {h0:02d}:00 → {h0:02d}:00 next day)")
    if yticks:
        n = len(days)
        ax.set_ylim(n, 0)
        step = 1 if n <= 35 else 7
        ticks = [i for i, d in enumerate(days) if step == 1 or d.weekday() == 0]
        ax.set_yticks(np.array(ticks) + 0.5, [f"{days[i]:%a %d %b}" for i in ticks])


def plot_day_hour_maps(df: pd.DataFrame, cfg: dict, path: str, title: str) -> bool:
    """Day x time-of-day map per feed: indoor, outdoor, lamp/LED and sun (IR-rich) light."""
    feeds = [("indoor_f", "Indoor temperature", "°F", INDOOR, None),
             ("outdoor_f", "Outdoor temperature (logged, else Open-Meteo)", "°F", OUTDOOR, None),
             ("led_lux", "Lamp / LED channel", "lux", LED, "log"),
             ("sun_lux", "Sun / IR channel", "lux", SUN, "log")]
    feeds = [f for f in feeds if f[0] in df and df[f[0]].notna().any()]
    if not feeds:
        return False
    grids = {f[0]: _day_hour_grid(df[f[0]], cfg) for f in feeds}
    days = grids[feeds[0][0]].index
    for g in grids.values():
        days = days.union(g.index)
    n, ncol = len(days), 2 if len(feeds) > 1 else 1
    nrow = math.ceil(len(feeds) / ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(7.5 * ncol, 1.2 + nrow * (1.0 + 0.17 * n)),
                             layout="constrained", squeeze=False)
    h0 = cfg["dayhour_start_hour"]
    _header(fig, title, f"Each row is one day from {h0:02d}:00 to {h0:02d}:00 the next morning, so a night "
                        f"is never split; {cfg['dayhour_bin_min']} min bins, local time. Sun-on-sensor indoor "
                        f"minutes are excluded. White = no data.")
    for k, (col, label, unit, color, transform) in enumerate(feeds):
        ax = axes.flat[k]
        mat = grids[col].reindex(days).to_numpy(dtype=float)
        if transform == "log":
            mat = np.log10(1 + np.clip(mat, 0, None))
        if col == "indoor_f":
            cmap = TEMP_CMAP.copy()
            lo, hi = cfg["comfort_low_f"], cfg["comfort_high_f"]
            mid = (lo + hi) / 2
            norm = TwoSlopeNorm(vcenter=mid, vmin=min(lo - 5, mid - 1), vmax=max(hi + 5, mid + 1))
        else:
            cmap = _role_cmap(f"dh_{col}", color)
            vmin, vmax = (0, np.nanmax(mat)) if transform == "log" else np.nanpercentile(mat, [1, 99])
            norm = plt.Normalize(vmin, max(vmax, vmin + 1e-6))
        cmap.set_bad("#ffffff")
        m = ax.pcolormesh(np.arange(mat.shape[1] + 1), np.arange(n + 1), np.ma.masked_invalid(mat),
                          cmap=cmap, norm=norm, shading="flat", rasterized=True)
        _day_hour_axes(ax, days, cfg, xlabel=k >= len(feeds) - ncol)
        if k % ncol:
            ax.set_yticklabels([])
        ax.set_title(label)
        cb = fig.colorbar(m, ax=ax, pad=0.01, aspect=25, extend="both" if col == "indoor_f" else "neither")
        cb.outline.set_visible(False)
        cb.set_label(unit)
        if transform == "log":
            lux = [10 ** p for p in range(int(norm.vmax) + 1) if math.log10(1 + 10 ** p) <= norm.vmax]
            cb.set_ticks(np.log10(1 + np.array(lux, dtype=float)), labels=[f"{v:,}" for v in lux])
        if col == "indoor_f":
            for v in (cfg["comfort_low_f"], cfg["comfort_high_f"]):
                cb.ax.axhline(v, color=INK, lw=1)
    for ax in list(axes.flat)[len(feeds):]:
        ax.set_visible(False)
    _save(fig, path)
    return True


def plot_delta_map(df: pd.DataFrame, cfg: dict, path: str, title: str, model=None) -> bool:
    """Indoor - outdoor differential as a day x time-of-day map, with the per-day mean at right
    and the median / IQR daily profile underneath. With a fitted drift model the colour bar
    also reads as the passive drift that differential drives (-K_COUPLE * dT, °F/h)."""
    if "outdoor_f" not in df or not df["outdoor_f"].notna().any():
        return False
    grid = _day_hour_grid(df["indoor_f"] - df["outdoor_f"], cfg)
    mat = grid.to_numpy(dtype=float)
    if not np.isfinite(mat).any():
        return False
    days, (n, nb) = grid.index, mat.shape
    lim = max(2.0, np.nanpercentile(np.abs(mat), 99))
    norm = TwoSlopeNorm(vcenter=0, vmin=-lim, vmax=lim)
    cmap = TEMP_CMAP.copy()
    cmap.set_bad("#ffffff")

    fig, axes = plt.subplots(2, 2, figsize=(13, 4.6 + 0.17 * n), layout="constrained",
                             gridspec_kw={"width_ratios": [6, 1], "height_ratios": [max(2.2, 0.6 + 0.17 * n), 2.4]})
    (ax, side), (prof, corner) = axes
    corner.set_visible(False)
    _header(fig, title, "Red = indoor warmer (heat leaks out), blue = outdoor warmer (heat leaks in). "
                        "Right: daily mean. Bottom: median by time of day, band = interquartile range. "
                        "White = no data.")
    m = ax.pcolormesh(np.arange(nb + 1), np.arange(n + 1), np.ma.masked_invalid(mat), cmap=cmap, norm=norm,
                      shading="flat", rasterized=True)
    _day_hour_axes(ax, days, cfg, xlabel=False)
    ax.set_title("Indoor − outdoor temperature")

    # per-day mean, only for rows with at least a quarter of the day covered
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        daily = np.where(np.isfinite(mat).sum(axis=1) >= nb / 4, np.nanmean(mat, axis=1), np.nan)
        q25, q50, q75 = np.nanpercentile(mat, [25, 50, 75], axis=0)
    ok = np.isfinite(daily)
    side.barh(np.arange(n)[ok] + 0.5, daily[ok], height=0.8, color=[cmap(norm(v)) for v in daily[ok]])
    side.axvline(0, color=AXIS, lw=0.8)
    side.set_ylim(n, 0)
    side.set_yticks([])
    side.grid(axis="y", visible=False)
    side.set_xlabel("daily mean, °F")
    side.set_title("Per day")

    x = np.arange(nb) + 0.5
    prof.fill_between(x, q25, q75, color=BLUE_SEQ[1], alpha=0.7, lw=0, label="interquartile range")
    prof.plot(x, q50, color=INK, lw=1.5, label="median")
    prof.axhline(0, color=AXIS, lw=0.8)
    _day_hour_axes(prof, days, cfg, yticks=False)
    prof.grid(True, axis="y")
    prof.set_ylabel("indoor − outdoor, °F")
    prof.legend(loc="lower left", fontsize=8)

    cb = fig.colorbar(m, ax=side, pad=0.04, aspect=12, extend="both")
    cb.set_label("indoor − outdoor, °F")
    cb.outline.set_visible(False)
    if model is not None and model.k_couple > 0:
        k_h = model.k_couple * 60
        sec = cb.ax.secondary_yaxis("left", functions=(lambda d: -k_h * d, lambda r: -r / k_h))
        sec.set_ylabel(f"passive drift, °F/h (τ {model.tau_h:.0f} h)")
    _save(fig, path)
    return True


# (column, title, role colour, transform); light is log-compressed so lamp on/off steps
# don't swamp everything else
SPEC_FEEDS = [("indoor_f", "Indoor temperature (°F)", INDOOR, None),
              ("outdoor_f", "Outdoor temperature (°F, logged, else Open-Meteo hourly)", OUTDOOR, None),
              ("led_lux", "Lamp / LED channel, log10(1 + lux)", LED, "log"),
              ("sun_lux", "Sun channel, log10(1 + lux)", SUN, "log"),
              ("humidity_pct", "Humidity (%RH)", HUMID, None),
              ("pressure_hpa", "Pressure (hPa)", PRESS, None)]
PERIOD_TICKS = [(1, "24 h"), (2, "12 h"), (4, "6 h"), (8, "3 h"), (24, "1 h"), (72, "20 min"), (144, "10 min")]


def _role_cmap(name: str, color: str) -> LinearSegmentedColormap:
    """Sequential ramp through a feed's role colour: pale tint -> colour -> near-ink."""
    c, w, k = np.array(to_rgb(color)), np.ones(3), np.array(to_rgb(INK))
    cmap = LinearSegmentedColormap.from_list(name, [0.85 * w + 0.15 * c, c, 0.55 * c + 0.45 * k])
    cmap.set_bad("#ffffff")
    return cmap


def _stft(x: np.ndarray, win: int, step: int, max_gap: float):
    """Hann-windowed power spectrum per window. Short gaps are linearly interpolated and each
    window is linearly detrended so slow drift doesn't leak into every bin; windows missing
    more than max_gap of their samples come back NaN."""
    starts = np.arange(0, len(x) - win + 1, step)
    taper = np.hanning(win)
    t = np.arange(win)
    out = np.full((win // 2 + 1, len(starts)), np.nan)
    for j, s in enumerate(starts):
        seg = x[s:s + win]
        ok = np.isfinite(seg)
        if ok.mean() < 1 - max_gap:
            continue
        seg = np.interp(t, t[ok], seg[ok])
        seg = seg - np.polyval(np.polyfit(t, seg, 1), t)
        out[:, j] = np.abs(np.fft.rfft(seg * taper)) ** 2 / (taper ** 2).sum()
    return starts, out


def plot_spectrograms(df: pd.DataFrame, cfg: dict, path: str, title: str) -> bool:
    """One spectrogram row per feed (time × period), plus the record's median spectrum at right.
    Returns False when the record is too short to fill even a few windows."""
    feeds = [f for f in SPEC_FEEDS if f[0] in df and df[f[0]].notna().any()]
    dt_min = (df.index[1] - df.index[0]) / pd.Timedelta(minutes=1)
    win = int(min(cfg["spectrogram_window_h"] * 60, len(df) // 3 * dt_min) / dt_min) // 2 * 2
    if not feeds or win * dt_min < 12 * 60:
        return False
    step = max(int(60 / dt_min), len(df) // 600)
    freqs = np.fft.rfftfreq(win, d=dt_min / 1440)          # cycles per day
    keep = (freqs >= 2 * freqs[1]) & (freqs <= min(144, freqs[-1]))
    f = freqs[keep]
    f_edges = np.concatenate([[f[0] ** 1.5 / f[1] ** 0.5], np.sqrt(f[1:] * f[:-1]), [f[-1] ** 1.5 / f[-2] ** 0.5]])

    n = len(feeds)
    fig, axes = plt.subplots(n, 2, figsize=(15, 1.4 + 2.1 * n), layout="constrained", squeeze=False,
                             sharey=True, gridspec_kw={"width_ratios": [6, 1.25]})
    _header(fig, title,
            f"Power by period over time ({win * dt_min / 60:.0f} h Hann windows, linear trend removed). "
            f"Colour = dB vs that period's typical level (dark = unusually active; 24 h = daily cycle, "
            f"< 1 h = HVAC / lamp switching). White = no data. Right: median spectrum.")
    for row, (col, label, color, transform) in enumerate(feeds):
        x = df[col].to_numpy(dtype=float)
        if transform == "log":
            x = np.log10(1 + np.clip(x, 0, None))
        starts, psd = _stft(x, win, step, cfg["spectrogram_max_gap_frac"])
        with np.errstate(divide="ignore"):
            db = 10 * np.log10(psd[keep])
        if not np.isfinite(db).any():
            continue
        if col == "outdoor_f" and "outdoor_src" in df:
            # Open-Meteo is hourly, interpolated: nothing real below a 2 h period
            src = (df["outdoor_src"] == "open-meteo").to_numpy()
            coarse = np.array([src[s:s + win].mean() > cfg["spectrogram_max_gap_frac"] for s in starts])
            db[np.ix_(f > 12, coarse)] = np.nan
        # the spectrum falls steeply with frequency, so colour each period against its own
        # typical level (shows *when* it is active); the side panel keeps the absolute shape
        typical = np.nanmedian(db, axis=1)
        db -= typical[:, None]
        t_mid = mdates.date2num(df.index[starts + win // 2])
        half = step * dt_min / 1440 / 2
        t_edges = np.concatenate([t_mid - half, [t_mid[-1] + half]])

        ax, side = axes[row]
        lim = max(1.0, np.nanpercentile(np.abs(db), 98))
        lo, hi = -lim, lim
        m = ax.pcolormesh(t_edges, f_edges, np.ma.masked_invalid(db), cmap=_role_cmap(f"spec_{col}", color),
                          vmin=lo, vmax=hi, shading="flat", rasterized=True)
        ax.set_yscale("log")
        ax.set_ylim(f_edges[-1], f_edges[0])
        ax.set_yticks([p for p, _ in PERIOD_TICKS if f_edges[0] <= p <= f_edges[-1]],
                      [s for p, s in PERIOD_TICKS if f_edges[0] <= p <= f_edges[-1]])
        ax.yaxis.set_minor_locator(plt.NullLocator())
        ax.grid(False)
        ax.set_ylabel("period")
        ax.set_title(label)
        ax.set_xlim(t_edges[0], t_edges[-1])
        loc = mdates.AutoDateLocator(minticks=6, maxticks=16, tz=df.index.tz)
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc, tz=df.index.tz))

        side.plot(typical - np.nanmedian(typical), f, color=color, lw=1.4)
        for p, _ in PERIOD_TICKS[:2]:
            side.axhline(p, color=AXIS, lw=0.8, ls=":")
        side.set_xlabel("median power, dB")
        side.tick_params(axis="y", labelleft=False)
        cb = fig.colorbar(m, ax=side, pad=0.04, aspect=12)
        cb.set_label("dB vs typical")
        cb.outline.set_visible(False)
    _save(fig, path)
    return True


def plot_thermal_model(df: pd.DataFrame, model, coast: pd.DataFrame, cfg: dict, path: str):
    sub = df.iloc[::cfg["fit_step_min"]].dropna(subset=["indoor_f", "outdoor_f", "dTdt"])
    sub = sub[sub["dTdt"].abs() <= cfg["fit_max_abs_slope"]]
    sun = np.log10(1 + sub["sun_lux"].fillna(0)) if model.uses_solar else 0
    has_val = "pred_err_f" in coast and coast["pred_err_f"].notna().any()
    ncol = 1 + int(model.uses_solar) + int(has_val)
    fig, axes = plt.subplots(1, ncol, figsize=(5 * ncol, 4.8), layout="constrained", squeeze=False)
    axes = axes[0]
    ci = model.tau_h_ci
    _header(fig, "Passive thermal model",
            f"dT/dt = K_COUPLE·(T_out−T) + K_SOLAR·log10(1+sun_lux) + K_BIAS   ·   "
            f"τ = {model.tau_h:.1f} h (90% CI {ci[0]:.0f}–{ci[1]:.0f} h)   ·   no heat, no sun → settles "
            f"{model.offset_f:+.1f} °F vs outdoor   ·   R² {model.r2:.2f}, {model.days} days")

    ax = axes[0]
    gap = sub["outdoor_f"] - sub["indoor_f"]
    y = (sub["dTdt"] - model.k_solar * sun) * 60
    ax.scatter(gap, y, s=10, color=INDOOR, alpha=0.35, lw=0)
    xs = np.linspace(gap.min(), gap.max(), 50)
    ax.plot(xs, (model.k_couple * xs + model.k_bias) * 60, color=INK, lw=2)
    ax.axhline(0, color=AXIS, lw=0.8)
    ax.axvline(-model.offset_f, color=MUTED, lw=0.8, ls=":")
    ax.annotate(f"balance: indoor {model.offset_f:.1f} °F above outdoor", (-model.offset_f, ax.get_ylim()[1]),
                xytext=(4, -10), textcoords="offset points", fontsize=8, color=INK2)
    ax.set_xlabel("outdoor − indoor (°F)")
    ax.set_ylabel("indoor drift, sun removed (°F/h)")
    ax.set_title("Coupling to outdoors")

    k = 1
    if model.uses_solar:
        ax = axes[k]
        k += 1
        day = sub["sun_lux"].fillna(0) > 1
        yr = (sub["dTdt"] - model.k_couple * gap - model.k_bias)[day] * 60
        ax.scatter(1 + sub["sun_lux"][day], yr, s=10, color=SUN, alpha=0.5, lw=0)
        xs = np.logspace(0, math.log10(max(10, sub["sun_lux"].max() + 1)), 50)
        ax.plot(xs, model.k_solar * np.log10(xs) * 60, color=INK, lw=2)
        ax.axhline(0, color=AXIS, lw=0.8)
        ax.set_xscale("log")
        ax.set_xlabel("1 + sun-channel lux (log)")
        ax.set_ylabel("drift left after outdoor term (°F/h)")
        ax.set_title(f"Solar gain: {model.k_solar * 60:+.2f} °F/h per decade of lux")

    if has_val:
        ax = axes[k]
        c = coast.dropna(subset=["pred_end_f"])
        ax.scatter(c["end_f"], c["pred_end_f"], s=40, color=INDOOR, edgecolor=SURFACE, lw=1.5, zorder=3)
        lim = [min(c["end_f"].min(), c["pred_end_f"].min()) - 1, max(c["end_f"].max(), c["pred_end_f"].max()) + 1]
        ax.plot(lim, lim, color=AXIS, lw=1)
        ax.fill_between(lim, np.array(lim) - 1, np.array(lim) + 1, color=COMFORT_FILL, alpha=0.6, lw=0,
                        label="±1 °F")
        for _, r in c.iterrows():
            ax.annotate(f"{pd.Timestamp(r['night_of']):%d %b}", (r["end_f"], r["pred_end_f"]),
                        xytext=(5, -3), textcoords="offset points", fontsize=7.5, color=INK2)
        ax.set_xlim(lim)
        ax.set_ylim(lim)
        ax.set_aspect("equal")
        ax.set_xlabel(f"actual at {cfg['coast_end_hour']:02d}:00 (°F)")
        ax.set_ylabel(f"model, from {cfg['coast_start_hour']:02d}:00 (°F)")
        ax.set_title(f"Overnight check: MAE {c['pred_err_f'].abs().mean():.1f} °F")
        ax.legend(loc="upper left")
    _save(fig, path)


def plot_overnight(df: pd.DataFrame, coast: pd.DataFrame, guide: pd.DataFrame | None, cfg: dict, path: str):
    tz, h0, h1 = cfg["timezone"], cfg["coast_start_hour"], cfg["coast_end_hour"]
    ncol = 2 if guide is not None and not guide.empty else 1
    fig, axes = plt.subplots(1, ncol, figsize=(7 * ncol, 5.2), layout="constrained", squeeze=False)
    axes = axes[0]
    _header(fig, "Overnight coast",
            f"How the apartment cools from {h0:02d}:00 to {h1:02d}:00 with no heat, and what that means "
            f"for pre-heating. Right: lines = thermal model; dots = real nights, labelled with the "
            f"{h1:02d}:00 result.")
    ax = axes[0]
    c = coast.dropna(subset=["outdoor_mean_f"]) if "outdoor_mean_f" in coast else coast
    vmin, vmax = (c["outdoor_mean_f"].min(), c["outdoor_mean_f"].max()) if len(c) and "outdoor_mean_f" in c else (0, 1)
    norm = plt.Normalize(vmin - 0.5, vmax + 0.5)
    ends = []
    for _, r in coast.iterrows():
        t0 = local_time(r["night_of"], h0, tz)
        seg = df.loc[t0:local_time(r["night_of"], 24 + h1, tz), "indoor_f"]
        hrs = (seg.index - t0) / pd.Timedelta(hours=1)
        col = COOL_CMAP(norm(r["outdoor_mean_f"])) if "outdoor_mean_f" in r and np.isfinite(r["outdoor_mean_f"]) else MUTED
        ax.plot(hrs, seg, color=col, lw=1.5, alpha=0.9)
        ends.append([seg.dropna().iloc[-1], f"{pd.Timestamp(r['night_of']):%d %b}", hrs[-1]])
    if len(ends) <= 14:
        # end-of-line labels, nudged apart so neighbouring nights don't overprint
        span = np.subtract(*ax.get_ylim()[::-1])
        ends.sort()
        for k in range(1, len(ends)):
            ends[k][0] = max(ends[k][0], ends[k - 1][0] + 0.045 * span)
        for y, lab, x in ends:
            ax.annotate(lab, (x, y), xytext=(4, 0), textcoords="offset points", fontsize=7.5,
                        color=INK2, va="center", annotation_clip=False)
    _comfort_band(ax, cfg, label=False)
    ax.set_xticks(range(0, 24 + h1 - h0 + 1, 2),
                  [f"{(h0 + k) % 24:02d}:00" for k in range(0, 24 + h1 - h0 + 1, 2)])
    ax.set_ylabel("indoor °F")
    ax.set_title("Each night, coloured by mean outdoor temperature")
    if len(c):
        sm = plt.cm.ScalarMappable(norm=norm, cmap=COOL_CMAP)
        cb = fig.colorbar(sm, ax=ax, shrink=0.8, pad=0.01)
        cb.set_label("overnight outdoor mean °F")
        cb.outline.set_visible(False)

    if ncol == 2:
        ax = axes[1]
        targets = cfg["morning_targets_f"]
        shades = [BLUE_SEQ[2], BLUE_SEQ[4], BLUE_SEQ[6]][:len(targets)]
        for target, col in zip(targets, shades):
            for h_start, ls in zip(cfg["preheat_start_hours"], ["-", (0, (4, 2)), ":"]):
                g = guide[(guide["target_f"] == target) & (guide["start_hour"] == h_start)].dropna(
                    subset=["required_start_f"])
                if g.empty:
                    continue
                ax.plot(g["outdoor_f"], g["required_start_f"], color=col, ls=ls, lw=2)
                if h_start == max(cfg["preheat_start_hours"]):
                    ax.annotate(f"wake ≥ {target:g} °F", (g["outdoor_f"].iloc[-1], g["required_start_f"].iloc[-1]),
                                xytext=(4, 0), textcoords="offset points", fontsize=8, color=INK2, va="center")
        if len(c):
            ax.scatter(c["outdoor_mean_f"], c["start_f"], s=36, color=INK, edgecolor=SURFACE, lw=1.5, zorder=4,
                       label=f"actual nights (temp at {h0:02d}:00)")
            for _, r in c.iterrows():
                ax.annotate(f"→ {r['end_f']:.1f}", (r["outdoor_mean_f"], r["start_f"]), xytext=(5, -9),
                            textcoords="offset points", fontsize=7.5, color=INK2)
        styles = [plt.Line2D([], [], color=INK2, ls=ls, lw=2, label=f"indoor needed at {h:02d}:00")
                  for h, ls in zip(cfg["preheat_start_hours"], ["-", (0, (4, 2)), ":"])]
        ax.legend(handles=styles + ax.get_legend_handles_labels()[0], loc="upper right")
        ax.set_xlabel("overnight mean outdoor °F")
        ax.set_ylabel("indoor °F needed when heat goes off")
        ax.set_title(f"Pre-heat guide (to still be at target at {h1:02d}:00)")
        ax.set_xlim(guide["outdoor_f"].min(), guide["outdoor_f"].max() + 6)
    _save(fig, path)


def plot_activity(df: pd.DataFrame, nights: pd.DataFrame, cfg: dict, path: str):
    tz = cfg["timezone"]
    nn = nights[nights.get("lights_out", pd.Series(dtype=object)).notna()] if "lights_out" in nights else nights
    days = list(nights["night_of"])
    fig, ax = plt.subplots(figsize=(13, 1.9 + 0.42 * len(days)), layout="constrained")
    _header(fig, "Nightly activity (living-room light)",
            "Each row is one evening → next noon. Lights out ≈ bedtime proxy: the sensor is in the living room, "
            "so it cannot see the bedroom.")
    for i, day in enumerate(days):
        t0 = local_time(day, 18, tz)
        w = df.loc[t0:local_time(day, 36, tz) - pd.Timedelta(minutes=1)]
        if w.empty:
            continue
        st = w["state"].to_numpy()
        hrs = (w.index - t0) / pd.Timedelta(hours=1)
        change = np.flatnonzero(st[1:] != st[:-1]) + 1
        b = np.concatenate([[0], change, [len(st)]])
        for a, e in zip(b[:-1], b[1:]):
            ax.broken_barh([(hrs[a], hrs[e - 1] - hrs[a] + 1 / 60)], (i - 0.36, 0.72),
                           color=STATE_COLORS[st[a]], lw=0, hatch="////" if st[a] == "no data" else None,
                           edgecolor=AXIS)
    for i, (_, r) in enumerate(nights.iterrows()):
        txt = (f"{r['lights_out']:%H:%M} → {r['wake']:%H:%M}  ·  {r['sleep_h']:.1f} h"
               if "lights_out" in r and pd.notna(r["lights_out"]) else r.get("note", ""))
        ax.text(18.15, i, txt, va="center", fontsize=8.5, color=INK2)
    ax.set_yticks(range(len(days)), [f"{pd.Timestamp(d):%a %d %b}" for d in days])
    ax.set_ylim(len(days) - 0.5, -0.5)
    ax.set_xlim(0, 18)
    ax.set_xticks(range(0, 19, 2), [f"{(18 + k) % 24:02d}:00" for k in range(0, 19, 2)])
    ax.grid(axis="y", visible=False)
    ax.spines["left"].set_visible(False)
    handles = [Patch(facecolor=STATE_COLORS[s], edgecolor=AXIS if s == "no data" else "none",
                     hatch="////" if s == "no data" else None, label=s)
               for s in STATES if (df["state"] == s).any()]
    ax.legend(handles=handles, loc="lower left", ncol=len(handles), bbox_to_anchor=(0, 1.0))
    _save(fig, path)


def write_explorer(df: pd.DataFrame, nights: pd.DataFrame, cfg: dict, path: str, title: str):
    """Zoomable full-record HTML (plotly loaded from CDN, so the file stays small)."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    span_days = (df.index[-1] - df.index[0]) / pd.Timedelta(days=1)
    r = df.resample("2min" if span_days <= 31 else "10min").mean(numeric_only=True)
    rows = [("temp", "°F")]
    if "led_lux" in r or "sun_lux" in r:
        rows.append(("light", "lux"))
    if any(c in r and r[c].notna().any() for c in ("humidity_pct", "bedroom_humidity_pct")):
        rows.append(("humidity", "%RH"))
    if "pressure_hpa" in r and r["pressure_hpa"].notna().any():
        rows.append(("pressure_hpa", "hPa"))
    fig = make_subplots(rows=len(rows), cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[3] + [1.4] * (len(rows) - 1))
    for i, (name, unit) in enumerate(rows, 1):
        if name == "temp":
            fig.add_hrect(cfg["comfort_low_f"], cfg["comfort_high_f"], fillcolor=COMFORT_FILL, opacity=0.6,
                          line_width=0, row=i, col=1)
            if "outdoor_f" in r:
                fig.add_trace(go.Scattergl(x=r.index, y=r["outdoor_f"], name="outdoor", line=dict(color=OUTDOOR, width=1.4)), i, 1)
            if "bedroom_f" in r:
                fig.add_trace(go.Scattergl(x=r.index, y=r["bedroom_f"], name="bedroom",
                                           line=dict(color=BEDROOM, width=1.4)), i, 1)
            fig.add_trace(go.Scattergl(x=r.index, y=r["indoor_f"], name="living room",
                                       line=dict(color=INDOOR, width=1.6)), i, 1)
        elif name == "light":
            for col, c, lab in (("led_lux", LED, "lamp / LED lux"), ("sun_lux", SUN, "sun lux")):
                if col in r:
                    fig.add_trace(go.Scattergl(x=r.index, y=r[col].clip(lower=0.3), name=lab,
                                               line=dict(color=c, width=1.2)), i, 1)
            fig.update_yaxes(type="log", row=i, col=1)
        elif name == "humidity":
            for col, c, lab in (("humidity_pct", HUMID, "living %RH"), ("bedroom_humidity_pct", BEDROOM, "bedroom %RH")):
                if col in r:
                    fig.add_trace(go.Scattergl(x=r.index, y=r[col], name=lab, line=dict(color=c, width=1.2)), i, 1)
        else:
            fig.add_trace(go.Scattergl(x=r.index, y=r[name], name=name, line=dict(color=PRESS, width=1.2)), i, 1)
        fig.update_yaxes(title_text=unit, row=i, col=1, gridcolor=GRID)
    if not nights.empty and "lights_out" in nights:
        for _, n in nights.dropna(subset=["lights_out"]).iterrows():
            fig.add_vrect(n["lights_out"], n["wake"], fillcolor="#0d366b", opacity=0.08, line_width=0)
    fig.update_xaxes(gridcolor=GRID, rangeslider=dict(visible=False))
    fig.update_xaxes(rangeselector=dict(buttons=[dict(count=1, label="1d", step="day", stepmode="backward"),
                                                 dict(count=7, label="1w", step="day", stepmode="backward"),
                                                 dict(step="all", label="all")]), row=1, col=1)
    fig.update_layout(title=title, template="plotly_white", hovermode="x unified", height=320 + 180 * len(rows),
                      paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, font=dict(family="Segoe UI, Arial", color=INK),
                      legend=dict(orientation="h", y=1.03, x=0))
    fig.write_html(path, include_plotlyjs="cdn", full_html=True)
