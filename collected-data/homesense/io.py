"""Feed discovery, cleaning, alignment to a 1-minute grid, Open-Meteo outdoor backfill.

Adafruit IO exports are named home-<feed>-<YYYYMMDD>-<HHMM>.csv with columns
id,value,feed_id,created_at,lat,lon,ele and created_at in UTC. Every export of a
feed is concatenated and de-duplicated by record id, so overlapping exports are
safe to leave in raw-data/.
"""

from __future__ import annotations

import glob
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# feed key in the filename -> (column name, unit, how to fill the 1-min grid, max fill gap in min)
#   "interp": sampled continuously (temperature every 30 s, outdoor every 15 min)
#   "hold":   published on change + heartbeat, so the last value stays valid until the next one
FEEDS = {
    "temperature":  ("indoor_f",     "°F",   "interp", 6),     # living room (main board)
    "bedroom-temperature": ("bedroom_f", "°F", "interp", 6),  # bedroom (peripheral board)
    "outdoor-temp": ("outdoor_f",    "°F",   "interp", 45),
    "led-lux":      ("led_lux",      "lux",  "hold",   10),
    "sun-lux":      ("sun_lux",      "lux",  "hold",   10),
    "humidity":     ("humidity_pct", "%RH",  "hold",   70),
    "bedroom-humidity": ("bedroom_humidity_pct", "%RH", "hold", 70),
    "pressure":     ("pressure_hpa", "hPa",  "hold",   70),
}
# Text feeds of space-separated key=value diagnostics, one per board -> room name.
STATUS_FEEDS = {"living-status": "living", "bedroom-status": "bedroom"}
TEMP_COLS = ("indoor_f", "bedroom_f")
FILE_RE = re.compile(r"^home-(?P<feed>[a-z0-9-]+?)-(?P<stamp>\d{8}-\d{4})\.csv$")


@dataclass
class FeedHealth:
    column: str
    files: list[str]
    raw_rows: int = 0
    duplicates: int = 0
    out_of_range: int = 0
    spikes: int = 0
    warmup: int = 0
    first: pd.Timestamp | None = None
    last: pd.Timestamp | None = None
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = field(default_factory=list)


