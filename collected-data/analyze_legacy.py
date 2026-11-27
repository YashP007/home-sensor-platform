#!/usr/bin/env python3
"""
analyze_legacy.py - the temperature-only history (before the light/outdoor feeds existed).

Weekly temperature dashboards with Open-Meteo outdoor temperature as a reference,
a temperature calendar, daily trends and the matching CSVs - in the same style as
analyze.py so the two eras read the same way.

Usage:
    python analyze_legacy.py                     # everything before the first light-feed sample
    python analyze_legacy.py --until 2026-09-01
    python analyze_legacy.py --day 2026-07-28

Outputs (analysis_output/legacy/): summary.md, summary.json, data/*.csv,
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
    ap.add_argument("--out", help="output folder (default: analysis_output/legacy/)")
    ap.add_argument("--until", help="end date YYYY-MM-DD (default: first light-feed sample)")
    ap.add_argument("--day", action="append", metavar="YYYY-MM-DD", help="also draw a one-day dashboard")
    ap.add_argument("--no-weather", action="store_true", help="no Open-Meteo outdoor reference")
    ap.add_argument("--no-html", action="store_true", help="skip the plotly explorer.html")
    args = ap.parse_args(argv)
    args.since, args.forecast = None, False
    return run("legacy", args, load_config(SCRIPT_DIR), SCRIPT_DIR)


if __name__ == "__main__":
    sys.exit(main())
