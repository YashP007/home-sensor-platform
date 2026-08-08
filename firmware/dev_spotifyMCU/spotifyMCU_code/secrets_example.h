/*
 * secrets_example.h
 *
 * Copy this to secrets.h in the same folder and fill in your own values.
 * secrets.h is in .gitignore - don't commit it.
 */

#ifndef SECRETS_H
#define SECRETS_H

// WiFi network to connect to. 2.4 GHz only - the C3 doesn't do 5 GHz.
#define WIFI_SSID       "YOUR_WIFI_SSID"
#define WIFI_PASS       "YOUR_WIFI_PASSWORD"

// Static test image, used when USE_SPOTIFY is set to 0 in the sketch (handy
// for checking the network path still works without dragging Spotify auth
// into it). Grab a live album-art URL with:
//     python ../dev_spotifyAPI/spotify_test.py now --verbose --art-size 64
// and paste the 64px "url" field here. Any small HTTPS JPEG works too.
#define TEST_IMAGE_URL  "https://i.scdn.co/image/REPLACE_WITH_A_REAL_URL"

// Spotify app credentials, from developer.spotify.com/dashboard -> your app
// -> Settings. No client secret - this is a PKCE public client, same as the
// desktop script, so there's nothing here that needs to stay secret at rest.
#define SPOTIFY_CLIENT_ID      "YOUR_SPOTIFY_CLIENT_ID"

// Run the desktop auth flow once and copy the refresh_token out of the
// tokens.json it produces:
//     cd ../dev_spotifyAPI
//     python spotify_test.py auth
// The device only ever exchanges this for an access token - it never opens
// a browser or talks to accounts.spotify.com's authorize page itself.
#define SPOTIFY_REFRESH_TOKEN  "YOUR_SPOTIFY_REFRESH_TOKEN"

#endif  // SECRETS_H
