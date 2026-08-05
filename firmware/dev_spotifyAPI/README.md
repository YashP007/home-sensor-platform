# dev_spotifyAPI — Spotify Web API harness

Desktop scaffolding for the [Level 4](../level4_future_spotify_display/) album-art display. The goal is to work out the OAuth 2.0 handshake and the currently-playing poll loop somewhere you have a debugger, a filesystem, and legible error messages — then port the working request/response shapes to the ESP32-C3.

Per the Level 4 estimate, "Spotify developer account setup, OAuth comprehension" is a 4–6 hour phase. This directory is that phase.

Nothing here is compiled or flashed. Python 3.8+ standard library only, no `pip install`.

## Files

| File | Purpose |
|---|---|
| `spotify_test.py` | The harness. Four subcommands: `auth`, `whoami`, `now`, `watch`. |
| `secrets.env.example` | Config template. Copy to `secrets.env`. |
| `.gitignore` | Excludes `secrets.env`, `tokens.json`, and `art/`. |

`secrets.env` and `tokens.json` are gitignored for the same reason `secrets.h` is — a refresh token is a durable credential granting read access to your listening activity, and bots scrape public GitHub for exactly this.

---

## One-time setup

### 1. Register the app

Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and click **Create app**. Any Spotify account works; you do not need a paid developer plan.

| Field | Value |
|---|---|
| App name | `SmartHome Monitor Dev` (arbitrary) |
| Redirect URI | `http://127.0.0.1:8888/callback` |
| Which API/SDKs | Check **Web API** only |

**The redirect URI is where this goes wrong.** Spotify's [27 November 2025 OAuth migration](https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025) removed support for the implicit grant flow, plain `http://` redirect URIs, and — the one that bites — the hostname `localhost`. Every tutorial written before 2025, including the SparkFun one referenced in the Level 4 README, tells you to use `http://localhost:8888/callback`. That now fails.

The current rules:

- HTTPS is required, **except** for a loopback address, where HTTP is permitted.
- A loopback redirect must use the IP *literal*: `http://127.0.0.1:PORT` or `http://[::1]:PORT`.
- `localhost` is rejected outright.

The match is byte-for-byte. `http://127.0.0.1:8888/callback` and `http://127.0.0.1:8888/callback/` are different URIs. A mismatch surfaces as `INVALID_CLIENT: Invalid redirect URI`, which does not say anything about the redirect URI being the problem, so it is worth getting right the first time.

### 2. Copy the Client ID

Open **Settings** in your new app. Copy the **Client ID**.