def discover(raw_dir: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.csv"))):
        m = FILE_RE.match(os.path.basename(path))
        if not m:
            continue
        if m["feed"] in STATUS_FEEDS:
            continue                                    # parsed by load_status()
        if m["feed"] not in FEEDS:
            print(f"[load] ignoring unknown feed '{m['feed']}' ({os.path.basename(path)})")
            continue
        found.setdefault(m["feed"], []).append(path)
    return found


def _spike_mask(s: pd.Series, window: int, thresh: float) -> pd.Series:
    med = s.rolling(window, center=True, min_periods=3).median()
    return (s - med).abs() > thresh


def load_feeds(raw_dir: str, cfg: dict) -> tuple[dict[str, pd.Series], dict[str, FeedHealth]]:
    """Return {column: cleaned raw-sample Series (tz-aware local index)} and per-feed health."""
    tz = cfg["timezone"]
    series, health = {}, {}
    for feed, paths in discover(raw_dir).items():
        col, unit, _, gap_min = FEEDS[feed]
        h = FeedHealth(column=col, files=[os.path.basename(p) for p in paths])
        df = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
        h.raw_rows = len(df)
        if "id" in df.columns:
            df = df.drop_duplicates(subset="id")
        df = df.drop_duplicates(subset=["created_at", "value"])
        h.duplicates = h.raw_rows - len(df)

        ts = pd.to_datetime(df["created_at"], utc=True, errors="coerce").dt.tz_convert(tz)
        s = pd.Series(pd.to_numeric(df["value"], errors="coerce").to_numpy(), index=ts)
        s = s[s.index.notna()].dropna().sort_index()
        s = s[~s.index.duplicated(keep="first")]

        if col in ("led_lux", "sun_lux"):
            s[s <= cfg["lux_floor"] * 1.001] = 0.0
        lo, hi = cfg["valid_range"].get(col, [-np.inf, np.inf])
        bad = (s < lo) | (s > hi)
        h.out_of_range = int(bad.sum())
        s = s[~bad]
        if col in cfg["spike_threshold"] and len(s) > cfg["spike_window_samples"]:
            spikes = _spike_mask(s, cfg["spike_window_samples"], cfg["spike_threshold"][col])
            h.spikes = int(spikes.sum())
            s = s[~spikes]

        if col in TEMP_COLS and len(s) > 1:
            gap_before = s.index.to_series().diff().fillna(pd.Timedelta(days=1))
            boots = s.index[gap_before > pd.Timedelta(minutes=cfg["warmup_gap_min"])]
            warm = np.zeros(len(s), dtype=bool)
            for b in boots:
                warm |= (s.index >= b) & (s.index < b + pd.Timedelta(minutes=cfg["warmup_drop_min"]))
            h.warmup = int(warm.sum())
            s = s[~warm]

        if s.empty:
            continue
        h.first, h.last = s.index[0], s.index[-1]
        d = s.index.to_series().diff()
        for end, dt in d[d > pd.Timedelta(minutes=gap_min)].items():
            h.gaps.append((end - dt, end))
        series[col], health[col] = s, h
        print(f"[load] {col:<13} {len(s):>7} samples  {h.first:%Y-%m-%d %H:%M} -> "
              f"{h.last:%Y-%m-%d %H:%M}  ({len(paths)} file(s), {h.duplicates} dup, "
              f"{h.out_of_range + h.spikes + h.warmup} rejected)")
    if "indoor_f" not in series:
        raise SystemExit("No home-temperature-*.csv export found - nothing to analyse.")
    return series, health


def load_status(raw_dir: str, cfg: dict) -> pd.DataFrame:
    """Parse the per-board status text feeds into one row per message (local ts, room, keys...)."""
    frames = []
    for path in sorted(glob.glob(os.path.join(raw_dir, "*.csv"))):
        m = FILE_RE.match(os.path.basename(path))
        if not m or m["feed"] not in STATUS_FEEDS:
            continue
        df = pd.read_csv(path, dtype={"value": str})
        kv = df["value"].fillna("").map(
            lambda v: dict(p.split("=", 1) for p in v.split() if "=" in p))
        out = pd.DataFrame(kv.tolist()).apply(pd.to_numeric, errors="coerce")
        out.insert(0, "room", STATUS_FEEDS[m["feed"]])
        out.insert(0, "ts", pd.to_datetime(df["created_at"], utc=True, errors="coerce").dt.tz_convert(cfg["timezone"]))
        out["id"] = df.get("id")
        frames.append(out)
    if not frames:
        return pd.DataFrame()
    st = pd.concat(frames, ignore_index=True)
    st = st.drop_duplicates(subset="id" if st["id"].notna().all() else ["ts", "room"]).drop(columns="id")
    st = st.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    if {"full", "ir"} <= set(st.columns):
        st["ir_ratio"] = np.where(st["full"] > 0, st["ir"] / st["full"], np.nan)
    print(f"[load] status        {len(st):>7} messages from {sorted(st['room'].unique())}")
    return st


def _blank_long_gaps(grid_vals: pd.Series, obs_index: pd.DatetimeIndex, max_gap_min: float):
    """NaN every grid point that sits inside an observation gap longer than max_gap_min."""
    if len(obs_index) < 2:
        return grid_vals
    obs = obs_index.as_unit("ns").asi8                  # pandas 3 may infer us/s units
    g = grid_vals.index.as_unit("ns").asi8
    nxt = np.searchsorted(obs, g, side="left")          # first obs at/after each grid point
    inside = (nxt > 0) & (nxt < len(obs))
    nxt_c = np.clip(nxt, 1, len(obs) - 1)
    exact = obs[np.clip(nxt, 0, len(obs) - 1)] == g
    span = obs[nxt_c] - obs[nxt_c - 1]
    bad = inside & ~exact & (span > pd.Timedelta(minutes=max_gap_min).value)
    grid_vals.iloc[np.flatnonzero(bad)] = np.nan
    return grid_vals


def to_grid(s: pd.Series, grid: pd.DatetimeIndex, method: str, max_gap_min: float) -> pd.Series:
    if method == "interp":
        out = s.reindex(s.index.union(grid)).interpolate(method="time", limit_area="inside")
        out = out.reindex(grid)
    else:  # hold
        out = s.reindex(s.index.union(grid)).ffill().reindex(grid)
    out.loc[(grid < s.index[0]) | (grid > s.index[-1] + pd.Timedelta(minutes=max_gap_min))] = np.nan
    return _blank_long_gaps(out, s.index, max_gap_min)


def build_frame(series: dict[str, pd.Series], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Align every feed onto one 1-minute grid over [start, end)."""
    grid = pd.date_range(start.ceil("min"), end.floor("min"), freq="1min", inclusive="left")
    by_col = {v[0]: v for v in FEEDS.values()}
    df = pd.DataFrame(index=grid)
    df.index.name = "ts"
    for col, s in series.items():
        _, _, method, gap = by_col[col]
        win = s[(s.index >= start - pd.Timedelta(hours=2)) & (s.index < end + pd.Timedelta(hours=2))]
        if len(win) >= 2:
            df[col] = to_grid(win, grid, method, gap).to_numpy()
    if "outdoor_f" in df.columns:
        df["outdoor_src"] = np.where(df["outdoor_f"].notna(), "logged", None)
    return df


def mask_sunlit_sensor(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Keep the raw indoor trace in indoor_raw_f and NaN indoor_f wherever sun is on the sensor."""
    df["indoor_raw_f"] = df["indoor_f"]
    if "sun_lux" not in df:
        df["sensor_sunlit"] = False
        return df
    lit = (df["sun_lux"] > cfg["sensor_sunlit_lux"]).to_numpy()
    before, after = cfg["sensor_sunlit_pad_min"]
    # pad: a centered max over a shifted window extends the mask `before` back and `after` forward
    padded = pd.Series(lit.astype(float)).rolling(before + after + 1, min_periods=1).max().shift(-before)
    df["sensor_sunlit"] = padded.fillna(0).to_numpy().astype(bool)
    df.loc[df["sensor_sunlit"], "indoor_f"] = np.nan
    n = int(df["sensor_sunlit"].sum())
    if n:
        print(f"[qc]   {n} min of indoor data masked: direct sun on the sensor "
              f"(sun_lux > {cfg['sensor_sunlit_lux']})")
    return df


# ---- Open-Meteo ----

def _om_get(url: str, timeout: float) -> pd.Series:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        h = json.loads(r.read().decode()).get("hourly") or {}
    s = pd.Series(h.get("temperature_2m"), index=pd.to_datetime(h.get("time")), dtype="float64")
    return s.dropna()


def _om_query(cfg: dict, **extra) -> str:
    q = {"latitude": cfg["latitude"], "longitude": cfg["longitude"], "hourly": "temperature_2m",
         "temperature_unit": "fahrenheit", "timezone": cfg["timezone"], **extra}
    return urllib.parse.urlencode(q)


def fetch_outdoor_hourly(start: pd.Timestamp, end: pd.Timestamp, cfg: dict, cache_path: str) -> pd.Series:
    """Hourly outdoor °F over [start, end], served from a local cache where possible.

    Archive (reanalysis) covers everything older than ~5 days; the forecast API's
    past_days covers the recent stretch the archive hasn't caught up on. Only past
    hours are cached, so forecast values never get frozen in as history.
    """
    tz = cfg["timezone"]
    cached = pd.Series(dtype="float64")
    if os.path.exists(cache_path):
        c = pd.read_csv(cache_path)
        cached = pd.Series(c["outdoor_f"].to_numpy(),
                           index=pd.to_datetime(c["ts"], utc=True).dt.tz_convert(tz))
    need = pd.date_range(start.floor("h"), end.ceil("h"), freq="1h")
    missing = need.difference(cached.index)
    if missing.empty:
        return cached
    now = pd.Timestamp.now(tz=tz)
    fetched = []
    try:
        arch_end = min(missing.max(), now - pd.Timedelta(days=2))
        if missing.min() <= arch_end:
            fetched.append(_om_get("https://archive-api.open-meteo.com/v1/archive?" + _om_query(
                cfg, start_date=str(missing.min().date()), end_date=str(arch_end.date())),
                cfg["weather_timeout_s"]))
        if missing.max() >= now - pd.Timedelta(days=7):
            past = int(min(92, max(1, (now - missing.min()).days + 1)))
            fetched.append(_om_get("https://api.open-meteo.com/v1/forecast?" + _om_query(
                cfg, past_days=past, forecast_days=1), cfg["weather_timeout_s"]))
    except Exception as exc:  # network is optional
        print(f"[wx]   Open-Meteo unavailable ({exc}); outdoor backfill limited to cache")
    for f in fetched:
        f.index = f.index.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
        f = f[f.index.notna() & (f.index <= now)]
        cached = f.combine_first(cached) if not cached.empty else f
    cached = cached[~cached.index.duplicated()].sort_index()
    if fetched:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        pd.DataFrame({"ts": cached.index.tz_convert("UTC"), "outdoor_f": cached.values}).to_csv(
            cache_path, index=False)
    return cached


def fetch_forecast_hourly(cfg: dict, hours: int = 36) -> pd.Series | None:
    tz = cfg["timezone"]
    try:
        s = _om_get("https://api.open-meteo.com/v1/forecast?" + _om_query(cfg, forecast_days=3),
                    cfg["weather_timeout_s"])
    except Exception as exc:
        print(f"[wx]   forecast unavailable ({exc})")
        return None
    s.index = s.index.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    now = pd.Timestamp.now(tz=tz).floor("h")
    return s[(s.index >= now) & (s.index <= now + pd.Timedelta(hours=hours))]


def backfill_outdoor(df: pd.DataFrame, cfg: dict, cache_path: str) -> pd.DataFrame:
    """Fill outdoor_f wherever the logged feed has no data, from Open-Meteo hourly."""
    if not cfg["weather_backfill"]:
        return df
    if "outdoor_f" not in df.columns:
        df["outdoor_f"] = np.nan
        df["outdoor_src"] = None
    holes = df["outdoor_f"].isna()
    if not holes.any():
        return df
    om = fetch_outdoor_hourly(df.index[holes][0], df.index[holes][-1], cfg, cache_path)
    if om.empty:
        return df
    filled = om.reindex(om.index.union(df.index)).interpolate(method="time", limit_area="inside")
    filled = _blank_long_gaps(filled.reindex(df.index), om.index, 120)
    use = holes & filled.notna()
    df.loc[use, "outdoor_f"] = filled[use]
    df.loc[use, "outdoor_src"] = "open-meteo"
    print(f"[wx]   outdoor: {int((df['outdoor_src'] == 'logged').sum())} min logged, "
          f"{int(use.sum())} min backfilled from Open-Meteo hourly")
    return df
