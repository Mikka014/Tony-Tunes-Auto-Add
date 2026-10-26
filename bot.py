#!/usr/bin/env python3
"""Discord -> Spotify playlist sync.

Runs once and exits (built to be run on a schedule by GitHub Actions).

Each run:
  1. Reads the latest 100 messages from one Discord channel. (With BACKFILL_SINCE set, it
     pages back through the channel's whole history to that date instead.)
  2. Skips messages the bot has already marked with a reaction (stateless memory).
  3. Pulls music links out of the rest, works out artist + title, finds the track on Spotify.
  4. Skips anything already in the playlist (by Spotify ID, and by artist + title).
  5. Adds new tracks and reacts to the Discord message:
       ✅ added    🔁 already in the playlist    ❓ couldn't find a match

Environment variables (GitHub Actions secrets):
  DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID,
  SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_REFRESH_TOKEN, SPOTIFY_PLAYLIST_ID
Optional:
  DRY_RUN=1                  log what would happen without adding tracks or reacting
  BACKFILL_SINCE=2026-01-01  one-time catch-up: scan every message back to this date (UTC)
"""
import html
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

DISCORD_API = "https://discord.com/api/v10"
SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

ADDED, DUPLICATE, NOT_FOUND = "✅", "🔁", "❓"
MARKERS = {ADDED, DUPLICATE, NOT_FOUND}

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


class TransientError(Exception):
    """Spotify is rate-limiting or having a bad moment. The message is left unmarked so a
    later run retries it, instead of being wrongly stamped as 'not found'."""

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

SPOTIFY_TRACK_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[A-Za-z-]+/)?track/([A-Za-z0-9]{22})"
)
URL_RE = re.compile(r"https?://[^\s<>\[\]()]+")
SPOTIFY_SHORT_HOSTS = ("spotify.link", "spoti.fi", "spotify.app.link")
MUSIC_HOSTS = (
    "youtube.com",
    "youtu.be",
    "soundcloud.com",
    "music.apple.com",
    "bandcamp.com",
    "tidal.com",
    "deezer.com",
)


# --------------------------------------------------------------------------- helpers


