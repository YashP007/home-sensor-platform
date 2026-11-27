/*
 * secrets_example.h
 *
 * Template for credentials. Copy this file to `secrets.h` in the same
 * directory as the sketch you are compiling, and fill in the values.
 *
 * `secrets.h` is listed in the repository .gitignore and MUST NOT be
 * committed. If you accidentally commit real credentials, rotate them
 * immediately; bots scrape public GitHub repos for exposed keys within
 * minutes.
 */

#ifndef SECRETS_H
#define SECRETS_H

// WiFi network. 2.4 GHz only: the ESP32-C3 does not support 5 GHz.
#define WIFI_SSID       "YOUR_WIFI_SSID"
#define WIFI_PASS       "YOUR_WIFI_PASSWORD"

// Adafruit IO account. Find your key at https://io.adafruit.com/ (Key icon).
#define IO_USERNAME     "YOUR_ADAFRUIT_IO_USERNAME"
#define IO_KEY          "YOUR_ADAFRUIT_IO_KEY"

// Level 2 only: which board this build is for.
//   ROLE_MAIN        living room: BME280 + TSL2591 + outdoor temperature
//   ROLE_PERIPHERAL  bedroom:     BME280 only
#define DEVICE_ROLE     ROLE_MAIN

// Level 2 main board only: location for the Open-Meteo outdoor temperature.
// Two decimals (about 1 km) is plenty for a weather-model grid.
#define OUTDOOR_LAT     "42.36"
#define OUTDOOR_LON     "-71.06"

#endif  // SECRETS_H
