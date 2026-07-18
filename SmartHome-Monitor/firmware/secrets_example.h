/*
 * secrets_example.h
 *
 * Template for credentials. Copy this file to `secrets.h` in the same
 * directory as the sketch you are compiling, and populate the values.
 *
 * `secrets.h` is listed in the repository .gitignore and MUST NOT be
 * committed. If you accidentally commit real credentials, rotate them
 * immediately — bots scrape public GitHub repos for exposed keys within
 * minutes.
 */

#ifndef SECRETS_H
#define SECRETS_H

// WiFi network to connect to. 2.4 GHz only — the ESP32-C3 does not support 5 GHz.
#define WIFI_SSID       "YOUR_WIFI_SSID"
#define WIFI_PASS       "YOUR_WIFI_PASSWORD"

// Adafruit IO account credentials.
// Find your key at: https://io.adafruit.com/<username>/keys
#define IO_USERNAME     "YOUR_ADAFRUIT_IO_USERNAME"
#define IO_KEY          "YOUR_ADAFRUIT_IO_KEY"

// Adafruit IO feed names. These must match feeds you have already created
// in the Adafruit IO dashboard, otherwise publishes will silently fail.
#define IO_FEED_TEMP    "home-temperature"
#define IO_FEED_HUMID   "home-humidity"
#define IO_FEED_PRESS   "home-pressure"
#define IO_FEED_HVAC    "home-hvac-state"   // Used by Level 2+
#define IO_FEED_LUX     "home-lux"          // Used by Level 3+

#endif  // SECRETS_H
