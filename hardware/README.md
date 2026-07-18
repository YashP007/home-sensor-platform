# Hardware

This directory contains the electrical design and fabrication artifacts.

## Contents

| File | Description |
|---|---|
| `schematic.pdf` | Full schematic export from EAGLE (v13). |
| `BOM.csv` | Cleaned bill of materials with source, cost, and function of every component. |
| `pcb_images/` | Top and bottom PCB renders, and photos of the assembled board. |
| `enclosure/` | 3D-printable enclosure STLs and Fusion 360 source files. |

## Fabrication

Gerber files are not committed to this repository. To fabricate:

1. Export Gerbers from the CAD source file (EAGLE `.brd` — not currently committed pending a migration to KiCad 8, which is planned for v1.1).
2. Upload to your preferred fab. This board has been verified with JLCPCB at their standard 2-layer, 1 oz copper, HASL finish specification.
3. Recommended: order 5 boards. Fab pricing is dominated by setup cost, so ordering 5 is roughly the same price as ordering 2 or 3.

Typical turnaround: 5–7 business days including shipping to the US.

## Reading the schematic

The schematic (`schematic.pdf`) is organized into functional blocks with clear boundaries:

- **Top-left:** thermistor / phototransistor analog input path.
- **Top-center:** LED matrix control (3-pin JST connector, series resistor).
- **Top-right:** I2C debug and expansion headers.
- **Middle:** XIAO ESP32-C3 module with pin assignments.
- **Bottom:** Power management (dual barrel jacks, Schottky isolation, bulk capacitor).

Net names are consistent between the schematic and layout: `VDD_5V`, `VDD_3V3`, `SDA`, `SCL`, `D_LED_MATRIX`, `V_SIGNAL`, and the RGB channels (`VRED`, `VGREEN`, `VBLUE`) reserved for a future RGB LED indicator.

## Pin assignments (XIAO ESP32-C3 → function)

| XIAO Pin | GPIO | Function |
|---|---|---|
| D0 (A0) | GPIO2 | Analog input from SENS 4-pin header (V_SIGNAL) |
| D1 (A1) | GPIO3 | Analog input from thermistor divider (V_SIGNAL) |
| D2 (A2) | GPIO4 | Reserved for future RGB LED (VRED) |
| D3 (A3) | GPIO5 | Reserved for future RGB LED (VGREEN) |
| D4 (SDA) | GPIO6 | I2C data — Qwiic + on-board |
| D5 (SCL) | GPIO7 | I2C clock — Qwiic + on-board |
| D6 (TX) | GPIO21 | Serial (debug) / Reserved for future RGB LED (VBLUE) |
| D7 (RX) | GPIO20 | Serial (debug) |
| D8 (SCK) | GPIO8 | WS2812B data line (D_LED_MATRIX) |
| D9 (MISO) | GPIO9 | Available |
| D10 (MOSI) | GPIO10 | Available |

## Version history

Current PCB revision: **v13**. Prior revisions were not fabricated — v13 is the first physical board. Earlier revisions in the design log addressed:

- v1–v4: Initial layouts with QT Py ESP32-S3, revised to XIAO ESP32-C3 for cost.
- v5–v7: Single barrel jack, then dual-input topology added after Level 4 (WS2812B) requirements were clarified.
- v8: Schematic reorganized into functional blocks. Design decisions from earlier revisions consolidated here.
- v9–v12: Layout iterations to fit within 50 × 45 mm target while maintaining ground plane integrity.
- v13: Current release. First physical fabrication.
