"""Bandcamp new-arrivals as a serendipity source — best-effort, fragile.

Bandcamp has NO official public API. This talks to the same private endpoint the
Bandcamp discover page uses (`/api/discover/3/get_web`), whose required parameter
set is undocumented and has changed before (it currently needs the terse keys
p/g/gn/f/t/r/w/s). Treat this as disposable: every call is wrapped so that any
shape change, network error, or empty response yields [] and the daily run
continues on its other sources. If Bandcamp stops surfacing here, that's expected
— don't let it break anything.

Bandcamp's catalogue is overwhelmingly Western/indie and organized by genre, not
language, so the daily run only feeds it the English profile's liked genres.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from email.utils import parsedate_to_datetime

import requests

DISCOVER = "https://bandcamp.com/api/discover/3/get_web"
UA = {"User-Agent": "Mozilla/5.0 (DailyFreshMusic personal use)",
      "Content-Type": "application/json"}

_MIN_INTERVAL = 0.5
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = _MIN_INTERVAL - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


def _publish_day(value: str) -> date | None:
    """Parse Bandcamp's RFC-2822 publish_date ('22 Jul 2026 20:24:04 GMT')."""
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _genre_tokens(text: str) -> set[str]:
    import re
    return {t for t in re.split(r"[^0-9a-z]+", (text or "").lower()) if t}


def new_arrivals(window_days: int, want_genres: list[str] | None = None,
                 limit: int = 48) -> list[dict]:
    """Fresh Bandcamp arrivals as candidate dicts; [] on any failure.

    Pulls the all-genre 'new' slice, keeps items published within the freshness
    window, and — if `want_genres` is given — only those whose genre tag shares a
    token with your liked genres, so serendipity still lands near your taste.
    """
    body = {"p": 0, "g": 0, "gn": 0, "f": "all", "t": 0, "r": None,
            "w": 0, "s": "new"}
    _throttle()
    try:
        r = requests.post(DISCOVER, data=json.dumps(body), headers=UA, timeout=25)
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
    except Exception:
        return []

    want = {g.strip().lower() for g in (want_genres or []) if g.strip()}
    cutoff = date.today() - timedelta(days=window_days)
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        day = _publish_day(it.get("publish_date", ""))
        if day is None or day < cutoff or day > date.today():
            continue
        artist = (it.get("secondary_text") or "").strip()
        ft = it.get("featured_track") or {}
        title = (ft.get("title") if isinstance(ft, dict) else None) \
            or it.get("primary_text") or ""
        title = title.strip()
        # featured_track.title often carries a " - <artist>" suffix; drop it so
        # the bare track name resolves on YT Music.
        if artist and title.lower().endswith(f"- {artist.lower()}"):
            title = title[:-(len(artist) + 1)].rstrip(" -").strip()
        if not title or not artist:
            continue
        gtext = it.get("genre_text", "")
        if want and not (_genre_tokens(gtext) & want):
            continue
        out.append({
            "title": title.strip(),
            "artist": artist.strip(),
            "genres": [gtext.strip().lower()] if gtext else [],
            "release_date": day.isoformat(),
            "source": "bandcamp",
        })
        if len(out) >= limit:
            break
    return out
