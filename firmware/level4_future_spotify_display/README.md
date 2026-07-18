# Level 4 — Spotify Album-Art Display (Future Work)

**Status: not implemented.** This directory contains the design plan for a firmware tier that will reuse the SmartHome Monitor PCB to drive a WS2812B 22×22 LED matrix displaying the album art of the currently-playing Spotify track.

## The concept

The base PCB has a 3-pin JST-SM connector (J5) and a 330Ω data-line series resistor already routed to a XIAO GPIO. Wiring a BTF-Lighting 22×22 WS2812B matrix to this connector, powered from the second barrel jack (J2), is a plug-and-play hardware operation. The interesting work is entirely in firmware.

## What the firmware needs to do

1. Complete OAuth 2.0 Authorization Code flow against Spotify's Web API to obtain a refresh token.
2. Persist the refresh token to the ESP32's non-volatile storage (Preferences library) so re-authorization is required only once per device.
3. Every ~10 seconds, poll `GET /v1/me/player/currently-playing` to retrieve the active track.
4. When the track changes, fetch the album-art URL from the response, download the smallest available image (typically 64×64 JPEG), and decode it.
5. Downsample the 64×64 image to 22×22 using a box filter, and write the resulting framebuffer to the WS2812B matrix via FastLED.
6. When the track has not changed, skip the image download and refresh path to stay well under the API rate limits.

## Why this is a substantial project

Every step above is straightforward on its own; the friction is in the composition.

**OAuth 2.0 on a microcontroller** requires implementing a web server on the ESP32 to receive the authorization code callback, plus a background HTTPS client to exchange the code for tokens. Spotify's OAuth implementation is standards-compliant but the ESP32-C3's WiFi stack does not offer great abstractions for the callback-server plus outbound-TLS pattern simultaneously. Existing libraries like `spotify-api-arduino` handle much of this, but were written for ESP32 (dual-core) and may need adaptation for the C3.

**TLS to `api.spotify.com`** requires the ESP32 to accept the Spotify certificate chain. The standard approach is to either bundle a set of trusted root CAs into the firmware (~2 KB flash) or use `WiFiClientSecure::setInsecure()` during development, which is convenient but should not ship to production.

**JPEG decoding on the ESP32-C3** with a 400 KB SRAM budget requires a streaming decoder like `TJpg_Decoder`. The 64×64 decoded framebuffer at 16-bit color is 8 KB — feasible but not luxurious once TLS working memory (~30 KB), WiFi stack (~50 KB), and the OAuth callback server (~10 KB) are accounted for. The C3's single core also means the JPEG decode blocks all other activity for the ~100 ms decode duration.

**LED matrix output** via FastLED is well-understood and low-effort compared to the above. This is not where the difficulty lies.

## Why the C3 may not be sufficient

The Spotify project is the single strongest argument for a v1.1 board revision to the XIAO ESP32-S3. The S3 has dual cores (WiFi/TLS on core 0, application on core 1), 512 KB SRAM plus 2 MB PSRAM in the WROOM variant, and hardware JPEG decode acceleration. All three of these substantially reduce the risk that the Spotify tier is infeasible on the current hardware.

The plan is:

1. Attempt Level 4 on the current C3-based board. If it works reliably, ship it as-is.
2. If reliability is poor (WiFi disconnections during JPEG decode, TLS handshake failures under load, framebuffer glitches during network activity), design a v1.1 board with the pin-compatible XIAO ESP32-S3 module and migrate firmware.

Deferring the S3 migration until after empirical testing avoids over-engineering — the C3 may be adequate.

## Rough time estimate

Based on similar projects documented elsewhere and my own experience level:

| Phase | Estimate |
|---|---|
| Spotify developer account setup, OAuth comprehension | 4–6 hours |
| Level 1 Spotify — current song title to serial monitor | 8–12 hours |
| Level 2 Spotify — album art fetched and decoded to serial output | 6–8 hours |
| Level 3 Spotify — matrix display integration | 4–6 hours |
| Testing, calibration, edge cases | 8–12 hours |
| **Total** | **30–44 hours** |

This is why the tier is called "future work" — it is a project of comparable scope to the entire Level 1–3 sensor tiers combined.

## References

- [Spotify Web API — Get Currently Playing](https://developer.spotify.com/documentation/web-api/reference/get-the-users-currently-playing-track)
- [Spotify Web API — Authorization Code Flow](https://developer.spotify.com/documentation/web-api/tutorials/code-flow)
- [SparkFun Live Spotify Album Art Display tutorial](https://learn.sparkfun.com/tutorials/live-spotify-album-art-display) (retired, but the OAuth and JPEG portions are reusable)
- [Brian Lough's spotify-api-arduino library](https://github.com/witnessmenow/spotify-api-arduino)
- [Bodmer's TJpg_Decoder](https://github.com/Bodmer/TJpg_Decoder)
