#!/usr/bin/env python3
"""
spotify_test.py - Desktop test harness for Level 4 (album art matrix).

Desktop OAuth2 + PKCE handshake before porting to ESP32-C3.
Uses stdlib only (urllib) so the request shapes map 1:1 to firmware.

Run `python spotify_test.py --help` for subcommands.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import socket  # not currently used; reserved for future IPv6 socket handling
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Scopes: ask for minimum. user-read-playback-state is nice-to-have (volume, device)
# but not essential for album art. Spotify shows every scope on the consent screen.
# TODO: drop the second one if we never end up using device/volume info.
SCOPES = "user-read-currently-playing user-read-playback-state"

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "secrets.env"
TOKEN_FILE = SCRIPT_DIR / "tokens.json"
ART_DIR = SCRIPT_DIR / "art"

# Refresh this many seconds before the token actually expires. Guards against
# clock skew and against a token expiring mid-request.
EXPIRY_MARGIN_S = 60

USER_AGENT = "smarthome-monitor-level4-dev/1.0 (+desktop harness)"


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
# Mirrors the Arduino [TAG] convention so firmware and desktop logs look the same.

def log(tag: str, msg: str) -> None:
    """Print a tagged log line (matches Arduino serial output format)."""
    print(f"[{tag}] {msg}")


def die(msg: str, code: int = 1) -> None:
    """Print error and exit. Called from config validation, etc."""
    print(f"[FATAL] {msg}", file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config() -> dict:
    """
    Load Spotify client ID and redirect URI from secrets.env or environment.
    Environment variables override the file (useful for testing multiple apps).
    No third-party config lib; just simple KEY=VALUE parsing.
    """
    cfg: dict[str, str] = {}

    if CONFIG_FILE.exists():
        for lineno, raw in enumerate(CONFIG_FILE.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                log("CFG", f"ignoring malformed line {lineno} in {CONFIG_FILE.name}: {raw!r}")
                continue
            key, _, value = line.partition("=")
            cfg[key.strip()] = value.strip().strip("'\"")

    # Environment vars override file; handy for testing different apps
    for key in ("SPOTIFY_CLIENT_ID", "SPOTIFY_REDIRECT_URI"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]

    client_id = cfg.get("SPOTIFY_CLIENT_ID", "").strip()
    redirect_uri = cfg.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip()

    if not client_id or client_id.startswith("YOUR_"):
        die(
            f"SPOTIFY_CLIENT_ID is not set.\n"
            f"  Copy secrets.env.example to {CONFIG_FILE.name} and paste the Client ID\n"
            f"  from https://developer.spotify.com/dashboard -> your app -> Settings."
        )

    validate_redirect_uri(redirect_uri)
    return {"client_id": client_id, "redirect_uri": redirect_uri}


def validate_redirect_uri(uri: str) -> None:
    """
    Validate redirect URI before the OAuth handshake.
    Catch the most common mistake (using 'localhost' instead of 127.0.0.1).

    Spotify Nov 2025 rules:
      - HTTPS required, EXCEPT for loopback IPs (127.0.0.1, ::1)
      - If http://, must be a literal IP, NOT 'localhost'
    """
    parsed = urllib.parse.urlparse(uri)

    if parsed.hostname == "localhost":
        die(
            "Redirect URI uses `localhost`, which Spotify no longer accepts.\n"
            "  Use the IP literal instead: http://127.0.0.1:8888/callback\n"
            "  (and update it in the Spotify dashboard to match exactly)."
        )

    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "::1"):
        die(
            f"Redirect URI {uri!r} uses http:// with a non-loopback host.\n"
            "  Spotify requires https:// unless the host is the literal 127.0.0.1 or [::1]."
        )

    if parsed.scheme not in ("http", "https"):
        die(f"Redirect URI {uri!r} has an unsupported scheme.")


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

class SpotifyHTTPError(Exception):
    """HTTP error from Spotify. Parses both error response shapes."""

    def __init__(self, status: int, body: str, headers: dict):
        self.status = status
        self.body = body
        self.headers = headers
        # Spotify error responses have two shapes:
        #   {"error": {"status": 403, "message": "..."}},  or
        #   {"error": "invalid_grant", "error_description": "..."}
        try:
            parsed = json.loads(body)
            err = parsed.get("error", parsed)
            if isinstance(err, dict):
                detail = err.get("message") or err.get("error_description") or body
            else:
                detail = parsed.get("error_description") or str(err)
        except (json.JSONDecodeError, AttributeError):
            detail = body or "(empty body)"
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


def http_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    form: dict | None = None,
    verbose: bool = False,
    timeout: int = 15,
) -> tuple[int, bytes, dict]:
    """
    Single entry point for all HTTP. With --verbose, prints the exact request
    and response so you can port it to C++/Arduino.
    Returns (status, body, response_headers).
    Raises SpotifyHTTPError on error status.
    """
    headers = dict(headers or {})
    headers.setdefault("User-Agent", USER_AGENT)

    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(data))

    if verbose:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        print("--- REQUEST " + "-" * 55)
        print(f"{method} {path} HTTP/1.1")
        print(f"Host: {parsed.netloc}")
        for k, v in headers.items():
            print(f"{k}: {_redact(k, v)}")
        if data:
            print()
            print(_redact_form(form))
        print("-" * 67)

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            resp_headers = dict(resp.headers.items())
            status = resp.status
    except urllib.error.HTTPError as e:
        body = e.read()
        resp_headers = dict(e.headers.items()) if e.headers else {}
        if verbose:
            _dump_response(e.code, resp_headers, body)
        raise SpotifyHTTPError(e.code, body.decode("utf-8", "replace"), resp_headers) from None
    except urllib.error.URLError as e:
        die(f"Network error contacting {url}: {e.reason}")

    if verbose:
        _dump_response(status, resp_headers, body)

    return status, body, resp_headers


def _redact(key: str, value: str) -> str:
    """Truncate auth headers and other secrets in verbose output."""
    if key.lower() == "authorization" and len(value) > 20:
        return f"{value[:13]}...{value[-6:]}  (truncated)"
    return value


def _redact_form(form: dict | None) -> str:
    """Hide sensitive form fields in verbose output (tokens, codes, verifiers)."""
    if not form:
        return ""
    safe = {}
    for k, v in form.items():
        if k in ("refresh_token", "code", "code_verifier") and len(str(v)) > 12:
            safe[k] = f"{str(v)[:6]}...{str(v)[-4:]}"
        else:
            safe[k] = v
    return urllib.parse.urlencode(safe)


def _dump_response(status: int, headers: dict, body: bytes) -> None:
    """Pretty-print HTTP response for --verbose (useful for porting to firmware)."""
    print("--- RESPONSE " + "-" * 54)
    print(f"HTTP/1.1 {status}")
    for k in ("Content-Type", "Content-Length", "Retry-After", "Cache-Control"):
        if k in headers:
            print(f"{k}: {headers[k]}")
    if body:
        preview = body[:600].decode("utf-8", "replace")
        print()
        print(preview + ("..." if len(body) > 600 else ""))
    print("-" * 67)


# --------------------------------------------------------------------------
# PKCE
# --------------------------------------------------------------------------

def b64url(raw: bytes) -> str:
    """Base64url encoding without padding (RFC 7636)."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_pkce_pair() -> tuple[str, str]:
    """
    Generate (code_verifier, code_challenge) for PKCE OAuth flow.

    - verifier: high-entropy secret the client keeps
    - challenge: SHA256(verifier), sent in the browser URL

    Since SHA-256 is one-way, observing the challenge in the redirect doesn't
    let an attacker replay the token exchange. This is why PKCE works on ESP32
    without embedding a client secret in firmware (which can be dumped over UART).
    """
    verifier = b64url(secrets.token_bytes(64))          # 86 chars, spec allows 43-128
    challenge = b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# --------------------------------------------------------------------------
