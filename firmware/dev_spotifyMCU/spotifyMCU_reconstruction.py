#!/usr/bin/env python3
"""
spotifyMCU_reconstruction.py

Companion to spotifyMCU_code.ino. Listens on the ESP32's serial port, finds
the base64-framed image the sketch prints, decodes it, and writes it out as
a real file - so you can actually open it and confirm the round trip
(WiFi -> TLS -> HTTP GET -> Serial) produced an intact image, not just "some
bytes showed up."

The sketch wraps each image like this:

    <<<IMG_BEGIN len=4213 type=image/jpeg>>>
    <base64, wrapped at 76 chars/line>
    ...
    <<<IMG_END>>>

Everything else coming over the port (the [WIFI]/[HTTP]/[IMG] status lines)
is just echoed to the terminal prefixed with [mcu], so you get the device's
own logging and the capture confirmations in one place.

Usage:
    python spotifyMCU_reconstruction.py --port COM5
    python spotifyMCU_reconstruction.py --port /dev/ttyUSB0 --baud 115200
    python spotifyMCU_reconstruction.py --port COM5 --outdir captures

Requires pyserial:
    pip install pyserial
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import time
from datetime import datetime
from pathlib import Path
import serial # pip install pyserial

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTDIR = SCRIPT_DIR / "captures"

BEGIN_RE = re.compile(r"^<<<IMG_BEGIN len=(\d+) type=(\S+)>>>$")
END_MARK = "<<<IMG_END>>>"

# Best-effort content-type -> extension. Falls back to .bin for anything we
# don't recognize, which is still useful - you can rename it and inspect the
# bytes by hand to see what actually came back.
EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def guess_extension(content_type: str) -> str:
    return EXT_BY_TYPE.get(content_type.strip().lower(), ".bin")


def looks_like_jpeg(data: bytes) -> bool:
    """SOI/EOI marker check - cheap sanity check that this isn't a truncated frame."""
    return data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"


def save_capture(b64_lines: list[str], declared_len: int, content_type: str, outdir: Path) -> None:
    raw_b64 = "".join(b64_lines)
    try:
        data = base64.b64decode(raw_b64, validate=True)
    except Exception as exc:
        log("CAPTURE", f"ERROR: base64 decode failed ({exc}). Dropping this frame.")
        return

    if len(data) != declared_len:
        log("CAPTURE", f"WARNING: decoded {len(data)} bytes, sketch declared {declared_len}. "
                       "Serial line may have been dropped or corrupted - saving anyway.")

    outdir.mkdir(parents=True, exist_ok=True)
    ext = guess_extension(content_type)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = outdir / f"capture_{stamp}{ext}"
    # Two captures inside the same second would otherwise clobber each other -
    # cheap to avoid, so avoid it.
    n = 2
    while out_path.exists():
        out_path = outdir / f"capture_{stamp}_{n}{ext}"
        n += 1
    out_path.write_bytes(data)

    note = ""
    if ext == ".jpg":
        note = " (looks like a valid JPEG)" if looks_like_jpeg(data) else \
               " (WARNING: missing JPEG SOI/EOI markers - likely truncated)"

    log("CAPTURE", f"saved {len(data)} bytes -> {out_path.relative_to(SCRIPT_DIR)}{note}")


def run(port: str, baud: int, outdir: Path, timeout_s: float) -> None:
    log("SERIAL", f"opening {port} @ {baud} baud (Ctrl+C to stop)")
    ser = serial.Serial(port, baud, timeout=timeout_s)
    # ESP32 resets on DTR toggle when the port opens; give it a moment to
    # boot and start printing before we start expecting sensible lines.
    time.sleep(2.0)
    ser.reset_input_buffer()

    capturing = False
    declared_len = 0
    content_type = ""
    buf: list[str] = []
    n_captured = 0

    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue   # timeout with nothing to read; loop and try again
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:
                continue
            if not line:
                continue

            if not capturing:
                m = BEGIN_RE.match(line)
                if m:
                    declared_len = int(m.group(1))
                    content_type = m.group(2)
                    buf = []
                    capturing = True
                    log("SERIAL", f"capturing image: declared {declared_len} bytes, "
                                  f"type={content_type}")
                else:
                    print(f"[mcu] {line}")
                continue

            # We're mid-capture: every line is either more base64 or the end marker.
            if line == END_MARK:
                capturing = False
                save_capture(buf, declared_len, content_type, outdir)
                n_captured += 1
                continue
            buf.append(line)

    except KeyboardInterrupt:
        print()
        log("SERIAL", f"stopped. {n_captured} image(s) captured this session.")
    finally:
        ser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True,
                    help="serial port, e.g. COM5 (Windows) or /dev/ttyUSB0 (Linux/Mac)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR,
                    help=f"where captured images go (default: {DEFAULT_OUTDIR.name}/)")
    ap.add_argument("--timeout", type=float, default=1.0,
                    help="serial read timeout in seconds (default: 1.0)")
    args = ap.parse_args()

    try:
        run(args.port, args.baud, args.outdir, args.timeout)
    except serial.SerialException as exc:
        print(f"[FATAL] Could not open {args.port}: {exc}", file=sys.stderr)
        print("        Check the port name (Arduino IDE Tools > Port, or Device "
              "Manager on Windows) and that nothing else has it open (like the "
              "Arduino Serial Monitor).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
