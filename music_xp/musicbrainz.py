"""MusicBrainz as a fallback release-date oracle for YT-only artists.

YT Music exposes only a release *year*. iTunes gives exact dates, but only for
artists it indexes. For everyone else we'd fall back to the coarse "this calendar
year" guess. MusicBrainz — a free, open catalogue — often has an exact
`first-release-date`, letting us apply the real freshness window instead.

MusicBrainz asks two things of unauthenticated callers:
  • a descriptive User-Agent that includes a contact address, and
  • no more than ~1 request/second.
Both are honoured below. Matching is left to the caller (which already has the
title/artist normalizers); we just return raw hits.
"""
from __future__ import annotations

import time

import requests

RECORDING = "https://musicbrainz.org/ws/2/recording"
ARTIST = "https://musicbrainz.org/ws/2/artist"
# Contact per MB policy; zEi's address so they can reach a human if needed.
UA = {"User-Agent": "DailyFreshMusic/0.1 (personal use; logiclovingmind@gmail.com)"}

_MIN_INTERVAL = 1.1  # MB requires <=1 req/sec sustained; stay just above.
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _lucene_escape(text: str) -> str:
    """Quote/escape a term for a MusicBrainz Lucene query value."""
    return (text or "").replace("\\", "").replace('"', "").strip()


def mbid_for_artist(artist: str) -> str:
    """Best-guess MusicBrainz artist ID ("" if none), for image lookups.

    Returns the top hit's id only when MusicBrainz is confident (score >= 90),
    so a fuzzy match can't send an image lookup off to the wrong artist.
    """
    a = _lucene_escape(artist)
    if not a:
        return ""
    _throttle()
    try:
        r = requests.get(ARTIST, params={"query": f'artist:"{a}"',
                                         "fmt": "json", "limit": 1},
                         headers=UA, timeout=20)
        if r.status_code != 200:
            return ""
        arts = r.json().get("artists", [])
    except Exception:
        return ""
    if arts and arts[0].get("score", 0) >= 90:
        return arts[0].get("id", "")
    return ""


def search_recordings(artist: str, title: str, limit: int = 15) -> list[dict]:
    """Return recording hits as {artist, title, date} for an artist+title query.

    `date` is MusicBrainz's raw first-release-date: it may be "", "YYYY",
    "YYYY-MM", or a full "YYYY-MM-DD". The caller decides what precision it needs.
    """
    a, t = _lucene_escape(artist), _lucene_escape(title)
    if not a or not t:
        return []
    params = {
        "query": f'artist:"{a}" AND recording:"{t}"',
        "fmt": "json",
        "limit": min(limit, 100),
    }
    _throttle()
    try:
        r = requests.get(RECORDING, params=params, headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        recs = r.json().get("recordings", [])
    except Exception:
        return []

    out: list[dict] = []
    for rec in recs:
        credits = rec.get("artist-credit") or []
        names = [c.get("name") or (c.get("artist") or {}).get("name", "")
                 for c in credits if isinstance(c, dict)]
        primary = names[0] if names else ""
        out.append({
            "artist": primary,
            "title": rec.get("title", ""),
            "date": rec.get("first-release-date", "") or "",
        })
    return out