# OAuth callback server
# --------------------------------------------------------------------------
# For the handshake: Spotify redirects to http://127.0.0.1:8888/callback?code=...
# We need a local HTTP server to catch that and extract the code.
# Not needed on the ESP32 (or use a different strategy: pre-authorize and flash token).

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for the OAuth callback. Captures code= and state= from the redirect."""

    result: dict | None = None
    callback_path: str = "/callback"

    def do_GET(self):  # noqa: N802 (stdlib naming convention for BaseHTTPRequestHandler)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.callback_path:
            # Ignore favicon.ico, robots.txt, and other browser noise
            self.send_response(404)
            self.end_headers()
            return

        # Parse the query params Spotify sent us
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        type(self).result = params

        ok = "code" in params
        title = "Authorization complete" if ok else "Authorization failed"
        detail = (
            "You can close this tab and return to the terminal."
            if ok
            else f"Spotify returned: {params.get('error', 'unknown error')}"
        )
        page = f"""<!doctype html><meta charset="utf-8">
<title>{title}</title>
<body style="font-family:system-ui,sans-serif;max-width:34rem;margin:6rem auto;line-height:1.5">
<h2>{title}</h2><p>{detail}</p></body>"""

        body = page.encode("utf-8")
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        """Silence the default HTTP server stderr logging."""
        pass


def wait_for_callback(redirect_uri: str, timeout_s: int = 300) -> dict:
    """
    Spin up a local HTTP server and block until Spotify redirects to it.
    Returns the query params {code, state, error, ...} from the redirect.
    """
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    _CallbackHandler.result = None
    _CallbackHandler.callback_path = parsed.path or "/"

    try:
        server = http.server.HTTPServer((host, port), _CallbackHandler)
    except OSError as e:
        die(
            f"Could not bind {host}:{port} — {e}\n"
            f"  Something else is using that port. Either stop it, or pick another port\n"
            f"  in both secrets.env and the Spotify dashboard (they must match exactly)."
        )

    server.timeout = 1
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    log("AUTH", f"listening on {host}:{port}{_CallbackHandler.callback_path} for the redirect")

    deadline = time.monotonic() + timeout_s
    try:
        while _CallbackHandler.result is None:
            if time.monotonic() > deadline:
                die(f"Timed out after {timeout_s}s waiting for the browser redirect.")
            time.sleep(0.2)
    except KeyboardInterrupt:
        die("Cancelled.")
    finally:
        server.shutdown()
        server.server_close()

    return _CallbackHandler.result


# --------------------------------------------------------------------------
# Token storage
# --------------------------------------------------------------------------

def save_tokens(tokens: dict) -> None:
    """Write tokens.json. Make it readable only by the owner (Unix)."""
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(TOKEN_FILE, 0o600)


def load_tokens() -> dict:
    """Load tokens from disk. Called by every command except `auth`."""
    if not TOKEN_FILE.exists():
        die(f"No {TOKEN_FILE.name} found. Run:  python {Path(__file__).name} auth")
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"{TOKEN_FILE.name} is corrupt ({e}). Delete it and re-run `auth`.")


def _store_token_response(payload: dict, previous: dict | None = None) -> dict:
    """
    Parse a Spotify token response and return the dict we persist to disk.

    IMPORTANT: Spotify ROTATES refresh tokens on PKCE. The response may include
    a new refresh_token, which invalidates the old one. Firmware that doesn't
    write the new value back to NVS will work for days then fail with
    'invalid_grant' at random times — very hard to debug on a device with no
    console. Always persist the refreshed token if present.
    """
    previous = previous or {}
    tokens = {
        "access_token": payload["access_token"],
        "token_type": payload.get("token_type", "Bearer"),
        "scope": payload.get("scope", previous.get("scope", "")),
        "expires_at": time.time() + int(payload.get("expires_in", 3600)),
        "refresh_token": payload.get("refresh_token") or previous.get("refresh_token", ""),
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if payload.get("refresh_token") and previous.get("refresh_token") \
            and payload["refresh_token"] != previous["refresh_token"]:
        log("TOKEN", "refresh token was rotated by Spotify; the old one is now dead")
    return tokens


def get_access_token(cfg: dict, *, verbose: bool = False) -> str:
    """
    Return a valid access token, refreshing proactively if close to expiry.
    This avoids the 401 path during active use.
    """
    tokens = load_tokens()
    remaining = tokens.get("expires_at", 0) - time.time()

    # If we have time, use the token we've got
    if remaining > EXPIRY_MARGIN_S:
        return tokens["access_token"]

    if not tokens.get("refresh_token"):
        die("Access token expired and no refresh token is stored. Re-run `auth`.")

    # Refresh it
    log("TOKEN", "access token expired — refreshing" if remaining <= 0
        else f"access token expires in {int(remaining)}s — refreshing")
    try:
        _, body, _ = http_request(
            "POST",
            TOKEN_URL,
            form={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": cfg["client_id"],
            },
            verbose=verbose,
        )
    except SpotifyHTTPError as e:
        if e.status == 400:
            die(
                f"Refresh rejected ({e.detail}).\n"
                "  The refresh token was revoked, rotated out, or belongs to a different app.\n"
                f"  Delete {TOKEN_FILE.name} and run `auth` again."
            )
        raise

    tokens = _store_token_response(json.loads(body), previous=tokens)
    save_tokens(tokens)
    log("TOKEN", f"refreshed; valid for {int(tokens['expires_at'] - time.time())}s")
    return tokens["access_token"]


def api_get(cfg: dict, path: str, *, verbose: bool = False, _retried: bool = False) -> tuple[int, dict | None]:
    """
    GET an API path with automatic refresh on 401 and backoff on 429.
    Returns (status, parsed_json). Status 204 (No Content) returns (204, None).

    Note: Spotify returns 204 with an empty body when nothing is playing.
    Firmware that calls json.loads unconditionally will crash here.
    """
    token = get_access_token(cfg, verbose=verbose)
    try:
        status, body, _ = http_request(
            "GET",
            API_BASE + path,
            headers={"Authorization": f"Bearer {token}"},
            verbose=verbose,
        )
    except SpotifyHTTPError as e:
        if e.status == 401 and not _retried:
            # Backstop: token was rejected even though get_access_token() thought it was valid.
            # Force an immediate refresh and retry once.
            log("API", "401 from Spotify; forcing token refresh")
            tokens = load_tokens()
            tokens["expires_at"] = 0
            save_tokens(tokens)
            return api_get(cfg, path, verbose=verbose, _retried=True)
        if e.status == 429:
            # Rate limited. Respect Retry-After.
            wait = int(e.headers.get("Retry-After", "5")) + 1
            log("API", f"rate limited; Retry-After={wait}s")
            time.sleep(wait)
            return api_get(cfg, path, verbose=verbose, _retried=_retried)
        if e.status == 403:
            die(
                f"403 Forbidden: {e.detail}\n"
                "  Usually means the token lacks a required scope, or your app is in\n"
                "  Development Mode and this account is not on the allow-list\n"
                "  (dashboard -> your app -> User Management)."
            )
        raise

    if status == 204 or not body:
        return status, None
    return status, json.loads(body)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_auth(cfg: dict, args) -> None:
    """
    Run the one-time OAuth handshake. Opens the browser, catches the redirect,
    exchanges the code for tokens, and writes tokens.json.
    """
    verifier, challenge = make_pkce_pair()
    state = b64url(secrets.token_bytes(16))

    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    log("AUTH", "opening the Spotify consent page in your browser")
    print()
    print("  If it does not open automatically, paste this URL:")
    print(f"  {url}")
    print()

    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass

    result = wait_for_callback(cfg["redirect_uri"])

    if "error" in result:
        die(
            f"Spotify returned error={result['error']}.\n"
            "  `access_denied` means you clicked Cancel.\n"
            "  `invalid_client` almost always means the redirect URI in secrets.env does not\n"
            "  byte-for-byte match one registered in the dashboard (trailing slash counts)."
        )

    # Validate the state to prevent CSRF
    if result.get("state") != state:
        die("State mismatch — the callback did not come from the request we started. Aborting.")

    log("AUTH", "authorization code received; exchanging it for tokens")

    # Exchange the code for tokens. code_verifier proves we initiated the request.
    _, body, _ = http_request(
        "POST",
        TOKEN_URL,
        form={
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": cfg["redirect_uri"],
            "client_id": cfg["client_id"],
            "code_verifier": verifier,      # PKCE: proves we created the challenge
        },
        verbose=args.verbose,
    )

    tokens = _store_token_response(json.loads(body))
    save_tokens(tokens)

    print()
    log("AUTH", f"success — tokens written to {TOKEN_FILE.name}")
    log("AUTH", f"granted scopes: {tokens['scope']}")
    log("AUTH", f"access token valid for {int(tokens['expires_at'] - time.time())}s")
    log("AUTH", "the refresh token does not expire; this is the value the ESP32 stores in NVS")
    print()
    print(f"  Next:  python {Path(__file__).name} whoami")


def cmd_whoami(cfg: dict, args) -> None:
    """Smallest possible authenticated call — confirms the token works at all."""
    _, me = api_get(cfg, "/me", verbose=args.verbose)
    log("API", f"display name : {me.get('display_name')}")
    log("API", f"account id   : {me.get('id')}")

    # `product` is only populated when the token carries the `user-read-private`
    # scope, which we deliberately do not request — the album-art display has no
    # use for it. So an absent value means "not disclosed", NOT "free account".
    # The authoritative test of whether the player endpoints work is to call one.
    product = me.get("product")
    if product:
        log("API", f"product tier : {product}")
        if product != "premium":
            print()
            log("API", "note: /me/player/* requires Premium and will return 403 or")
            log("API", "      empty responses on a free account.")
    else:
        log("API", "product tier : not disclosed (needs the user-read-private scope, "
                   "which this app does not request)")
        log("API", "               run `now` while music is playing to confirm player access")


def _summarize(playing: dict | None) -> dict | None:
    """
    Extract the useful fields from /me/player/currently-playing.
    Handles both tracks and episodes (podcasts).
    Returns None if nothing is playing or the item is hidden (e.g. ad break).
    """
    if not playing or not playing.get("item"):
        return None

    item = playing["item"]

    # Podcasts have a different structure than tracks
    if item.get("type") == "episode":
        images = item.get("images", [])
        return {
            "kind": "episode",
            "title": item.get("name"),
            "artist": (item.get("show") or {}).get("name", ""),
            "album": (item.get("show") or {}).get("name", ""),
            "images": images,
            "is_playing": playing.get("is_playing", False),
            "progress_ms": playing.get("progress_ms") or 0,
            "duration_ms": item.get("duration_ms") or 0,
        }

    # Regular track
    album = item.get("album") or {}
    return {
        "kind": "track",
        "title": item.get("name"),
        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
        "album": album.get("name"),
        "images": album.get("images", []),
        "is_playing": playing.get("is_playing", False),
        "progress_ms": playing.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
    }


def pick_image(images: list[dict], target: int) -> dict | None:
    """
    Choose the image closest to `target` width (prefer smaller on a tie).
    Spotify returns ~3 sizes (640, 300, 64). The ESP32 wants 64px
    because 640px JPEG won't fit in C3's 400KB SRAM with TLS buffers.
    """
    sized = [im for im in images if im.get("width")]
    if not sized:
        return images[0] if images else None
    # Sort by: distance from target, then prefer smaller (for ties)
    return min(sized, key=lambda im: (abs(im["width"] - target), im["width"]))


def _bar(progress_ms: int, duration_ms: int, width: int = 28) -> str:
    """Draw an ASCII progress bar with elapsed/total time."""
    if not duration_ms:
        return ""
    filled = int(width * min(progress_ms / duration_ms, 1.0))
    def mm_ss(ms):
        return f"{ms // 60000}:{(ms // 1000) % 60:02d}"
    return f"[{'=' * filled}{'.' * (width - filled)}] {mm_ss(progress_ms)}/{mm_ss(duration_ms)}"


def download_art(image: dict, label: str, verbose: bool = False) -> Path | None:
    """
    Download album art JPEG from the URL in the image dict.
    Saves to art/{sanitized_label}_{width}.jpg.
    """
    ART_DIR.mkdir(exist_ok=True)
    # Sanitize the label to make a safe filename
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in label)[:60].strip() or "art"
    out = ART_DIR / f"{safe}_{image.get('width', 0)}.jpg"

    try:
        status, body, headers = http_request("GET", image["url"], verbose=verbose)
    except SpotifyHTTPError as e:
        log("ART", f"download failed: {e}")
        return None

    out.write_bytes(body)
    log("ART", f"{image.get('width')}x{image.get('height')} -> "
               f"{ART_DIR.name}/{out.name} ({len(body):,} bytes)")
    return out


def cmd_now(cfg: dict, args) -> None:
    """Fetch and display the currently-playing track once."""
    status, data = api_get(
        cfg,
        "/me/player/currently-playing?additional_types=track,episode",
        verbose=args.verbose,
    )

    # 204 No Content = nothing playing (empty body, no JSON)
    # Firmware that calls json.loads unconditionally will crash here,
    # and only when paused, making it look like a random intermittent bug.
    if status == 204 or data is None:
        log("NOW", "nothing is playing (HTTP 204, empty body)")
        return

    info = _summarize(data)
    if info is None:
        # Playing an ad, or in a private session (item is hidden)
        log("NOW", "a session is active but no item is exposed (private session or an ad)")
        return

    state = "playing" if info["is_playing"] else "paused"
    print()
    log("NOW", f"{info['title']}")
    log("NOW", f"  by     {info['artist']}")
    log("NOW", f"  on     {info['album']}  ({info['kind']}, {state})")
    bar = _bar(info["progress_ms"], info["duration_ms"])
    if bar:
        log("NOW", f"  {bar}")

    sizes = ", ".join(f"{im.get('width')}x{im.get('height')}" for im in info["images"])
    log("NOW", f"  art    {sizes or 'none available'}")
    print()

    if not args.save_art:
        log("NOW", "art download skipped (--no-save-art)")
    elif info["images"]:
        chosen = pick_image(info["images"], args.art_size)
        if chosen:
            download_art(chosen, f"{info['artist']} - {info['album']}", verbose=args.verbose)


def cmd_watch(cfg: dict, args) -> None:
    """
    Long-running poller. Fetch every N seconds, act only on track changes.
    Matches the firmware loop: polling is cheap (2KB JSON), but JPEG decode
    is expensive (8KB buffer, ~100ms on C3's single core). So we gate the
    download/decode on a change in (track, album) identity.
    """
    log("WATCH", f"polling every {args.interval}s — Ctrl+C to stop")
    last_key = object()   # Sentinel; equals nothing, so first call always triggers
    consecutive_errors = 0

    try:
        while True:
            try:
                status, data = api_get(
                    cfg,
                    "/me/player/currently-playing?additional_types=track,episode",
                    verbose=args.verbose,
                )
                consecutive_errors = 0
            except SpotifyHTTPError as e:
                # Exponential backoff on failure, capped at 2min
                consecutive_errors += 1
                backoff = min(args.interval * (2 ** consecutive_errors), 120)
                log("WATCH", f"error: {e} — backing off {backoff}s")
                if consecutive_errors >= 5:
                    die("Five consecutive failures. Stopping.")
                time.sleep(backoff)
                continue

            info = _summarize(data) if status != 204 else None
            # Track identity is (title, album). Skip everything else.
            key = None if info is None else (info["title"], info["album"])

            # On change, download art and report
            if key != last_key:
                stamp = datetime.now().strftime("%H:%M:%S")
                if info is None:
                    print(f"  {stamp}  --  nothing playing")
                else:
                    print(f"  {stamp}  ->  {info['title']} — {info['artist']}")
                    if args.save_art and info["images"]:
                        chosen = pick_image(info["images"], args.art_size)
                        if chosen:
                            download_art(chosen, f"{info['artist']} - {info['album']}",
                                         verbose=args.verbose)
                last_key = key

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print()
        log("WATCH", "stopped")


# --------------------------------------------------------------------------
# CLI setup
# --------------------------------------------------------------------------

def main() -> None:
    """Parse arguments and dispatch to the appropriate subcommand."""
    parser = argparse.ArgumentParser(
        description="Spotify Web API harness for the Level 4 album-art display.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Start with:  python spotify_test.py auth",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="dump raw HTTP requests/responses (useful when porting to ESP32)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("auth", help="run the one-time browser authorization")
    p_auth.set_defaults(func=cmd_auth)

    p_who = sub.add_parser("whoami", help="verify the token by fetching your profile")
    p_who.set_defaults(func=cmd_whoami)

    # Shared options for both 'now' and 'watch'
    for name, help_text, fn in (
        ("now", "fetch the currently-playing track once", cmd_now),
        ("watch", "poll continuously and react to track changes", cmd_watch),
    ):
        p = sub.add_parser(name, help=help_text)
        # Art download is on by default. An opt-in flag means you'd run
        # the command, see nothing in art/, and wonder what broke.
        p.add_argument("--no-save-art", dest="save_art", action="store_false",
                       help="skip downloading the album art JPEG")
        p.add_argument("--save-art", dest="save_art", action="store_true",
                       help=argparse.SUPPRESS)   # For muscle memory; already the default
        p.set_defaults(save_art=True)
        p.add_argument("--art-size", type=int, default=64, metavar="PX",
                       help="preferred art width in px (default: 64; what the ESP32 wants)")
        if name == "watch":
            p.add_argument("--interval", type=float, default=5.0, metavar="SEC",
                           help="seconds between polls (default: 5)")
        p.set_defaults(func=fn)

    args = parser.parse_args()
    cfg = load_config()
    args.func(cfg, args)


if __name__ == "__main__":
    main()
