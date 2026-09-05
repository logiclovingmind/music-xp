"""YouTube Data API v3 client for the operations that need your account.

Why this exists: YouTube Music's internal API rejects OAuth tokens for playlist
creation (only its own TV client is accepted, and TVs can't create playlists),
and browser-cookie auth is unreliable. The official Data API v3, however, cleanly
creates playlists and adds videos with our OAuth token — and playlists made this
way show up in YouTube Music. It also reads your Liked Music (playlist "LM") for
the feedback loop.

Reads that don't need your account (search / track resolution) still go through
unauthenticated ytmusicapi — see ytmusic.py.
"""
from __future__ import annotations

import json
import time

import requests

from . import notify
from .config import ROOT, load_config

OAUTH_FILE = ROOT / "oauth.json"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
LIKED_MUSIC_PLAYLIST = "LM"  # YouTube Music "Liked songs" auto-playlist


def _is_invalid_grant(r: requests.Response) -> bool:
    try:
        return (r.json() or {}).get("error") == "invalid_grant"
    except ValueError:
        return False


class YouTubeData:
    def __init__(self) -> None:
        if not OAUTH_FILE.exists():
            raise SystemExit(
                "Not authenticated. Run the OAuth device flow to create oauth.json."
            )
        env = load_config()["_env"]
        self.client_id = env["ytm_oauth_client_id"]
        self.client_secret = env["ytm_oauth_client_secret"]
        if not self.client_id or not self.client_secret:
            raise SystemExit(
                "Missing YTM_OAUTH_CLIENT_ID / YTM_OAUTH_CLIENT_SECRET in .env"
            )
        self.tok = json.loads(OAUTH_FILE.read_text())
        self._ensure_fresh()

    # ── token management ──────────────────────────────────────────────────
    def _ensure_fresh(self) -> None:
        if self.tok.get("expires_at", 0) - time.time() < 120:
            self._refresh()

    def _refresh(self) -> None:
        r = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.tok["refresh_token"],
            "grant_type": "refresh_token",
        })
        # Google kills the refresh token every ~7 days while the Cloud consent
        # screen is in "Testing". That used to surface as a full run that found
        # everything and then died with a bare HTTP 400 and no playlist, so say
        # plainly what happened and how to fix it.
        if r.status_code == 400 and _is_invalid_grant(r):
            notify.notify("Music XP", "Google sign-in expired — nothing was published.",
                          subtitle="Run: python3 reauth_google.py")
            raise SystemExit(
                "\nGoogle auth expired (invalid_grant) — no playlist was created."
                "\nRe-authorise, then run this again:"
                "\n    .venv/bin/python3 reauth_google.py\n")
        r.raise_for_status()
        d = r.json()
        self.tok["access_token"] = d["access_token"]
        self.tok["expires_at"] = int(time.time()) + int(d.get("expires_in", 3600))
        OAUTH_FILE.write_text(json.dumps(self.tok, indent=1))

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": "Bearer " + self.tok["access_token"],
            "Content-Type": "application/json",
        }

    # ── writes ────────────────────────────────────────────────────────────
    def create_playlist(self, title: str, description: str, privacy: str) -> str:
        body = {
            "snippet": {"title": title, "description": description},
            "status": {"privacyStatus": privacy.lower()},
        }
        r = requests.post(f"{API}/playlists?part=snippet,status",
                          headers=self._headers, json=body)
        r.raise_for_status()
        return r.json()["id"]

    def add_video(self, playlist_id: str, video_id: str) -> bool:
        body = {"snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }}
        r = requests.post(f"{API}/playlistItems?part=snippet",
                          headers=self._headers, json=body)
        return r.status_code == 200

    def rate_video(self, video_id: str, rating: str) -> bool:
        """Rate a video on your account: 'like', 'dislike', or 'none'."""
        r = requests.post(f"{API}/videos/rate", headers=self._headers,
                          params={"id": video_id, "rating": rating})
        return r.status_code == 204

    def create_with_videos(
        self, title: str, description: str, video_ids: list[str], privacy: str
    ) -> tuple[str, int]:
        """Create a playlist and append videos in order. Returns (id, n_added)."""
        pid = self.create_playlist(title, description, privacy)
        added = sum(1 for v in video_ids if self.add_video(pid, v))
        return pid, added

    # ── reads (account) ───────────────────────────────────────────────────
    def liked_music_tracks(self, limit: int = 300) -> list[dict]:
        """Your Liked songs as {video_id, title, artist, liked_at}, newest first.

        `liked_at` is when the track entered the playlist, not when it was
        released — it is what lets a like you gave outside Music XP be recorded
        on the day you actually gave it.
        """
        out: list[dict] = []
        for it in self._paginate(f"{API}/playlistItems",
                                 {"part": "snippet",
                                  "playlistId": LIKED_MUSIC_PLAYLIST}, limit):
            sn = it.get("snippet", {})
            vid = sn.get("resourceId", {}).get("videoId", "")
            title = sn.get("title", "")
            if not vid or title in ("Deleted video", "Private video"):
                continue
            artist = artist_from_item(it)
            # Uploads outside the Topic system title themselves "Artist - Song".
            if " - " in title and title.split(" - ", 1)[0].strip() == artist:
                title = title.split(" - ", 1)[1].strip()
            out.append({"video_id": vid, "title": title, "artist": artist,
                        "liked_at": (sn.get("publishedAt") or "")[:10]})
        return out

    def liked_music_artists(self, limit: int = 1000) -> list[str]:
        return [artist_from_item(it) for it in self._paginate(
            f"{API}/playlistItems",
            {"part": "snippet", "playlistId": LIKED_MUSIC_PLAYLIST}, limit)]

    def subscription_artists(self, limit: int = 1000) -> list[str]:
        out = []
        for it in self._paginate(f"{API}/subscriptions",
                                 {"part": "snippet", "mine": "true"}, limit):
            title = it.get("snippet", {}).get("title", "")
            out.append(clean_artist_name(title))
        return out

    def my_playlists_artists(self, verbose: bool = False):
        """Yield (playlist_title, [artist, ...]) for each of your own playlists."""
        for pl in self._paginate(f"{API}/playlists",
                                 {"part": "snippet", "mine": "true"}, 200):
            pid = pl.get("id")
            title = pl.get("snippet", {}).get("title", "?")
            if not pid:
                continue
            artists = [artist_from_item(it) for it in self._paginate(
                f"{API}/playlistItems",
                {"part": "snippet", "playlistId": pid}, 500)]
            yield title, artists

    # ── reads (recommendations) ───────────────────────────────────────────
    # A playlist ID of RD<videoId> (or RDAMVM… / RDMM…) is a real, readable
    # playlist holding what YouTube recommends around that track. That makes the
    # recommender — the thing that already works on this account — reachable with
    # the token we hold, at 1 quota unit per call against 10,000/day.
    def my_playlists(self, limit: int = 50) -> list[dict]:
        r = requests.get(f"{API}/playlists", headers=self._headers,
                         params={"part": "snippet,contentDetails",
                                 "mine": "true", "maxResults": min(limit, 50)})
        if r.status_code != 200:
            return []
        return [{"id": i["id"], "title": i["snippet"]["title"],
                 "count": i["contentDetails"]["itemCount"]}
                for i in r.json().get("items", [])]

    def playlist_video_ids(self, playlist_id: str, limit: int = 50) -> list[str]:
        r = requests.get(f"{API}/playlistItems", headers=self._headers,
                         params={"part": "snippet", "playlistId": playlist_id,
                                 "maxResults": min(limit, 50)})
        if r.status_code != 200:
            return []
        out = []
        for i in r.json().get("items", []):
            vid = i.get("snippet", {}).get("resourceId", {}).get("videoId")
            if vid:
                out.append(vid)
        return out

    def radio(self, seed_video_id: str, prefix: str = "RD",
              limit: int = 50) -> list[str]:
        """Video IDs YouTube recommends around a seed track."""
        return self.playlist_video_ids(prefix + seed_video_id, limit)

    def videos_meta(self, video_ids: list[str]) -> list[dict]:
        """Metadata for up to 50 IDs per call — one quota unit per batch."""
        out: list[dict] = []
        for i in range(0, len(video_ids), 50):
            r = requests.get(f"{API}/videos", headers=self._headers,
                             params={"part": "snippet",
                                     "id": ",".join(video_ids[i:i + 50])})
            if r.status_code != 200:
                continue
            for it in r.json().get("items", []):
                s = it["snippet"]
                out.append({
                    "video_id": it["id"],
                    "raw_title": s.get("title", ""),
                    "channel": s.get("channelTitle", ""),
                    "published": (s.get("publishedAt") or "")[:10],
                    "category": s.get("categoryId", ""),
                    "audio_language": (s.get("defaultAudioLanguage")
                                       or s.get("defaultLanguage") or ""),
                })
        return out

    def _paginate(self, url: str, params: dict, limit: int):
        seen = 0
        page = None
        while seen < limit:
            q = {**params, "maxResults": 50}
            if page:
                q["pageToken"] = page
            r = requests.get(url, headers=self._headers, params=q)
            if r.status_code != 200:
                break
            j = r.json()
            for it in j.get("items", []):
                yield it
                seen += 1
            page = j.get("nextPageToken")
            if not page:
                break


def clean_artist_name(name: str) -> str:
    """Strip YouTube's channel bookkeeping off an artist name.

    '- Topic' marks the auto-generated channel the label's audio is delivered to,
    so it means the opposite of unofficial — but it is plumbing, not part of the
    name, and it must never reach a display label or a taste key.
    """
    name = (name or "").strip()
    for suffix in (" - Topic",):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    if name.endswith("VEVO") and len(name) > 4:
        name = name[:-4].strip()
    return name


def artist_from_item(item: dict) -> str:
    """Best-effort artist from a playlistItem: prefer the auto-generated
    'Artist - Topic' owner channel, else parse the 'Artist - Title' video title.
    """
    sn = item.get("snippet", {})
    owner = sn.get("videoOwnerChannelTitle", "")
    if owner:
        cleaned = clean_artist_name(owner)
        if cleaned:
            return cleaned
    title = sn.get("title", "")
    if " - " in title:
        return title.split(" - ", 1)[0].strip()
    return title.strip()
