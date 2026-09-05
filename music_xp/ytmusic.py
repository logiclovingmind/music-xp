"""YouTube access, split by what needs your account.

- Search / track resolution: unauthenticated ytmusicapi. No login needed and it
  parses YouTube Music's web responses correctly.
- Playlist creation + reading your Liked Music: the official YouTube Data API v3
  (see ytdata.py). YouTube Music's internal API rejects OAuth tokens for writes,
  so the Data API is the reliable path — and its playlists appear in YT Music.
"""
from __future__ import annotations

from ytmusicapi import YTMusic

from .config import ROOT
from .ytdata import YouTubeData, clean_artist_name

OAUTH_FILE = ROOT / "oauth.json"


def client() -> YTMusic:
    """Unauthenticated YT Music client — used for search / track resolution."""
    return YTMusic()


def data_client() -> YouTubeData:
    """Authenticated YouTube Data API client — used for reads/writes on your account."""
    return YouTubeData()


def liked_tracks(ytd: YouTubeData, limit: int = 500) -> list[dict]:
    """Your Liked Music — the automatic feedback signal, and the whole of it.

    Empty on any failure rather than raising: a day without feedback costs one
    day of learning, where letting the read take the process down would cost the
    playlist too. An expired sign-in still stops the run, since that needs you.
    """
    try:
        return ytd.liked_music_tracks(limit=limit)
    except Exception:
        return []


ATV = "MUSIC_VIDEO_TYPE_ATV"  # audio-only album track, no music video


def search_track(yt: YTMusic, title: str, artist: str) -> str | None:
    """Resolve a title+artist to a YouTube Music songs videoId (audio only)."""
    query = f"{artist} {title}".strip()
    try:
        results = yt.search(query, filter="songs", limit=5)
    except Exception:
        return None
    # Prefer the pure album-audio version (ATV) over any music-video result.
    for r in results:
        if r.get("videoId") and r.get("videoType") == ATV:
            return r["videoId"]
    for r in results:
        if r.get("videoId"):
            return r["videoId"]
    return None


def track_credits(yt: YTMusic, video_id: str, title: str,
                  artist: str = "") -> list[str]:
    """All credited artist names for a track (e.g. ['Shakira', 'Anitta']).

    YT Music's songs search lists every performer, so a collab isn't reduced to
    just the lead artist. Prefers the row whose videoId matches; else the first
    row whose title matches. Best-effort — returns [] if nothing lines up.
    """
    import re

    def norm(s: str) -> str:
        return re.sub(r"[^0-9a-z]+", "", (s or "").lower())

    def name_norm(s: str) -> str:
        return " ".join(re.sub(r"[^0-9a-z]+", " ", (s or "").lower()).split())

    want = norm(title)
    seed = name_norm(artist)
    try:
        results = yt.search(f"{artist} {title}".strip(), filter="songs", limit=5)
    except Exception:
        return []
    fallback: list[str] = []
    for r in results:
        names = [clean_artist_name(a.get("name"))
                 for a in (r.get("artists") or []) if a.get("name")]
        names = [n for n in names if n]
        if not names:
            continue
        # Exact videoId is authoritative; take the full credit as-is.
        if r.get("videoId") == video_id:
            return names
        # Title-only match can point at a *different* artist's same-named song
        # (e.g. "giulia" vs "GIULIA BE"), so only trust it when one credited name
        # equals the seed artist exactly — a shared token isn't enough.
        got = norm(r.get("title", ""))
        if not fallback and want and (want in got or got in want) and seed \
                and seed in {name_norm(n) for n in names}:
            fallback = names
    return fallback


def audio_version(yt: YTMusic, video_id: str, title: str, artist: str) -> str:
    """Swap a music-video id for its audio-only (ATV) counterpart when one exists.

    YT Music albums sometimes list the official video as the lead track; its
    stream starts with video intro/outro sound and its thumbnail is a video
    frame. The ATV 'Song' version is the clean studio audio with album art.
    """
    try:
        vtype = yt.get_song(video_id)["videoDetails"].get("musicVideoType")
    except Exception:
        return video_id
    if vtype == ATV:
        return video_id

    def norm(s: str) -> str:
        import re
        return re.sub(r"[^0-9a-z]+", "", (s or "").lower())

    want = norm(title)
    try:
        results = yt.search(f"{artist} {title}".strip(), filter="songs", limit=5)
    except Exception:
        return video_id
    for r in results:
        got = norm(r.get("title", ""))
        if r.get("videoId") and r.get("videoType") == ATV and want and \
                (want in got or got in want):
            return r["videoId"]
    return video_id


def create_daily_playlist(
    ytd: YouTubeData, name: str, description: str, video_ids: list[str], privacy: str
) -> tuple[str, int]:
    """Create the playlist and add the videos. Returns (playlist_id, n_added)."""
    return ytd.create_with_videos(name, description, video_ids, privacy)


def taste_artist_counts(ytd: YouTubeData, verbose: bool = False) -> dict[str, float]:
    """Weighted taste signal by artist from your account, via the Data API:
    followed channels (artists) > Liked Music > your playlists.
    """
    counts: dict[str, float] = {}

    def bump(name: str, weight: float) -> bool:
        name = (name or "").strip()
        if not name or name.lower() in ("", "song", "video", "various artists"):
            return False
        counts[name] = counts.get(name, 0.0) + weight
        return True

    def log(src: str, n: int) -> None:
        if verbose:
            print(f"    · {src}: {n} artist-credits")

    # Followed artists = channel subscriptions (strongest explicit signal).
    try:
        n = sum(1 for a in ytd.subscription_artists() if bump(a, 4.0))
        log("followed artists", n)
    except Exception:
        pass

    # Liked Music — strong intentional signal.
    try:
        n = sum(1 for a in ytd.liked_music_artists() if bump(a, 3.0))
        log("liked music", n)
    except Exception:
        pass

    # Every saved / created playlist and its tracks.
    try:
        for title, artists in ytd.my_playlists_artists(verbose=verbose):
            n = sum(1 for a in artists if bump(a, 2.0))
            log(f"playlist '{title}'", n)
    except Exception:
        pass

    return counts
