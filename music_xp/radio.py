"""Scout fresh releases by expanding YouTube's own radio around your own picks.

This replaces the per-artist catalogue walk. That design asked iTunes "what did
each of my 4,042 artists release?" — one HTTP call per artist per storefront —
and Apple now answers with HTTP 403 from roughly the 68th call onward, so a run
that needed ~1,300 calls was throwing nearly all of them away and reporting the
result as a quiet release day.

Radio inverts it. A playlist ID of `RD<videoId>` holds what YouTube recommends
around that track, 50 at a time for a single quota unit, and the recommender
already knows this account. Languages stop being something to configure: one
English seed returns German, Spanish, Serbian, Arabic and French on its own.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import parse
from .lastfm import LastFM
from .ytdata import LIKED_MUSIC_PLAYLIST, YouTubeData

MUSIC_CATEGORY = "10"
_TRAILING_NUM = re.compile(r"(\d+)\s*$")


def _series_rank(title: str) -> int:
    """BathX169 sorts above BathX168. Unnumbered lists sort last."""
    m = _TRAILING_NUM.search(title)
    return int(m.group(1)) if m else -1


def collect_seeds(ytd: YouTubeData, cfg: dict) -> list[str]:
    """Seed video IDs: the tracks you chose yourself, most personal first.

    Never this project's own output. Seeding a recommender with its own picks is
    how a feed slowly stops sounding like you.
    """
    want = int(cfg.get("seed_count", 40))
    prefixes = tuple(p.lower() for p in cfg.get("seed_playlist_prefixes", []))

    seeds: list[str] = []
    seen: set[str] = set()

    def add(ids: list[str]) -> None:
        for v in ids:
            if v not in seen:
                seen.add(v)
                seeds.append(v)

    if cfg.get("seed_from_liked", True):
        add(ytd.playlist_video_ids(LIKED_MUSIC_PLAYLIST, limit=25))

    if prefixes:
        mine = [p for p in ytd.my_playlists(limit=50)
                if p["title"].lower().startswith(prefixes)]
        mine.sort(key=lambda p: _series_rank(p["title"]), reverse=True)
        for pl in mine:
            if len(seeds) >= want:
                break
            add(ytd.playlist_video_ids(pl["id"], limit=50))

    return seeds[:want]


def gather_video_ids(ytd: YouTubeData, seeds: list[str], cfg: dict) -> list[str]:
    """Every distinct video the radios recommend, across all surfaces."""
    found: list[str] = []
    seen: set[str] = set(seeds)
    for seed in seeds:
        for prefix in cfg.get("radio_prefixes", ["RD"]):
            for vid in ytd.radio(seed, prefix, limit=50):
                if vid not in seen:
                    seen.add(vid)
                    found.append(vid)
    return found


def _fresh(published: str, window_days: int) -> bool:
    try:
        d = date.fromisoformat(published)
    except ValueError:
        return False
    return date.today() - timedelta(days=window_days) <= d <= date.today()


def build_candidates(ytd: YouTubeData, video_ids: list[str], cfg: dict,
                     lf: LastFM, seen_ids: set[str],
                     seen_pairs: set[tuple[str, str]],
                     disliked: set[str]) -> tuple[list[dict], dict]:
    """Fetch metadata, filter to fresh unseen music, and tag with genres."""
    window = int(cfg.get("release_window_days", 5))
    stats = {"stale": 0, "not_music": 0, "live": 0, "already_seen": 0,
             "disliked": 0, "unnamed": 0, "kept": 0}

    stats["already_seen"] = sum(1 for v in video_ids if v in seen_ids)
    stats["disliked"] = sum(1 for v in video_ids if v in disliked)
    wanted = [v for v in video_ids if v not in seen_ids and v not in disliked]

    candidates: list[dict] = []
    kept_pairs: set[tuple[str, str]] = set()

    for meta in ytd.videos_meta(wanted):
        if meta["category"] != MUSIC_CATEGORY:
            stats["not_music"] += 1
            continue
        if not _fresh(meta["published"], window):
            stats["stale"] += 1
            continue
        if parse.is_live(meta["raw_title"]):
            stats["live"] += 1
            continue

        artist, title = parse.split_artist_title(meta["raw_title"],
                                                 meta["channel"])
        if not artist:
            stats["unnamed"] += 1
            continue
        key = (artist.lower(), title.lower())
        if key in seen_pairs or key in kept_pairs:
            stats["already_seen"] += 1
            continue
        kept_pairs.add(key)

        candidates.append({
            "video_id": meta["video_id"],
            "artist": artist,
            "title": title,
            # Left as None when YouTube doesn't declare one. A placeholder would
            # mint a junk per-language table in the taste model.
            "language": parse.language_of(meta["audio_language"]),
            "release_date": meta["published"],
            "genres": lf.artist_tags(artist) if lf.enabled else [],
            "similar_seed": False,
        })
        stats["kept"] += 1

    return candidates, stats