Ignore the Client Secret. This harness uses Authorization Code with **PKCE**, which does not need one — see [Why PKCE](#why-pkce-and-not-the-client-secret-flow) below.

```
cd firmware/dev_spotifyAPI
cp secrets.env.example secrets.env
```

Paste the Client ID into `secrets.env`.

### 3. Confirm your account can use the player endpoints

`whoami` reports `product tier : not disclosed`. That is expected, not a problem — the `product` field is only populated when the token carries the `user-read-private` scope, which this app deliberately does not request. An album-art display has no use for your subscription tier, and every extra scope is one more line on the consent screen.

The authoritative test of player access is to call a player endpoint: run `now` with music playing. If a track comes back, you have access.

Reading and *controlling* playback differ here. `GET /me/player/currently-playing` works on free accounts. The write endpoints — play, pause, skip, seek — require Premium. Level 4 only reads, so this is not a constraint for this project.

Separately, a new app starts in **Development Mode**, which limits it to accounts you explicitly allow-list. Your own account is added automatically. If you later test against a second account, add it under **Settings → User Management**, or calls fail with 403.

---

## Running it

### `auth` — the one-time handshake

```
python spotify_test.py auth
```

Opens your browser to Spotify's consent screen, catches the redirect on a temporary local HTTP server, and exchanges the authorization code for tokens. Writes `tokens.json`.

You should only need this once. The refresh token it returns does not expire.

### `whoami` — smallest possible authenticated call

```
python spotify_test.py whoami
```

`GET /v1/me` needs no scopes, so if this fails the problem is the token itself, not your permissions. Run it before debugging anything else.

### `now` — one currently-playing fetch

```
python spotify_test.py now
python spotify_test.py now --art-size 300      # grab a larger variant instead
python spotify_test.py now --no-save-art       # metadata only
python spotify_test.py now --verbose
```

Start music on any device, then run it. Artwork is saved to `art/` by default; `--art-size` picks the variant nearest that width, defaulting to the 64 px one the ESP32 wants.

`--verbose` prints the raw HTTP request line, headers, and body — this is the output to keep open when writing the firmware version.

### `watch` — the firmware loop, in Python

```
python spotify_test.py watch --interval 5
```

Polls continuously and only acts when the track actually changes. This is the structure the ESP32 loop will have, and the change-gating matters there: polling costs ~2 KB of JSON, but downloading and decoding a JPEG costs ~8 KB of buffer plus roughly 100 ms of blocked single-core time on the C3.

---

## What the flow actually does

```
  1. Generate  code_verifier  = 64 random bytes, base64url
               code_challenge = base64url(SHA256(code_verifier))

  2. Browser -> accounts.spotify.com/authorize
               ?client_id&redirect_uri&scope&state
               &code_challenge_method=S256&code_challenge

  3. User approves. Spotify redirects:
               http://127.0.0.1:8888/callback?code=AQD...&state=...
               (the local server in the script catches this)

  4. POST accounts.spotify.com/api/token
               grant_type=authorization_code
               &code&redirect_uri&client_id&code_verifier   <- verifier, not challenge

  5. Response: access_token (1 hour), refresh_token (permanent), scope

  6. GET api.spotify.com/v1/me/player/currently-playing
               Authorization: Bearer <access_token>

  7. On expiry: POST /api/token with grant_type=refresh_token
```

### Why PKCE and not the client-secret flow

The classic Authorization Code flow authenticates step 4 with a client secret. That works for a server you control. It does not work for firmware, because anyone with physical access can dump the flash over UART and read the secret out of it — and a leaked Spotify client secret lets an attacker impersonate your app against any user who authorized it.

PKCE replaces the secret with a per-transaction proof. The script invents a random `code_verifier`, sends only its SHA-256 hash through the browser (where a malicious app on the same machine could observe the redirect), and reveals the verifier only in the direct back-channel call to the token endpoint. An attacker who intercepts the authorization code cannot redeem it without the verifier, and cannot derive the verifier from the hash.

Nothing in this design needs to stay secret at rest, which is what makes it the right choice for the ESP32.

### Why `state` is checked

`state` is a random value sent in step 2 and echoed back in step 3. The script aborts if it does not match. This blocks CSRF: without it, an attacker can trick your client into completing a flow that binds *their* Spotify account to your device. It costs four lines. Keep it in the firmware port.

---

## Things that surprised me, worth carrying into firmware

**204 is not an error.** When nothing is playing, Spotify returns `HTTP 204 No Content` with a completely empty body — not `{}`, not `null`. Code that calls the JSON parser unconditionally crashes here, and only when you pause, which makes it look intermittent. `cmd_now()` checks status before parsing.

**Refresh tokens rotate.** On the PKCE flow, a refresh response *may* include a new `refresh_token`, and when it does, the old one is immediately dead. Firmware that writes the refresh token to NVS once at setup and never updates it will work for days and then fail with `invalid_grant` at an unpredictable moment. `_store_token_response()` handles this; the ESP32 version must write back to `Preferences` on every refresh that returns a new value.

**`item` can be null while a session is active.** During an ad break, or in a private session, you get a 200 with `item: null`. Guard it.

**Podcasts have a different shape.** Episodes carry artwork at `item.images`, tracks at `item.album.images`. If you request `additional_types=track,episode` you must handle both; if you omit it, episodes arrive as `item: null` and you lose the distinction between "podcast playing" and "nothing playing."

**Ask for the 64 px image.** Spotify returns three sizes, typically 640/300/64. The 640 px JPEG will not decode inside the C3's SRAM budget alongside the TLS working buffers. `pick_image()` defaults to 64.

**Refresh early, not on failure.** The script refreshes 60 seconds before nominal expiry rather than waiting for a 401. On a device with drifting time this margin matters, and it turns a two-request failure path into a one-request happy path. The 401 handler stays as a backstop.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `INVALID_CLIENT: Invalid redirect URI` | The URI in `secrets.env` does not byte-for-byte match the dashboard. Check the trailing slash and the port. |
| Consent page loads, callback never arrives | Something else is bound to port 8888. Change it in *both* `secrets.env` and the dashboard. |
| `403 Forbidden` on `/me/player/*` | The account is not allow-listed on a Development Mode app, or you called a playback *control* endpoint (play/pause/skip) on a free account. Reading currently-playing is fine on free. |
| `whoami` says `product tier : not disclosed` | Expected. Requires the `user-read-private` scope, which this app does not request. Not a sign of anything wrong. |
| `now` returns nothing while music plays | Playback is on a device Spotify does not consider active, or you are in a private session. Play from the desktop or phone app and retry. |
| `invalid_grant` on refresh | The refresh token was rotated out or revoked. Delete `tokens.json` and re-run `auth`. |
| `429` | Rate limited. The script honors `Retry-After` automatically. Polling at 5–10 s intervals stays comfortably inside the limits. |

---

## Porting to the ESP32

Run `--verbose` and copy the request shapes. The mapping:

| This script | ESP32 equivalent |
|---|---|
| `http_request()` over `urllib` | `WiFiClientSecure` + `HTTPClient` |
| `json.loads()` | `ArduinoJson` with a sized `StaticJsonDocument` |
| `tokens.json` | `Preferences` (NVS) |
| Local callback server | `WebServer` on the ESP32, *or* run `auth` here once and flash the refresh token — see below |
| Trusted CA bundle | `WiFiClientSecure::setCACert()`, or `setInsecure()` for bring-up only |

One shortcut worth considering: the Level 4 README notes that running a callback web server and an outbound TLS client simultaneously is awkward on the C3. You can sidestep that entirely for v1 by running `auth` on the desktop once, then flashing the resulting refresh token into NVS. The device then only ever needs the outbound `grant_type=refresh_token` call — no inbound server, no browser, and a meaningfully smaller memory footprint. Re-authorization becomes a manual step, but it is a step you take approximately never.

## References

- [Authorization Code with PKCE Flow](https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow)
- [Redirect URI requirements](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)
- [OAuth migration — 27 November 2025](https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025)
- [Get Currently Playing Track](https://developer.spotify.com/documentation/web-api/reference/get-the-users-currently-playing-track)
- [Scopes](https://developer.spotify.com/documentation/web-api/concepts/scopes)
- [Rate limits](https://developer.spotify.com/documentation/web-api/concepts/rate-limits)
- [RFC 7636 — PKCE](https://datatracker.ietf.org/doc/html/rfc7636)
