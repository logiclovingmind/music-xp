"""Scout fresh releases, per language/storefront, and tag each candidate.

Free sources only (no Spotify — its Web API is Premium-gated):
  1. iTunes song search, seeded by your favourite + similar artists per storefront
     → taste-anchored real songs, with release dates for the freshness filter.
  2. Apple RSS most-played albums per storefront, filtered to fresh ones
     → serendipity / coverage beyond artists you already know.
Genres come from Last.fm (consistent English tags) so they match the taste model;
iTunes' localized genre is only a fallback.
"""
from __future__ import annotations

import re

from . import itunes, store, taste
from .lastfm import LastFM

# Live/stage recordings we don't want — we only want the official studio song.
# Kept deliberately narrow so real titles survive: "Live Forever", "Live Your
# Life", "Alive" etc. don't match. We only flag "live" when it's a version
# qualifier: bracketed/dashed ("(Live", "- Live"), or "Live at/in/from …".
_LIVE_RE = re.compile(
    r"[\(\[]\s*live\b"                       # "(Live"  "[Live"
    r"|[-–—]\s*live\b"                       # "- Live"
    r"|\blive\s+(?:at|in|from|on|@|session|sessions|version|performance|"
    r"recording|edit|acoustic|concert|tour)\b"
    r"|\bunplugged\b"
    r"|\bin\s+concert\b",
    re.IGNORECASE,
)


def is_live(*texts: str) -> bool:
    """True if any title/album text looks like a live/stage recording."""
    return any(_LIVE_RE.search(t or "") for t in texts)


def _genres_for(artist: str, lf: LastFM, fallback: str) -> list[str]:
    tags = lf.artist_tags(artist) if lf.enabled else []
    if tags:
        return tags
    return [fallback.lower()] if fallback else []


def _todays_seeds(ranked: list[str], language: str, budget: int,
                  always_top: int) -> list[str]:
    """Today's slice of a language's ranked artists, advancing a saved cursor.

    iTunes starts returning 403 long before the whole pool can be queried in one
    run, so coverage has to be spent over days instead of bought in one. The
    strongest names are re-checked every run — a new Charli xcx single should
    never wait — while everyone below them takes turns, so an artist at rank 109
    is reached within days rather than never.
    """
    head = ranked[:always_top]
    tail = ranked[always_top:]
    take = min(max(budget - len(head), 0), len(tail))
    if not take:
        return head

    cursors = store.load_seed_cursors()
    start = cursors.get(language, 0) % len(tail)
    slice_ = [tail[(start + i) % len(tail)] for i in range(take)]
    cursors[language] = (start + take) % len(tail)
    store.save_seed_cursors(cursors)
    return head + slice_


def scout_language(
    lang: dict,
    model: dict,
    lf: LastFM,
    window_days: int,
    daily_seed_budget: int = 30,
    always_top: int = 10,
    max_similar: int = 25,
) -> list[dict]:
    """Return candidate track dicts for one language."""
    language = lang["name"]
    storefronts = [m.lower() for m in lang.get("markets", [])] or ["us"]
    primary = storefronts[0]

    # How deep into your ranked artists to look. Going deeper matters — your
    # weights are a plateau, not a peak, so a shallow cut drops real favourites
    # at random — but iTunes 403s long before the whole list can be queried in
    # one run, so the depth is spent over days instead (see _todays_seeds).
    #
    # The pool is therefore bounded by how long a release stays fresh: an artist
    # reached on day 6 of the rotation has already aged out of a 5-day window, so
    # anyone beyond a full cycle's reach would be invisible no matter their rank.
    seed_pool = always_top + (daily_seed_budget - always_top) * max(window_days, 1)
    seed_artists = _todays_seeds(taste.seed_artists(model, language, seed_pool),
                                 language, daily_seed_budget, always_top)

    similar: list[str] = []
    similar_set: set[str] = set()
    for a in seed_artists[:15]:
        for s in lf.similar_artists(a, limit=8):
            if s.lower() not in similar_set:
                similar_set.add(s.lower())
                similar.append(s)
    similar = similar[:max_similar]

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(track: dict, is_similar: bool) -> None:
        if not itunes.within_window(track.get("release_date", ""), window_days):
            return
        if is_live(track.get("title", ""), track.get("album", "")):
            return
        key = (track["artist"].lower(), track["title"].lower())
        if key in seen:
            return
        seen.add(key)
        candidates.append({
            "title": track["title"],
            "artist": track["artist"],
            "language": language,
            "storefront": primary,
            "genres": _genres_for(track["artist"], lf, track.get("itunes_genre", "")),
            "release_date": track.get("release_date"),
            "similar_seed": is_similar,
        })

    # 1) Taste-anchored: your artists + similar artists.
    for artist in seed_artists:
        for t in itunes.search_songs(artist, primary, limit=25):
            add(t, is_similar=False)
    for artist in similar:
        for t in itunes.search_songs(artist, primary, limit=15):
            add(t, is_similar=True)

    # 2) Serendipity: fresh popular albums in this storefront.
    for album in itunes.recent_albums(primary, count=25):
        known = album["artist"].lower() in {a.lower() for a in seed_artists}
        add(album, is_similar=not known)

    return candidates
