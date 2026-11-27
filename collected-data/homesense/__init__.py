"""homesense - analysis package for the SmartHome Monitor Adafruit IO exports.

Modules:
    config    defaults + optional config.json overrides
    io        feed discovery, cleaning, 1-minute alignment, Open-Meteo backfill
    activity  sun position, lights-out / wake detection from the lux feeds
    thermal   passive drift model (K_COUPLE / K_SOLAR / K_BIAS), overnight coast
    metrics   daily / weekly tables, data health, auto-flags
    plots     static PNG dashboards + plotly explorer
    report    CSV + summary.md + summary.json writers
    pipeline  wires it together for analyze.py / analyze_legacy.py
"""
