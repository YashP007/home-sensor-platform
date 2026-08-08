# dev_spotifyMCU — HTTPS image fetch test

A stepping stone toward [Level 4](../level4_future_spotify_display/), not the
real pipeline. Before writing OAuth-on-a-microcontroller and a JPEG decoder,
this proves the one thing everything else depends on: that the XIAO
ESP32-C3 can open a TLS connection, `GET` a binary image over HTTPS, and
hand the bytes back intact.

It isolates the networking question from everything else, so if something
breaks later during real Level 4 development, you already know the HTTP/TLS
path works and the bug is somewhere else.

## How it works

```
  ESP32-C3                                  your computer
  ────────                                  ─────────────
  connect WiFi
  HTTPS GET test image  ──────────────────>
                         <──────────────────  image bytes
  base64-encode
  print over Serial,     ──────────────────>  spotifyMCU_reconstruction.py
  framed with markers                         decodes it, saves a real file
```

Base64 over Serial, not raw bytes — a dropped byte in a raw binary stream
produces an unrecoverable frame with no good way to tell what went wrong.
Text either arrives intact or the corruption is obvious immediately.

## Setup

The sketch lives in `spotifyMCU_code/spotifyMCU_code.ino` — Arduino requires
the containing folder name to match the `.ino` filename, so that's where it
has to sit even though this README is one level up.

1. Copy `spotifyMCU_code/secrets_example.h` to `spotifyMCU_code/secrets.h`
   (must be next to the `.ino`, not in this top-level directory).
2. Fill in your WiFi credentials.
3. Grab a real, current album-art URL to test against:
   ```
   cd ../dev_spotifyAPI
   python spotify_test.py now --verbose --art-size 64
   ```
   Copy the 64px image's `"url"` field out of the printed JSON into
   `TEST_IMAGE_URL` in `secrets.h`. (Any small HTTPS JPEG works if you just
   want to prove connectivity before Spotify's involved at all.)
4. Arduino IDE: File > Open, select `spotifyMCU_code/spotifyMCU_code.ino`.
   Board: `XIAO_ESP32C3`. Upload.
   No extra libraries needed beyond the ESP32 board package itself —
   `WiFiClientSecure`, `HTTPClient`, and `mbedtls/base64.h` all ship with it.

## Running it

```
pip install pyserial
python spotifyMCU_reconstruction.py --port COM5
```

(Port varies — check Arduino IDE's Tools > Port, or Device Manager on
Windows.) Leave it running while the sketch is uploaded and executing. Every
`FETCH_INTERVAL_MS` (15s by default) you should see:

```
[mcu] [HTTP] GET https://i.scdn.co/image/...
[mcu] [HTTP] 200 OK, Content-Type=image/jpeg, Content-Length=4213
[SERIAL] capturing image: declared 4213 bytes, type=image/jpeg
[CAPTURE] saved 4213 bytes -> captures/capture_20260807-223857.jpg (looks like a valid JPEG)
```

Open the saved file. If it renders as a real image, the round trip works —
WiFi, TLS, HTTP GET, streaming read, base64 encode, serial transport, and
decode are all confirmed good.

## What this deliberately does NOT do yet

- No OAuth. `TEST_IMAGE_URL` is a static, manually-pasted URL.
- No JPEG decode on-device. The image is proven readable on your computer,
  not on the ESP32.
- No LED matrix output.
- `client.setInsecure()` skips certificate validation. Fine for proving the
  path works; must be replaced with a real root CA bundle before this goes
  anywhere near production (see the Level 4 README for why).

Each of those is a separate, addressable problem once this foundation is
confirmed solid.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `[HTTP] ERROR: request failed` | TLS handshake failure, DNS issue, or `setInsecure()` somehow removed. Check WiFi is actually connected first. |
| `[HTTP] ERROR: server returned HTTP 404` | The pasted `TEST_IMAGE_URL` expired or was mistyped — Spotify's CDN URLs are not permanent, grab a fresh one. |
| `[HTTP] ERROR: image is N bytes, over the cap` | Raised `--art-size` too high, or pointed at a non-64px image. Use `--art-size 64` when grabbing the test URL. |
| Reconstruction script: `base64 decode failed` | A serial line got corrupted or dropped. Usually a bad USB cable/port or a baud mismatch — confirm both sides use 115200. |
| Reconstruction script: file saved but doesn't open / not a valid JPEG | `[CAPTURE]` will say so explicitly (missing SOI/EOI markers) — likely a stalled or truncated download. Check `[HTTP] ERROR: stalled mid-download` on the device side. |
| `Could not open COMx` | Something else has the port — usually the Arduino Serial Monitor. Close it before running the Python script (or vice versa; only one can hold the port at a time). |

## Better ways to develop and test this

A few things worth doing as this grows past "stepping stone":

**Get a real compiler check without the IDE.** [`arduino-cli`](https://arduino.github.io/arduino-cli/) compiles from the command line — `arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C3 .` catches syntax errors in seconds instead of round-tripping through the IDE. Worth setting up once, especially if you want a CI check on every push (a GitHub Actions job that just compiles, no hardware needed, catches a broken build before you're standing next to the device).

**Desktop-first, like `dev_spotifyAPI`.** The Python harness in the sibling directory is the pattern to keep leaning on: anything that isn't inherently hardware (URL construction, JSON shape, error handling logic) is faster to get right in Python with a debugger than on-device with a 115200-baud println. This sketch already follows that split — HTTP/TLS is the only thing actually tested on hardware here.

**Unit-testable pieces, even in C++.** Things like `readImageBody()`'s stall-timeout logic or the base64 chunking could be pulled into plain functions and tested against a fake `Stream` with something like [Unity](http://www.throwtheswitch.org/unity) or [AUnit](https://github.com/bxparks/AUnit) — off-device, fast, no upload cycle. Probably overkill at this size, but worth knowing about once the sketch grows past a few hundred lines.

**A serial log capture, not just a live view.** `spotifyMCU_reconstruction.py` could `tee` everything to a timestamped `.log` file alongside the captured images, so a flaky run overnight leaves a trail instead of just scrolled-off terminal output. Small addition if you find yourself debugging something intermittent.

**Memory margin, watched not assumed.** `ESP.getFreeHeap()` printed once at boot and once after each fetch would tell you directly whether TLS + the 32KB image buffer + 40KB base64 buffer are actually leaving enough headroom, instead of inferring it from the datasheet. Cheap to add, and worth having before this sketch grows a JPEG decoder on top.
