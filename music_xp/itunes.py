"""Apple / iTunes as the free, no-signup new-releases source.

Two calls, both keyless:
  • iTunes Search API  — songs per storefront (country), with release dates + genre.
  • Apple RSS most-played — a serendipity feed of currently-popular albums per
    storefront, which we then filter down to genuinely fresh ones.

Storefront = country code = our language proxy (KR→korean, JP→japanese, …).
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import requests

SEARCH = "https://itunes.apple.com/search"
RSS = "https://rss.applemarketingtools.com/api/v2/{cc}/music/most-played/{n}/albums.json"
UA = {"User-Agent": "DailyFreshMusic/0.1 (personal use)"}

# Be polite to Apple's unauthenticated endpoints.
_MIN_INTERVAL = 0.4
_last_call = 0.0

# A throttled search returns HTTP 403 with an empty body, which is indistinguishable
# from "this artist has no songs" once it becomes []. Counting them is what stops a
# rate-limited run from reporting itself as a quiet release day.
throttled_calls = 0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _parse_day(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def within_window(release_date: str, window_days: int) -> bool:
    d = _parse_day(release_date)
    if d is None:
        return False
    return date.today() - timedelta(days=window_days) <= d <= date.today()


def search_songs(term: str, storefront: str, limit: int = 25,
                 by_artist: bool = True) -> list[dict]:
    """Return normalized song dicts for a search term in one storefront."""
    if not term:
        return []
    params = {
        "term": term,
        "country": storefront.lower(),
        "media": "music",
        "entity": "song",
        "limit": min(limit, 200),
    }
    if by_artist:
        params["attribute"] = "artistTerm"
    global throttled_calls
    _throttle()
    try:
        r = requests.get(SEARCH, params=params, headers=UA, timeout=20)
        if r.status_code != 200:
            if r.status_code in (403, 429):
                throttled_calls += 1
            return []
        results = r.json().get("results", [])
    except Exception:
        return []

    out = []
    for x in results:
        title = x.get("trackName")
        artist = x.get("artistName")
        if not title or not artist:
            continue
        out.append({
            "title": title,
            "artist": artist,
            "album": x.get("collectionName", ""),
            "release_date": (x.get("releaseDate") or "")[:10],
            "itunes_genre": x.get("primaryGenreName", ""),
        })
    return out


def recent_albums(storefront: str, count: int = 25) -> list[dict]:
    """Popular albums per storefront (RSS) — freshness filtered by caller."""
    _throttle()
    try:
        r = requests.get(RSS.format(cc=storefront.lower(), n=count),
                         headers=UA, timeout=20)
        if r.status_code != 200:
            return []
        results = r.json().get("feed", {}).get("results", [])
    except Exception:
        return []

    out = []
    for a in results:
        name = a.get("name")
        artist = a.get("artistName")
        if not name or not artist:
            continue
        out.append({
            "title": name,           # album name; resolved to its top song on YTM
            "artist": artist,
            "release_date": (a.get("releaseDate") or "")[:10],
            "itunes_genre": (a.get("genres") or [{}])[0].get("name", "")
                            if a.get("genres") else "",
            "is_album": True,
        })
    return out
