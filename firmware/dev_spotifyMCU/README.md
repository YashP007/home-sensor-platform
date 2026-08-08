# dev_spotifyMCU

Gets the XIAO ESP32-C3 actually talking to Spotify - refreshing a token,
polling what's playing, pulling the album art over HTTPS - without any of
the JPEG decode or LED matrix work yet. That's still ahead of us; see
[Level 4](../level4_future_spotify_display/) for the full plan.

Started as just an HTTPS image fetch test (proving TLS + GET even worked on
this chip before dragging OAuth into it). That test mode is still in there -
flip `USE_SPOTIFY` to 0 in the sketch and it goes back to hammering a static
test URL, which is still the fastest way to tell a network problem apart
from a Spotify problem.

## How it works

```
  ESP32-C3                                      your computer
  --------                                      -------------
  connect WiFi
  POST accounts.spotify.com/api/token   ------->
                                          <-------  access token
  GET .../currently-playing               ------->
                                          <-------  track info + art url
  GET the art url                         ------->
                                          <-------  image bytes
  base64-encode, print over serial        ------->  spotifyMCU_reconstruction.py
                                                      decodes it, saves a real file
```

Album art goes out as base64 over serial rather than raw bytes, on purpose -
a dropped byte in a raw binary stream wrecks the whole frame with no way to
tell what happened. Text either shows up intact or the corruption is obvious.

## Setup

The sketch lives in `spotifyMCU_code/spotifyMCU_code.ino` - Arduino wants
the folder name to match the `.ino` name, so that's where it has to sit even
though this README is one level up.

1. Copy `spotifyMCU_code/secrets_example.h` to `spotifyMCU_code/secrets.h`
   (has to be next to the `.ino`, not up here).
2. Fill in WiFi.
3. Get a Spotify client ID: developer.spotify.com/dashboard -> your app ->
   Settings. No client secret needed - same PKCE public-client setup as the
   desktop script.
4. Get a refresh token by running the desktop auth flow once:
   ```
   cd ../dev_spotifyAPI
   python spotify_test.py auth
   ```
   then copying `refresh_token` out of the `tokens.json` it writes. Paste
   both into `secrets.h`. The device never opens a browser or does the
   authorize-page dance itself - it only ever exchanges this refresh token
   for a fresh access token, which is why there's no redirect URI to worry
   about here.
5. Arduino IDE: File > Open, `spotifyMCU_code/spotifyMCU_code.ino`.
   Board: `XIAO_ESP32C3`. Install `ArduinoJson` via Library Manager if you
   haven't already - everything else (`WiFiClientSecure`, `HTTPClient`,
   `Preferences`, `mbedtls`'s base64) ships with the board package. Upload.

## Running it

```
pip install pyserial
python spotifyMCU_reconstruction.py --port COM5
```

