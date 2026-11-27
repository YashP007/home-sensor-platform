#!/usr/bin/env python3
"""
analyze.py - multi-feed analysis (indoor temp + outdoor temp + light, humidity/pressure if exported).

Covers the period from the first light-feed sample onward (override with --since or
"multi_feed_start" in config.json). Earlier, temperature-only data is handled by
analyze_legacy.py so the two eras are reported consistently but separately.

Usage:
    python analyze.py                       # everything in raw-data/ from the multi-feed start
    python analyze.py --forecast            # + tonight's pre-heat advice from the Open-Meteo forecast
    python analyze.py --day 2026-10-21      # + a single-day dashboard (repeatable)
    python analyze.py --since 2026-10-01    # include earlier temperature-only days too

Outputs (analysis_output/current/): summary.md, summary.json, data/*.csv,
plots/*.png, plots/weekly/week_<YYYY-Www>.png, explorer.html
"""

import argparse
import os
import sys

from homesense.config import load_config
from homesense.pipeline import run

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", help="folder of Adafruit IO exports (default: raw-data/)")
    ap.add_argument("--out", help="output folder (default: analysis_output/current/)")
    ap.add_argument("--since", help="start date YYYY-MM-DD (default: first light-feed sample)")
    ap.add_argument("--day", action="append", metavar="YYYY-MM-DD", help="also draw a one-day dashboard")
    ap.add_argument("--forecast", action="store_true", help="predict tonight's coast from the Open-Meteo forecast")
    ap.add_argument("--no-weather", action="store_true", help="no Open-Meteo backfill for missing outdoor data")
    ap.add_argument("--no-html", action="store_true", help="skip the plotly explorer.html")
    args = ap.parse_args(argv)
    args.until = None
    return run("current", args, load_config(SCRIPT_DIR), SCRIPT_DIR)


if __name__ == "__main__":
    sys.exit(main())
