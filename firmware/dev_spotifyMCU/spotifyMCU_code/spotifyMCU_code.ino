/*
 * SmartHome Monitor — dev_spotifyMCU: HTTPS image fetch test (stepping stone)
 *
 * This is NOT the real album-art pipeline. It is the smallest useful proof
 * that the XIAO ESP32-C3 can open a TLS connection, GET a binary image over
 * HTTPS, and hand the bytes back intact — before layering OAuth, JSON
 * parsing, and JPEG decode on top of it. See
 * firmware/level4_future_spotify_display/README.md for why that combination
 * is the hard part on this chip; this sketch isolates just the networking
 * piece so a bug there doesn't get confused with a bug in the rest.
 *
 * What it does, every FETCH_INTERVAL_MS:
 *   1. Confirm WiFi is up (reconnect if not).
 *   2. HTTPS GET a single test image. The URL lives in secrets.h — grab a
 *      real, live one with:
 *          python ../dev_spotifyAPI/spotify_test.py now --verbose --art-size 64
 *      and copy the 64px "url" field out of the printed JSON. Any small
 *      HTTPS JPEG works too if you just want to prove connectivity before
 *      Spotify is involved at all.
 *   3. Base64-encode the bytes and print them to Serial, wrapped in
 *      BEGIN/END markers.
 *
 * Run the companion script in another terminal while this is uploaded and
 * running:
 *      python spotifyMCU_reconstruction.py --port COM5
 * (COM port varies — check the Arduino IDE's Tools > Port menu, or Device
 * Manager on Windows.) It parses the markers, decodes the image, and saves
 * it to captures/ so you can actually open the file and confirm the round
 * trip produced a real, undamaged JPEG — not just "some bytes arrived."
 *
 * Hardware:
 *   - XIAO ESP32-C3, USB connected. No sensors needed for this test.
 *
 * Setup:
 *   1. Copy `secrets_example.h` (this directory) to `secrets.h`. Fill in
 *      WiFi credentials and TEST_IMAGE_URL.
 *   2. Board: XIAO_ESP32C3. Upload.
 *   3. Serial Monitor at 115200 baud is optional — it shows the [WIFI]/
 *      [HTTP]/[IMG] status lines, but the image payload itself is much
 *      easier to work with through spotifyMCU_reconstruction.py.
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "mbedtls/base64.h"
#include "secrets.h"

// ─── Configuration ──────────────────────────────────────────────────────

constexpr uint32_t FETCH_INTERVAL_MS = 15000;   // time between test fetches
constexpr uint32_t HTTP_TIMEOUT_MS   = 10000;   // connect + per-read stall timeout

// Hard cap on how large an image we'll accept. Generous on purpose for this
// bring-up test — a real 64px album-art JPEG is typically 2-8 KB, so this
// leaves a lot of headroom to see the actual size before tightening it.
// Shrink this once you've confirmed what Spotify actually sends; there's no
// reason to keep a 32 KB buffer around once you know you need 8 KB of it.
constexpr size_t MAX_IMAGE_BYTES = 32768;

constexpr size_t BASE64_LINE_CHARS = 76;   // standard MIME wrap width

// ─── Globals ────────────────────────────────────────────────────────────

uint32_t lastFetchMs = 0;

// Static, not stack-allocated: MAX_IMAGE_BYTES is too big for the C3's
// default stack and we only ever need one of these at a time anyway.
uint8_t imageBuf[MAX_IMAGE_BYTES];

// Base64 expands data by 4/3. Sized once at compile time against the same
// cap as imageBuf, so there's no separate "did the encode buffer fit"
// bookkeeping at runtime.
uint8_t b64Buf[4 * ((MAX_IMAGE_BYTES + 2) / 3) + 8];

// ─── Setup ──────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  delay(2000);  // allow serial monitor / reconstruction script to attach

  Serial.println();
  Serial.println("[BOOT] dev_spotifyMCU — HTTPS image fetch test");
  Serial.printf("[BOOT] image buffer: %u bytes, base64 buffer: %u bytes\n",
                (unsigned)sizeof(imageBuf), (unsigned)sizeof(b64Buf));

  connectWiFi();
}

// ─── Main loop ──────────────────────────────────────────────────────────

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] Connection lost — reconnecting.");
    connectWiFi();
  }

  if (millis() - lastFetchMs < FETCH_INTERVAL_MS) return;
  lastFetchMs = millis();

  fetchAndDumpImage();
}

// ─── WiFi ───────────────────────────────────────────────────────────────

void connectWiFi() {
  Serial.print("[WIFI] Connecting to ");
  Serial.print(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(400);
    if (millis() - start > 20000) {
      Serial.println();
      Serial.println("[WIFI] ERROR: no connection after 20s. Retrying from scratch.");
      WiFi.disconnect(true);
      delay(500);
      WiFi.begin(WIFI_SSID, WIFI_PASS);
      start = millis();
    }
  }
  Serial.println();
  Serial.print("[WIFI] Connected. IP: ");
  Serial.println(WiFi.localIP());
}

// ─── HTTPS image fetch ──────────────────────────────────────────────────

void fetchAndDumpImage() {
  Serial.print("[HTTP] GET ");
  Serial.println(TEST_IMAGE_URL);

  WiFiClientSecure client;
  // Bring-up only: skip certificate validation so we can prove the HTTP
  // path works before spending time on a trusted root CA bundle. This must
  // NOT ship to production — see level4_future_spotify_display/README.md.
  client.setInsecure();
  client.setTimeout(HTTP_TIMEOUT_MS);

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);

  // Content-Type isn't returned by http.header() unless we ask for it by
  // name up front — this is an ESP32 HTTPClient quirk, not optional.
  const char *headerKeys[] = {"Content-Type"};
  http.collectHeaders(headerKeys, 1);

  if (!http.begin(client, TEST_IMAGE_URL)) {
    Serial.println("[HTTP] ERROR: begin() failed — malformed URL?");
    return;
  }

  uint32_t t0 = millis();
  int status = http.GET();

  if (status != HTTP_CODE_OK) {
    if (status < 0) {
      Serial.printf("[HTTP] ERROR: request failed — %s\n",
                    http.errorToString(status).c_str());
    } else {
      Serial.printf("[HTTP] ERROR: server returned HTTP %d\n", status);
    }
    http.end();
    return;
  }

  int contentLength = http.getSize();   // -1 if the server omitted Content-Length
  String contentType = http.header("Content-Type");
  Serial.printf("[HTTP] 200 OK, Content-Type=%s, Content-Length=%d\n",
                contentType.c_str(), contentLength);

  if (contentLength > (int)MAX_IMAGE_BYTES) {
    Serial.printf("[HTTP] ERROR: image is %d bytes, over the %u-byte test cap. "
                  "Ask for a smaller art size.\n",
                  contentLength, (unsigned)MAX_IMAGE_BYTES);
    http.end();
    return;
  }

  size_t received = readImageBody(http, imageBuf, MAX_IMAGE_BYTES);
  http.end();

  Serial.printf("[HTTP] Received %u bytes in %lums\n",
                (unsigned)received, millis() - t0);

  if (received == 0) {
    Serial.println("[HTTP] ERROR: zero bytes received.");
    return;
  }

  dumpImageAsBase64(imageBuf, received, contentType);
}

// Reads the response body into `dest` (capacity `cap`), stopping when the
// server closes the connection, we hit `cap`, or nothing arrives for
// HTTP_TIMEOUT_MS. Returns the number of bytes actually read.
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
      Serial.println("[HTTP] ERROR: stalled mid-download, giving up.");
      break;
    }
    if (avail == 0) delay(1);   // brief yield while waiting for more data
  }
  return received;
}

// ─── Serial framing ─────────────────────────────────────────────────────

// Base64-encodes `data` and prints it to Serial wrapped in BEGIN/END
// markers that spotifyMCU_reconstruction.py knows how to parse. Base64
// rather than raw bytes on purpose — plain binary over a USB-serial link
// is one dropped byte away from an unrecoverable frame, and debugging that
// is a bad use of an evening. Text is slower but it either arrives intact
// or it's obviously broken.
void dumpImageAsBase64(const uint8_t *data, size_t len, const String &contentType) {
  size_t b64Len = 0;
  int rc = mbedtls_base64_encode(b64Buf, sizeof(b64Buf), &b64Len, data, len);
  if (rc != 0) {
    Serial.printf("[IMG]  ERROR: base64 encode failed (%d) — image bigger than expected?\n", rc);
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

  Serial.printf("[IMG]  Sent %u raw bytes as %u base64 chars.\n",
                (unsigned)len, (unsigned)b64Len);
}