def need(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def request(method, url, **kwargs):
    """requests wrapper that waits out rate limits and retries server errors."""
    resp = None
    for attempt in range(4):
        resp = requests.request(method, url, timeout=20, **kwargs)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", "2"))
            time.sleep(min(wait, 30) + 0.5)
            continue
        if resp.status_code >= 500:
            time.sleep(2**attempt)
            continue
        return resp
    return resp


def tokens(text):
    return re.findall(r"\w+", text.lower())


def track_key(name, artist):
    """Normalized 'artist|title' key used to catch duplicates that have different Spotify IDs."""
    return f"{' '.join(tokens(artist))}|{' '.join(tokens(name))}"


def extract_urls(content):
    urls = []
    for raw in URL_RE.findall(content or ""):
        url = raw.rstrip(".,;:!?")
        if url not in urls:
            urls.append(url)
    return urls


def host_of(url):
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def host_matches(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def parse_ts(stamp):
    """Discord timestamps look like 2026-01-02T03:04:05.123000+00:00."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def parse_since(value):
    """BACKFILL_SINCE like '2026-01-01' -> midnight UTC on that date, or None if blank."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        sys.exit(f"BACKFILL_SINCE must look like 2026-01-01, but got: {value!r}")


# --------------------------------------------------------------------------- title parsing

_JUNK_BRACKET = re.compile(
    r"[\(\[\{][^\)\]\}]*\b(official|lyrics?|video|audio|visuali[sz]er|hd|hq|4k|explicit)\b[^\)\]\}]*[\)\]\}]",
    re.I,
)


def clean_title(text):
    text = html.unescape(text or "")
    text = _JUNK_BRACKET.sub("", text)
    text = re.sub(r"\s*[|｜].*$", "", text)  # "Song | Channel name"
    return re.sub(r"\s+", " ", text).strip(" -–—")


def clean_artist(name):
    name = html.unescape(name or "")
    name = re.sub(r"\s*-\s*Topic$", "", name, flags=re.I)
    name = re.sub(r"VEVO$", "", name, flags=re.I)
    return name.strip()


def split_artist_title(raw_title, author=None):
    """'Artist - Title' -> (artist, title). Falls back to the uploader name for the artist."""
    title = clean_title(raw_title)
    for sep in (" - ", " – ", " — "):
        if sep in title:
            artist, rest = title.split(sep, 1)
            return clean_artist(artist), clean_title(rest)
    return clean_artist(author), title


def fetch_metadata(url):
    """Return (artist, title) for a non-Spotify music link, or None."""
    host = host_of(url)
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.8"}
    try:
        if host_matches(host, ("youtube.com", "youtu.be")):
            yt_url = url.replace("music.youtube.com", "www.youtube.com")
            r = request(
                "GET",
                "https://www.youtube.com/oembed",
                params={"url": yt_url, "format": "json"},
                headers=headers,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            author = data.get("author_name", "")
            artist, title = split_artist_title(data.get("title", ""), author)
            return (artist, title) if title else None

        if host_matches(host, ("soundcloud.com",)):
            r = request(
                "GET",
                "https://soundcloud.com/oembed",
                params={"url": url, "format": "json"},
                headers=headers,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            raw = html.unescape(data.get("title", ""))
            m = re.match(r"^(.*) by (.*)$", raw)
            if m:
                return clean_artist(m.group(2)), clean_title(m.group(1))
            return split_artist_title(raw, data.get("author_name", ""))

        # Apple Music, Bandcamp, Tidal, Deezer: read the page's og:title
        r = request("GET", url, headers=headers, allow_redirects=True)
        if r.status_code != 200:
            return None
        m = re.search(
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            r.text,
            re.I,
        ) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
            r.text,
            re.I,
        )
        if not m:
            return None
        og = html.unescape(m.group(1)).strip()
        # "Song by Artist on Apple Music", "Song - Single by Artist", "Song, by Artist"
        m2 = re.match(
            r"^(.*?)(?:\s+-\s+(?:Single|EP))?,?\s+by\s+(.*?)(?:\s+on Apple Music|\s*\|.*)?$",
            og,
        )
        if m2:
            return clean_artist(m2.group(2)), clean_title(m2.group(1))
        artist, title = split_artist_title(og)
        return (artist, title) if title else None
    except requests.RequestException:
        return None


# --------------------------------------------------------------------------- Discord


class Discord:
    def __init__(self, token, channel_id):
        self.channel_id = channel_id
        self.headers = {"Authorization": f"Bot {token}"}

    def messages(self, since=None, max_pages=1000):
        """Messages oldest-first. Normally just the latest 100. With `since` (a datetime),
        page backwards 100 at a time until the channel's history reaches that date."""
        collected, before, pages = [], None, 0
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            r = request(
                "GET",
                f"{DISCORD_API}/channels/{self.channel_id}/messages",
                headers=self.headers,
                params=params,
            )
            if r.status_code != 200:
                sys.exit(
                    f"Discord returned {r.status_code} reading the channel: {r.text[:300]}\n"
                    "Check the channel ID and that the bot can View Channel + Read Message History."
                )
            batch = r.json()  # newest first
            if not batch:
                break
            collected.extend(batch)
            before = batch[-1]["id"]
            pages += 1
            if since is None or len(batch) < 100 or pages >= max_pages:
                break
            if parse_ts(batch[-1]["timestamp"]) < since:
                break
            time.sleep(0.3)
            if pages % 10 == 0:
                print(f"  ...read {len(collected)} messages so far")
        if since is not None:
            collected = [m for m in collected if parse_ts(m["timestamp"]) >= since]
        collected.reverse()  # oldest first, so the playlist ends up in chronological order
        return collected

    @staticmethod
    def is_marked(msg):
        return any(
            rx.get("me") and rx.get("emoji", {}).get("name") in MARKERS
            for rx in msg.get("reactions", [])
        )

    def react(self, msg_id, emoji):
        if DRY_RUN:
            return True
        r = request(
            "PUT",
            f"{DISCORD_API}/channels/{self.channel_id}/messages/{msg_id}"
            f"/reactions/{quote(emoji)}/@me",
            headers=self.headers,
        )
        time.sleep(0.4)  # stay well inside Discord's reaction rate limit
        return r.status_code in (200, 204)


# --------------------------------------------------------------------------- Spotify


class Spotify:
    def __init__(self, client_id, client_secret, refresh_token, playlist_id):
        self.playlist_id = playlist_id
        self._creds = (client_id, client_secret, refresh_token)
        self.headers = {}
        self._refresh()

    def _refresh(self):
        client_id, client_secret, refresh_token = self._creds
        r = request(
            "POST",
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
        )
        if r.status_code != 200:
            sys.exit(f"Spotify token refresh failed ({r.status_code}): {r.text[:300]}")
        self.headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    def _call(self, method, url, **kwargs):
        """Spotify passes expire after an hour, so a long backfill refreshes and retries once."""
        r = request(method, url, headers=self.headers, **kwargs)
        if r.status_code == 401:
            self._refresh()
            r = request(method, url, headers=self.headers, **kwargs)
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"Spotify returned {r.status_code}")
        return r

    def get(self, path_or_url, **kwargs):
        url = path_or_url if path_or_url.startswith("http") else SPOTIFY_API + path_or_url
        return self._call("GET", url, **kwargs)

    def playlist_contents(self):
        """Return (set of track IDs, set of artist|title keys) already in the playlist."""
        ids, keys = set(), set()
        url = f"/playlists/{self.playlist_id}/items"
        params = {"limit": 50}
        while url:
            r = self.get(url, params=params)
            if r.status_code != 200:
                sys.exit(
                    f"Couldn't read the playlist ({r.status_code}): {r.text[:300]}\n"
                    "Check SPOTIFY_PLAYLIST_ID and that you own (or collaborate on) the playlist."
                )
            data = r.json()
            for entry in data.get("items", []):
                # Spotify's Feb 2026 API renamed 'track' to 'item'; accept both.
                t = entry.get("item") or entry.get("track")
                if not t or t.get("type", "track") != "track" or not t.get("id"):
                    continue
                ids.add(t["id"])
                linked = (t.get("linked_from") or {}).get("id")
                if linked:
                    ids.add(linked)
                artists = t.get("artists") or [{}]
                keys.add(track_key(t.get("name", ""), artists[0].get("name", "")))
            url, params = data.get("next"), None
        return ids, keys

    def track_by_id(self, track_id):
        r = self.get(f"/tracks/{track_id}")
        return r.json() if r.status_code == 200 else None

    def search(self, artist, title):
        queries = []
        if artist:
            queries.append(f'track:"{title}" artist:"{artist}"')
        queries.append(f"{artist} {title}".strip())
        for q in queries:
            r = self.get("/search", params={"q": q, "type": "track", "limit": 10})
            if r.status_code != 200:
                continue
            for t in r.json().get("tracks", {}).get("items", []):
                if title_matches(title, t.get("name", "")):
                    return t
        return None

    def add(self, uri):
        if DRY_RUN:
            return
        r = self._call(
            "POST",
            f"{SPOTIFY_API}/playlists/{self.playlist_id}/items",
            json={"uris": [uri]},
        )
        if r.status_code not in (200, 201):
            sys.exit(
                f"Adding to the playlist failed ({r.status_code}): {r.text[:300]}\n"
                "If this is 403, re-run get_refresh_token.py so the token has playlist-modify scopes."
            )


def title_matches(wanted, candidate):
    """Loose check so a bad search hit isn't added: most words in the title must line up."""
    a, b = set(tokens(wanted)), set(tokens(candidate))
    if not a or not b:
        return False
    overlap = len(a & b)
    return overlap / len(a) >= 0.6 and overlap / len(b) >= 0.5


# --------------------------------------------------------------------------- main flow


def resolve_spotify_id(url):
    """Spotify track ID from a direct or shortened Spotify link, else None."""
    m = SPOTIFY_TRACK_RE.search(url)
    if m:
        return m.group(1)
    if host_of(url) in SPOTIFY_SHORT_HOSTS:
        try:
            r = request(
                "GET", url, headers={"User-Agent": BROWSER_UA}, allow_redirects=True
            )
        except requests.RequestException:
            return None
        m = SPOTIFY_TRACK_RE.search(r.url) or SPOTIFY_TRACK_RE.search(r.text or "")
        if m:
            return m.group(1)
    return None


def handle_url(url, sp, have_ids, have_keys):
    """Process one link. Returns ADDED, DUPLICATE, NOT_FOUND, or None (not a music link)."""
    host = host_of(url)
    track = None

    track_id = resolve_spotify_id(url)
    if track_id:
        if track_id in have_ids:
            print(f"  duplicate (same Spotify ID): {track_id}")
            return DUPLICATE
        track = sp.track_by_id(track_id)
        if not track:
            print(f"  couldn't load Spotify track {track_id}")
            return NOT_FOUND
    elif host_matches(host, MUSIC_HOSTS):
        meta = fetch_metadata(url)
        if not meta:
            print(f"  couldn't read a title from {host}")
            return NOT_FOUND
        artist, title = meta
        track = sp.search(artist, title)
        if not track:
            print(f"  no Spotify match for: {artist} - {title}")
            return NOT_FOUND
    else:
        return None

    artists = track.get("artists") or [{}]
    first_artist = artists[0].get("name", "")
    label = f"{first_artist} - {track.get('name', '')}"
    key = track_key(track.get("name", ""), first_artist)

    if track["id"] in have_ids or key in have_keys:
        print(f"  duplicate, skipped: {label}")
        return DUPLICATE

    sp.add(track["uri"])
    have_ids.add(track["id"])
    have_keys.add(key)
    print(f"  {'would add' if DRY_RUN else 'added'}: {label}")
    return ADDED


def main():
    discord = Discord(need("DISCORD_BOT_TOKEN"), need("DISCORD_CHANNEL_ID"))
    sp = Spotify(
        need("SPOTIFY_CLIENT_ID"),
        need("SPOTIFY_CLIENT_SECRET"),
        need("SPOTIFY_REFRESH_TOKEN"),
        need("SPOTIFY_PLAYLIST_ID"),
    )

    since = parse_since(os.environ.get("BACKFILL_SINCE"))
    if since:
        print(f"Backfill mode: reading the channel's history back to {since.date()} (UTC).")
    messages = discord.messages(since)
    humans = [m for m in messages if not m.get("author", {}).get("bot")]
    if humans and not any(m.get("content") for m in humans):
        print(
            "WARNING: messages came back with empty text. Enable the 'Message Content "
            "Intent' for the bot in the Discord Developer Portal."
        )

    try:
        have_ids, have_keys = sp.playlist_contents()
    except TransientError as e:
        sys.exit(f"Spotify is busy right now ({e}). Nothing was changed; try again later.")
    print(f"Playlist has {len(have_ids)} tracks. Scanning {len(messages)} messages."
          + (" (dry run)" if DRY_RUN else ""))

    counts = {ADDED: 0, DUPLICATE: 0, NOT_FOUND: 0}
    react_failed = False
    transient_errors = 0
    for msg in messages:
        if discord.is_marked(msg):
            continue
        urls = extract_urls(msg.get("content", ""))
        if not urls:
            continue
        print(f"Message {msg['id']}:")
        try:
            results = [handle_url(u, sp, have_ids, have_keys) for u in urls]
        except TransientError as e:
            # Leave the message unmarked so the next run retries it.
            transient_errors += 1
            print(f"  Spotify hiccup ({e}); will retry this message next run.")
            if transient_errors >= 3:
                print("Spotify keeps rate-limiting. Stopping early. Progress so far is saved "
                      "(handled messages carry a reaction). Re-run later to continue.")
                break
            continue
        results = [r for r in results if r]
        if not results:
            continue
        outcome = next((e for e in (ADDED, DUPLICATE, NOT_FOUND) if e in results), None)
        counts[outcome] += 1
        if outcome and not discord.react(msg["id"], outcome) and not react_failed:
            react_failed = True
            print(
                "WARNING: couldn't add a reaction. Give the bot the 'Add Reactions' "
                "permission. (Duplicates are still skipped via the playlist check.)"
            )

    print(
        f"\nDone{' (dry run)' if DRY_RUN else ''}: "
        f"{counts[ADDED]} messages added songs, {counts[DUPLICATE]} were duplicates, "
        f"{counts[NOT_FOUND]} had no confident match."
    )


if __name__ == "__main__":
    main()
