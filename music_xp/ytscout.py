"""Scout fresh releases straight from YouTube Music's own catalog.

Complements the iTunes storefront scout: for each language's top seed artists,
pull their newest singles/albums from YT Music (`get_artist`). This catches drops
the storefront search misses — especially for languages with thin Apple coverage.

Freshness caveat: YT Music only exposes a release *year*, not a date, and that
year belongs to the release entry — a 2026 re-issue of a 1960s record reads as
2026. So the year is only a pre-filter: a track ships only once iTunes or
MusicBrainz confirms a day-precise date inside the window. YT-only releases that
neither service indexes are dropped, which costs some genuine finds.
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import itunes, musicbrainz, taste
from .config import DATA_DIR
from .lastfm import LastFM
from .scout import _genres_for, is_live
from .ytdata import clean_artist_name

_CHANNEL_CACHE = DATA_DIR / "yt_channel_ids.json"

# YT Music lists short "teaser" uploads (~15-40s) alongside real releases,
# and they're often a single's first track. Skip anything this short so the
# player gets the full song, not a snippet.
_MIN_TRACK_SECONDS = 60

# Version/remix noise that differs between YT and iTunes titles for the same song.
_NOISE = {"feat", "featuring", "with", "remix", "mixed", "mix", "extended", "edit",
          "version", "ver", "remaster", "remastered", "instrumental", "intro",
          "prod", "the", "a"}


def _norm(text: str) -> str:
    """Latin-alphanumeric skeleton of a title/artist, minus version noise."""
    words = re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split()
    return " ".join(w for w in words if w not in _NOISE).strip()


def _title_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and (a in b or b in a)


def _artist_match(seed: str, found: str) -> bool:
    if not seed or not found:
        return False
    return seed == found or seed in found or found in seed


def load_channel_cache() -> dict:
    if _CHANNEL_CACHE.exists():
        try:
            return json.loads(_CHANNEL_CACHE.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_channel_cache(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = _CHANNEL_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    tmp.replace(_CHANNEL_CACHE)


def _channel_id(yt, artist: str, cache: dict) -> str | None:
    """Resolve an artist name to a YT Music channelId, cached (incl. misses)."""
    key = artist.lower().strip()
    if key in cache:
        return cache[key]
    cid = None
    try:
        res = yt.search(artist, filter="artists", limit=1)
        if res:
            cid = res[0].get("browseId")
    except Exception:
        cid = None
    cache[key] = cid
    return cid


def _first_track(yt, browse_id: str) -> dict | None:
    """Lead full-length track (title + videoId) of an album/single browseId.

    Skips teaser clips shorter than _MIN_TRACK_SECONDS; a track missing a
    duration is kept (unknown, don't over-filter).
    """
    try:
        alb = yt.get_album(browse_id)
    except Exception:
        return None
    for track in (alb.get("tracks") or []):
        secs = track.get("duration_seconds")
        if secs is not None and secs < _MIN_TRACK_SECONDS:
            continue
        return track
    return None


def _official_audio_id(yt, artist: str, title: str, fallback: str) -> str:
    """Resolve the official *audio* (Topic/ATV) videoId for a track.

    Album lookups sometimes return the music-video upload (videoType OMV/UGC) —
    a video re-encoded to audio: lower quality and a video thumbnail. The songs
    search exposes the ATV audio track, so prefer that when title + artist match
    and it clears the teaser-length guard. Falls back to `fallback` otherwise.
    """
    nt, na = _norm(title), _norm(artist)
    try:
        results = yt.search(f"{artist} {title}", filter="songs", limit=5)
    except Exception:
        return fallback
    for s in results:
        if s.get("videoType") != "MUSIC_VIDEO_TYPE_ATV":
            continue
        if (s.get("duration_seconds") or 0) < _MIN_TRACK_SECONDS:
            continue
        arts = s.get("artists") or []
        s_artist = arts[0].get("name", "") if arts else ""
        if _title_match(nt, _norm(s.get("title", ""))) and \
           _artist_match(na, _norm(s_artist)):
            vid = s.get("videoId")
            if vid:
                return vid
    return fallback


def _itunes_release_date(artist: str, title: str, storefront: str) -> str | None:
    """Earliest release date iTunes has for this track, or None if not indexed.

    Searches by artist+title term (not artistTerm) so a brand-new single is found
    directly rather than buried under the artist's popular back catalogue. Titles
    and artists are normalized (remix/feat/version noise stripped) so collab
    credits and localized titles still match — and we take the *earliest* date so
    a fresh-looking remix of an old song is correctly judged stale.
    """
    nt, na = _norm(title), _norm(artist)
    dates: list[str] = []
    for song in itunes.search_songs(f"{artist} {title}", storefront, limit=25,
                                    by_artist=False):
        if _title_match(nt, _norm(song["title"])) and \
           _artist_match(na, _norm(song["artist"])):
            rd = song.get("release_date")
            if rd:
                dates.append(rd)
    return min(dates) if dates else None


def _musicbrainz_release_date(artist: str, title: str) -> str | None:
    """Earliest *full-precision* MusicBrainz date for a track, else None.

    Used only when iTunes doesn't index the track. MB dates can be year- or
    month-only; those add nothing over YT's own year, so we accept a hit only
    when it carries a full YYYY-MM-DD we can actually window on.
    """
    nt, na = _norm(title), _norm(artist)
    dates: list[str] = []
    for rec in musicbrainz.search_recordings(artist, title):
        d = rec.get("date", "")
        if len(d) < 10:  # "", "YYYY", or "YYYY-MM" — not day-precise
            continue
        if _title_match(nt, _norm(rec["title"])) and \
           _artist_match(na, _norm(rec["artist"])):
            dates.append(d[:10])
    return min(dates) if dates else None


def scout_youtube_language(
    lang: dict,
    model: dict,
    yt,
    lf: LastFM,
    window_days: int,
    seen_video_ids: set[str],
    channel_cache: dict,
    max_seed_artists: int = 15,
    per_artist: int = 2,
) -> list[dict]:
    """Candidate tracks for one language, sourced from YT Music artist pages."""
    language = lang["name"]
    storefronts = [m.lower() for m in lang.get("markets", [])] or ["us"]
    primary = storefronts[0]
    this_year = str(date.today().year)

    seed_artists = taste.seed_artists(model, language, max_seed_artists)

    candidates: list[dict] = []
    seen_key: set[tuple[str, str]] = set()

    for artist in seed_artists:
        cid = _channel_id(yt, artist, channel_cache)
        if not cid:
            continue
        try:
            info = yt.get_artist(cid)
        except Exception:
            continue

        # Newest-first singles + albums released this calendar year.
        recent = []
        for section in ("singles", "albums"):
            sec = info.get(section) or {}
            for r in (sec.get("results") or []):
                if r.get("browseId") and str(r.get("year") or "") == this_year:
                    recent.append(r)

        added = 0
        for rel in recent:
            if added >= per_artist:
                break
            track = _first_track(yt, rel["browseId"])
            if not track:
                continue
            vid, title = track.get("videoId"), track.get("title")
            if not vid or not title:
                continue
            # Skip live/stage recordings — only the official studio song.
            if is_live(title, rel.get("title", "")):
                continue
            if track.get("videoType") != "MUSIC_VIDEO_TYPE_ATV":
                vid = _official_audio_id(yt, artist, title, vid)
            if vid in seen_video_ids:
                continue
            key = (artist.lower(), title.lower())
            if key in seen_key:
                continue

            # A day-precise date is required. YT Music's `year` is the year of
            # the *release entry*, so re-issues and compilations of old catalogue
            # carry the current year and would otherwise pass as fresh.
            rd = _itunes_release_date(artist, title, primary)
            if rd is None:
                rd = _musicbrainz_release_date(artist, title)
            if rd is None or not itunes.within_window(rd, window_days):
                continue

            # Full credit list (features included) for display/tags; the seed
            # `artist` stays the matching/genre key.
            names = [clean_artist_name(a.get("name"))
                     for a in (track.get("artists") or []) if a.get("name")]
            names = [n for n in names if n]

            seen_key.add(key)
            seen_video_ids.add(vid)
            candidates.append({
                "title": title,
                "artist": artist,
                "artist_display": ", ".join(names) if names else artist,
                "language": language,
                "storefront": primary,
                "genres": _genres_for(artist, lf, ""),
                "release_date": rd,
                "similar_seed": False,
                "video_id": vid,
                "source": "youtube",
            })
            added += 1

    return candidates
