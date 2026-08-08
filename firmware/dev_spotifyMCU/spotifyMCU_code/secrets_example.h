/*
 * secrets_example.h — dev_spotifyMCU
 *
 * Copy this file to `secrets.h` in the same directory and fill in the
 * values. `secrets.h` is listed in the repository .gitignore and MUST NOT
 * be committed.
 */

#ifndef SECRETS_H
#define SECRETS_H

// WiFi network to connect to. 2.4 GHz only - the ESP32-C3 does not support 5 GHz.
#define WIFI_SSID       "YOUR_WIFI_SSID"
#define WIFI_PASS       "YOUR_WIFI_PASSWORD"

// A single test image URL, fetched once per FETCH_INTERVAL_MS to prove out
// the HTTPS GET path. Grab a real, current album-art URL with:
//
//     python ../dev_spotifyAPI/spotify_test.py now --verbose --art-size 64
//
// and copy the 64px entry's "url" field out of the printed JSON response.
// Any small HTTPS JPEG works if you just want to test connectivity before
// Spotify is involved at all — e.g. a direct link to a small image on
// i.scdn.co, or any other HTTPS image host.
#define TEST_IMAGE_URL  "https://i.scdn.co/image/REPLACE_WITH_A_REAL_URL"

#endif  // SECRETS_H
