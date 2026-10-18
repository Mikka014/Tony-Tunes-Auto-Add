#!/usr/bin/env python3
"""One-time helper: log in to Spotify and print a refresh token for bot.py.

Run this on YOUR computer (not in GitHub):
    python get_refresh_token.py            # opens a browser, catches the redirect automatically
    python get_refresh_token.py --manual   # you paste the redirected URL back in

It asks for your Spotify app's Client ID and Client Secret (or reads them from the
SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET environment variables).
The redirect URI http://127.0.0.1:8888/callback must be added to the app in the Spotify dashboard.
"""
import http.server
import os
import secrets
import sys
import urllib.parse
import webbrowser

import requests

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-public playlist-modify-private"
)


def main():
    client_id = os.environ.get("SPOTIFY_CLIENT_ID") or input("Spotify Client ID: ").strip()
    client_secret = (
        os.environ.get("SPOTIFY_CLIENT_SECRET") or input("Spotify Client Secret: ").strip()
    )
    state = secrets.token_urlsafe(16)
    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPES,
            "state": state,
        }
    )

    print("\nOpen this link, log in, and click Agree:\n")
    print(auth_url + "\n")

    if "--manual" in sys.argv:
        redirected = input("After agreeing, paste the full URL you were redirected to: ").strip()
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(redirected).query))
    else:
        webbrowser.open(auth_url)
        params = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                query = urllib.parse.urlparse(self.path).query
                params.update(dict(urllib.parse.parse_qsl(query)))
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"All set. You can close this tab and go back to the terminal.")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 8888), Handler)
        while "code" not in params and "error" not in params:
            server.handle_request()
        server.server_close()

    if params.get("state") != state:
        sys.exit("State mismatch. Start over.")
    if "error" in params or "code" not in params:
        sys.exit(f"Spotify login failed: {params.get('error', 'no code returned')}")

    r = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "authorization_code",
            "code": params["code"],
            "redirect_uri": REDIRECT_URI,
        },
        auth=(client_id, client_secret),
        timeout=20,
    )
    if r.status_code != 200:
        sys.exit(f"Token exchange failed ({r.status_code}): {r.text}")

    print("\nYour refresh token (save it as the SPOTIFY_REFRESH_TOKEN secret; keep it private):\n")
    print(r.json()["refresh_token"])


if __name__ == "__main__":
    main()
