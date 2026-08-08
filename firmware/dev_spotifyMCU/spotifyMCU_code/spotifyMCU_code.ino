/*
 * dev_spotifyMCU - talks to Spotify from the XIAO ESP32-C3
 *
 * Started as a bare "can this chip even fetch an image over HTTPS" test.
 * Now it actually authenticates against Spotify (refresh-token flow, no
 * client secret, no browser step on the device) and pulls whatever's
 * currently playing, dumping the album art out over serial so you can
 * check it landed intact. Still not the real display firmware - no JPEG
 * decode, no LED matrix - just the network + auth side proven out first.
 * See ../../level4_future_spotify_display/README.md for the bigger picture.
 *
 * The refresh token isn't generated here. Run the desktop script once:
 *     cd ../dev_spotifyAPI
 *     python spotify_test.py auth
 * and copy the refresh_token out of tokens.json into secrets.h. From then
 * on the device only ever does the token-refresh call, not the full OAuth
 * dance - no redirect URI, no callback server needed on-device.
 *
 * Set USE_SPOTIFY to 0 below to skip all of that and just re-fetch
 * TEST_IMAGE_URL on a timer instead, like the original version of this
 * sketch did. Useful for telling a network problem apart from a Spotify
 * problem.
 *
 * Board: XIAO_ESP32C3. Needs the ESP32 board package plus the ArduinoJson
 * library (Library Manager). Everything else - WiFiClientSecure,
 * HTTPClient, Preferences, mbedtls's base64 - ships with the board package.
 *
 * Setup: copy secrets_example.h to secrets.h, fill in WiFi + Spotify
 * client ID + refresh token, upload. Run spotifyMCU_reconstruction.py on
 * the other end to actually see the captured images.
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include "mbedtls/base64.h"
#include "secrets.h"

// flip to 0 to bypass Spotify and just hammer TEST_IMAGE_URL - see header comment
#define USE_SPOTIFY 1

// ---- config ----

constexpr uint32_t FETCH_INTERVAL_MS = 15000;   // how often we poll / re-fetch
constexpr uint32_t HTTP_TIMEOUT_MS   = 10000;
constexpr uint32_t TOKEN_EXPIRY_MARGIN_S = 60;  // refresh a bit early, not right at expiry

// generous cap for a bring-up test - real 64px art is more like 2-8KB.
// tighten this once you've actually seen what Spotify sends back.
constexpr size_t MAX_IMAGE_BYTES = 32768;
constexpr size_t BASE64_LINE_CHARS = 76;

// ---- globals ----

uint32_t lastFetchMs = 0;

uint8_t imageBuf[MAX_IMAGE_BYTES];                          // static, too big for the stack
uint8_t b64Buf[4 * ((MAX_IMAGE_BYTES + 2) / 3) + 8];         // base64 is ~4/3 bigger than the input

Preferences prefs;
String accessToken;
uint32_t accessTokenExpiresAt = 0;    // millis()-relative
String refreshToken;                  // starts from secrets.h, may get rotated by Spotify later
String lastTrackLabel;                // "artist - title", used to skip redundant art downloads

// ---- setup / loop ----

void setup() {
  Serial.begin(115200);
  delay(2000);  // give the serial monitor / reconstruction script time to attach

  Serial.println();
  Serial.println("[BOOT] dev_spotifyMCU");
  Serial.printf("[BOOT] image buf %uB, base64 buf %uB\n",
                (unsigned)sizeof(imageBuf), (unsigned)sizeof(b64Buf));

  prefs.begin("spotify", false);
  refreshToken = prefs.getString("refresh_token", SPOTIFY_REFRESH_TOKEN);

  connectWiFi();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] connection dropped, reconnecting");
    connectWiFi();
  }

  if (millis() - lastFetchMs < FETCH_INTERVAL_MS) return;
  lastFetchMs = millis();

#if USE_SPOTIFY
  if (!ensureAccessToken()) return;

  String artUrl, label;
  if (!fetchCurrentlyPlaying(artUrl, label)) return;

  if (label == lastTrackLabel) {
    Serial.print("[SPOTIFY] still playing: ");
    Serial.println(label);
    return;   // don't re-download art for a track we already have
  }
  lastTrackLabel = label;

  Serial.print("[SPOTIFY] now playing: ");
  Serial.println(label);

  if (artUrl.length() == 0) {
    Serial.println("[SPOTIFY] no art url in the response, skipping the image fetch");
    return;
  }
  fetchAndDumpImageFromUrl(artUrl);
#else
  fetchAndDumpImageFromUrl(TEST_IMAGE_URL);
#endif
}

// ---- wifi ----

void connectWiFi() {
  Serial.print("[WIFI] connecting to ");
  Serial.print(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(400);
    if (millis() - start > 20000) {
      Serial.println();
      Serial.println("[WIFI] still nothing after 20s, retrying");
      WiFi.disconnect(true);
      delay(500);
      WiFi.begin(WIFI_SSID, WIFI_PASS);
      start = millis();
    }
  }
  Serial.println();
  Serial.print("[WIFI] connected, IP ");
  Serial.println(WiFi.localIP());
}

// ---- spotify auth ----

// Trades the refresh token for a short-lived access token. Spotify can
// hand back a NEW refresh token here too (it rotates them under PKCE) -
// miss that and the device works fine for a while, then one day starts
// failing with invalid_grant for no obvious reason. Saving it to flash
// immediately is what avoids that.
bool ensureAccessToken() {
  if (accessToken.length() > 0 &&
      millis() < accessTokenExpiresAt - TOKEN_EXPIRY_MARGIN_S * 1000UL) {
    return true;
  }

  Serial.println("[TOKEN] refreshing");

  WiFiClientSecure client;
  client.setInsecure();   // bring-up only, same tradeoff as the image fetch below
  client.setTimeout(HTTP_TIMEOUT_MS);

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, "https://accounts.spotify.com/api/token")) {
    Serial.println("[TOKEN] begin() failed");
    return false;
  }
  http.addHeader("Content-Type", "application/x-www-form-urlencoded");

  String body = "grant_type=refresh_token&refresh_token=" + urlEncode(refreshToken) +
                "&client_id=" + urlEncode(SPOTIFY_CLIENT_ID);
  int status = http.POST(body);

  if (status != HTTP_CODE_OK) {
    Serial.printf("[TOKEN] refresh failed, HTTP %d\n", status);
    if (status > 0) Serial.println(http.getString());
    http.end();
    return false;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) {
    Serial.printf("[TOKEN] bad JSON: %s\n", err.c_str());
    return false;
  }

  const char *newAccess = doc["access_token"];
  if (!newAccess) {
    Serial.println("[TOKEN] no access_token in response");
    return false;
  }
  int expiresIn = doc["expires_in"] | 3600;
  accessToken = String(newAccess);
  accessTokenExpiresAt = millis() + (uint32_t)expiresIn * 1000UL;

  if (doc["refresh_token"].is<const char *>()) {
    String rotated = String((const char *)doc["refresh_token"]);
    if (rotated.length() > 0 && rotated != refreshToken) {
      refreshToken = rotated;
      prefs.putString("refresh_token", refreshToken);
      Serial.println("[TOKEN] refresh token rotated, saved the new one");
    }
  }

  Serial.printf("[TOKEN] good for %ds\n", expiresIn);
  return true;
}

// Minimal percent-encoding - Arduino doesn't ship one. Refresh tokens and
// client IDs are base64url-ish already so this rarely has much to do, but
// better to have it than assume.
String urlEncode(const String &s) {
  String out;
  out.reserve(s.length() * 3);
  const char *hex = "0123456789ABCDEF";
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if (isalnum((unsigned char)c) || c == '-' || c == '_' || c == '.' || c == '~') {
      out += c;
    } else {
      out += '%';
      out += hex[(c >> 4) & 0xF];
      out += hex[c & 0xF];
    }
  }
  return out;
}

// ---- spotify currently-playing ----

// Fills outLabel with "artist - title" and outImageUrl with whichever art
// size is closest to 64px (same idea as pick_image() in
// dev_spotifyAPI/spotify_test.py, just redone in C++ since the device
// can't shell out to Python for this one). Returns false if nothing is
// playing or the request failed - either way there's nothing to fetch.
bool fetchCurrentlyPlaying(String &outImageUrl, String &outLabel) {
  outImageUrl = "";
  outLabel = "";

  WiFiClientSecure client;
  client.setInsecure();
  client.setTimeout(HTTP_TIMEOUT_MS);

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(client, "https://api.spotify.com/v1/me/player/currently-playing")) {
    Serial.println("[SPOTIFY] begin() failed");
    return false;
  }
  http.addHeader("Authorization", "Bearer " + accessToken);

  int status = http.GET();

  // 204 with an empty body is how Spotify says "nothing's playing" - not
  // an error, just nothing to do this cycle.
  if (status == 204) {
    Serial.println("[SPOTIFY] nothing playing right now");
    http.end();
    return false;
  }
  if (status != HTTP_CODE_OK) {
    Serial.printf("[SPOTIFY] currently-playing returned HTTP %d\n", status);
    http.end();
    return false;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) {
    Serial.printf("[SPOTIFY] bad JSON: %s\n", err.c_str());
    return false;
  }

  JsonObject item = doc["item"];
  if (item.isNull()) {
    Serial.println("[SPOTIFY] session active but no track exposed (ad, or private session)");
    return false;
  }

  const char *title = item["name"] | "(untitled)";
  const char *artist = item["artists"][0]["name"] | "(unknown artist)";
  outLabel = String(artist) + " - " + String(title);

  JsonArray images = item["album"]["images"];
  int bestDiff = INT32_MAX;
  for (JsonObject img : images) {
    int w = img["width"] | 0;
    int diff = abs(w - 64);
    if (diff < bestDiff) {
      bestDiff = diff;
      const char *url = img["url"] | "";
      outImageUrl = String(url);
    }
  }

  return true;
}

// ---- image fetch + serial dump ----

void fetchAndDumpImageFromUrl(const String &url) {
  Serial.print("[HTTP] GET ");
  Serial.println(url);

  WiFiClientSecure client;
  // bring-up only: skipping cert validation so we can prove the HTTP path
  // works before spending time on a real root CA bundle. don't ship this -
  // see level4_future_spotify_display/README.md for what that'd take.
  client.setInsecure();
  client.setTimeout(HTTP_TIMEOUT_MS);

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);

  // http.header() won't return Content-Type unless we ask for it by name
  // first - an ESP32 HTTPClient quirk, not optional.
  const char *headerKeys[] = {"Content-Type"};
  http.collectHeaders(headerKeys, 1);

  if (!http.begin(client, url)) {
    Serial.println("[HTTP] begin() failed, bad URL?");
    return;
  }

  uint32_t t0 = millis();
  int status = http.GET();

  if (status != HTTP_CODE_OK) {
    if (status < 0) {
      Serial.printf("[HTTP] request failed: %s\n", http.errorToString(status).c_str());
    } else {
      Serial.printf("[HTTP] server returned HTTP %d\n", status);
    }
    http.end();
    return;
  }

  int contentLength = http.getSize();   // -1 if the server didn't send one
  String contentType = http.header("Content-Type");
  Serial.printf("[HTTP] 200 OK, type=%s, length=%d\n", contentType.c_str(), contentLength);

  if (contentLength > (int)MAX_IMAGE_BYTES) {
    Serial.printf("[HTTP] image is %d bytes, over the %u-byte cap - skipping\n",
                  contentLength, (unsigned)MAX_IMAGE_BYTES);
    http.end();
    return;
  }

  size_t received = readImageBody(http, imageBuf, MAX_IMAGE_BYTES);
  http.end();

  Serial.printf("[HTTP] got %u bytes in %lums\n", (unsigned)received, millis() - t0);

  if (received == 0) {
    Serial.println("[HTTP] zero bytes received");
    return;
  }

  dumpImageAsBase64(imageBuf, received, contentType);
}

// reads the response body into dest (capacity cap), stopping on connection
// close, hitting cap, or a stall longer than HTTP_TIMEOUT_MS.
size_t readImageBody(HTTPClient &http, uint8_t *dest, size_t cap) {
  WiFiClient *stream = http.getStreamPtr();
  size_t received = 0;
  uint32_t lastProgressMs = millis();

  while (http.connected() && received < cap) {
    size_t avail = (size_t)stream->available();
    if (avail > 0) {
      size_t toRead = min(avail, cap - received);
      int n = stream->readBytes(dest + received, toRead);
      if (n > 0) {
        received += n;
        lastProgressMs = millis();
      }
    } else if (!http.connected()) {
      break;
    }

    if (millis() - lastProgressMs > HTTP_TIMEOUT_MS) {
      Serial.println("[HTTP] stalled mid-download, giving up");
      break;
    }
    if (avail == 0) delay(1);  // yield, not a real delay loop
  }
  return received;
}

// base64-encodes data and prints it framed with BEGIN/END markers that
// spotifyMCU_reconstruction.py parses on the other end. base64 instead of
// raw bytes because a dropped byte in a raw binary serial stream produces
// an unrecoverable frame and that's a bad way to spend an evening. text
// either arrives intact or the corruption is obvious.
void dumpImageAsBase64(const uint8_t *data, size_t len, const String &contentType) {
  size_t b64Len = 0;
  int rc = mbedtls_base64_encode(b64Buf, sizeof(b64Buf), &b64Len, data, len);
  if (rc != 0) {
    Serial.printf("[IMG] base64 encode failed (%d) - image bigger than expected?\n", rc);
    return;
  }

  Serial.println();
  Serial.printf("<<<IMG_BEGIN len=%u type=%s>>>\n", (unsigned)len, contentType.c_str());
  for (size_t i = 0; i < b64Len; i += BASE64_LINE_CHARS) {
    size_t chunk = min((size_t)BASE64_LINE_CHARS, b64Len - i);
    Serial.write(b64Buf + i, chunk);
    Serial.println();
  }
  Serial.println("<<<IMG_END>>>");

  Serial.printf("[IMG] sent %u bytes as %u base64 chars\n", (unsigned)len, (unsigned)b64Len);
}