(Port varies - Arduino IDE's Tools > Port, or Device Manager on Windows.)
Leave it running while the sketch executes. Play something on Spotify and
you should see, within `FETCH_INTERVAL_MS` (15s by default):

```
[mcu] [TOKEN] refreshing
[mcu] [TOKEN] good for 3600s
[mcu] [SPOTIFY] now playing: Caroline Polachek - Bunny Is A Rider
[mcu] [HTTP] GET https://i.scdn.co/image/...
[mcu] [HTTP] 200 OK, type=image/jpeg, length=4213
[SERIAL] capturing image: declared 4213 bytes, type=image/jpeg
[CAPTURE] saved 4213 bytes -> captures/capture_20260807-223857.jpg (looks like a valid JPEG)
```

Skip a track and it should fetch new art. Let the same track keep playing
and it shouldn't - `lastTrackLabel` gates the download so it only happens on
an actual change, same idea as the `watch` command in `dev_spotifyAPI`.

Open the saved file. If it renders, the whole chain worked - auth, the
currently-playing poll, the art fetch, base64 over serial, and decode back
on the desktop side.

## What this deliberately does NOT do yet

- No JPEG decode on-device. The image is proven readable on your computer,
  not on the ESP32 - that's a separate piece of work.
- No LED matrix output.
- `client.setInsecure()` skips certificate validation on every HTTPS call.
  Fine for proving the path works, but it needs a real root CA bundle
  before this goes anywhere near a device that isn't sitting on your desk.
  See the Level 4 README for what that involves.
- The refresh token lives in `secrets.h` at flash time and gets copied into
  NVS (via `Preferences`) on first boot. If Spotify rotates it during a
  refresh, the new one gets saved back to NVS automatically - but if you
  ever reflash with a stale `secrets.h`, the old value in NVS still wins,
  since it's only seeded from `secrets.h` when NVS is empty. Worth knowing
  if refresh ever starts failing after you've changed `secrets.h` and
  reflashed: erase flash first, or clear the `spotify` NVS namespace.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `[TOKEN] refresh failed, HTTP 400` | Refresh token expired, got revoked, or belongs to a different client ID. Re-run `spotify_test.py auth` on the desktop and paste in the new one. |
| `[TOKEN] refresh failed` immediately after changing `secrets.h` | NVS still has the old refresh token cached from a previous flash - see the note above. Erase flash and re-upload. |
| `[SPOTIFY] nothing playing right now` | Not an error - Spotify's 204 response for "nothing active." Start playback and it'll pick it up next cycle. |
| `[SPOTIFY] currently-playing returned HTTP 403` | Token lacks the right scope, or your Spotify app is in Development Mode and this account isn't on the allow-list (dashboard -> your app -> User Management). |
| `[HTTP] request failed` (image fetch) | TLS handshake failure or DNS issue. Confirm WiFi is actually connected first. |
| `[HTTP] image is N bytes, over the cap` | `MAX_IMAGE_BYTES` is 32KB by default; something's requesting a bigger art size than expected. |
| Reconstruction script: `base64 decode failed` | A serial line got corrupted or dropped - usually a bad cable/port or baud mismatch. Confirm both sides use 115200. |
| Reconstruction script: saved file isn't a valid JPEG | `[CAPTURE]` flags this directly (missing SOI/EOI markers) - usually a stalled or truncated download. Check for `[HTTP] stalled mid-download` on the device side. |
| `Could not open COMx` | Something else has the port, usually the Arduino Serial Monitor. Only one program can hold it at a time. |

## Better ways to develop and test this

A few things worth doing as this grows past "stepping stone":

**Get a real compiler check without the IDE.** [`arduino-cli`](https://arduino.github.io/arduino-cli/) compiles from the command line: `arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C3 .` catches syntax errors in seconds instead of round-tripping through the IDE, and it's scriptable into a GitHub Actions job that compiles on every push with no hardware needed.

**Keep leaning on the desktop-first split.** `dev_spotifyAPI` is still the place to work out anything that isn't inherently hardware - the JSON shape, error handling, retry logic. It's a lot faster to iterate there with a real debugger than on-device with a 115200-baud println. This sketch's `fetchCurrentlyPlaying()` and `ensureAccessToken()` are close ports of what the Python script already proved out.

**Watch the heap, don't guess at it.** `ESP.getFreeHeap()` printed at boot and after each fetch would show directly whether TLS plus the 32KB image buffer plus the ~40KB base64 buffer are actually leaving headroom, instead of inferring it from the datasheet. Worth adding before a JPEG decoder gets stacked on top of all this.

**A persistent log, not just a live view.** `spotifyMCU_reconstruction.py` could tee everything to a timestamped `.log` file next to the captured images, so an overnight run that hits something flaky leaves a trail instead of scrolled-off terminal output.

**Unit-testable pieces, even in C++.** Things like the base64 chunking or the "closest to 64px" image-picking logic could be pulled out and tested against fixed inputs with something like [Unity](http://www.throwtheswitch.org/unity) or [AUnit](https://github.com/bxparks/AUnit), off-device and fast. Probably not worth it yet at this size, but worth knowing about once the sketch keeps growing.
