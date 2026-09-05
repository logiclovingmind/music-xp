"""Last.fm API client (free) — expands your taste into similar artists and tags.

This is how discovery works without Spotify's (now-restricted) related-artists:
your seed artists -> similar artists -> we watch those for new releases.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

API = "https://ws.audioscrobbler.com/2.0/"


class LastFM:
    def __init__(self, api_key: str, cache_path: Path | None = None):
        self.enabled = bool(api_key)
        self._key = api_key
        self._cache_path = cache_path
        self._similar_cache: dict[str, list[str]] = {}
        self._tag_cache: dict[str, list[str]] = {}
        self._dirty = 0
        if cache_path and cache_path.exists():
            try:
                data = json.loads(cache_path.read_text())
                self._tag_cache = data.get("tags", {})
                self._similar_cache = data.get("similar", {})
            except (json.JSONDecodeError, ValueError):
                pass

    def flush(self) -> None:
        if not self._cache_path:
            return
        self._cache_path.write_text(json.dumps(
            {"tags": self._tag_cache, "similar": self._similar_cache},
            ensure_ascii=False))
        self._dirty = 0

    def _touch(self) -> None:
        self._dirty += 1
        if self._cache_path and self._dirty >= 25:
            self.flush()

    def _get(self, method: str, params: dict) -> dict:
        base = {"method": method, "api_key": self._key, "format": "json"}
        base.update(params)
        for _ in range(3):
            try:
                r = requests.get(API, params=base, timeout=15)
            except requests.RequestException:
                time.sleep(1)
                continue
            if r.status_code == 429:
                time.sleep(2)
                continue
            if r.status_code != 200:
                return {}
            try:
                return r.json()
            except ValueError:
                return {}
        return {}

    def similar_artists(self, artist: str, limit: int = 20) -> list[str]:
        if not self.enabled or not artist:
            return []
        key = artist.lower()
        if key in self._similar_cache:
            return self._similar_cache[key]
        data = self._get("artist.getsimilar", {"artist": artist, "limit": limit})
        items = data.get("similarartists", {}).get("artist", [])
        names = [it["name"] for it in items if it.get("name")]
        self._similar_cache[key] = names
        self._touch()
        return names

    def tag_top_artists(self, tag: str, limit: int = 30) -> list[str]:
        """Top artists for a genre tag — the engine for out-of-comfort-zone
        discovery: pick a genre you don't have, get its leading artists."""
        if not self.enabled or not tag:
            return []
        data = self._get("tag.gettopartists", {"tag": tag, "limit": limit})
        items = data.get("topartists", {}).get("artist", [])
        return [it["name"] for it in items if it.get("name")]

    def tag_top_tracks(self, tag: str, limit: int = 50) -> list[dict]:
        """Most-listened tracks carrying a tag, as {artist, title, rank}.

        Year tags ("2009") are the only free way to ask "what was big then" —
        Last.fm has no chart-by-year endpoint. They're user-applied and let
        strays through, so callers verify the release date elsewhere.
        """
        if not self.enabled or not tag:
            return []
        data = self._get("tag.gettoptracks", {"tag": tag, "limit": limit})
        items = data.get("tracks", {}).get("track", [])
        out = []
        for i, it in enumerate(items):
            name = it.get("name")
            artist = (it.get("artist") or {}).get("name", "")
            if not name or not artist:
                continue
            out.append({"title": name, "artist": artist, "rank": i})
        return out

    def artist_top_tracks(self, artist: str, limit: int = 10) -> list[dict]:
        """The artist's best-known songs, most listeners first.

        Picking from here instead of an arbitrary slice of their catalogue is
        what keeps discovery on signature songs rather than deep cuts.
        """
        if not self.enabled or not artist:
            return []
        data = self._get("artist.gettoptracks", {"artist": artist,
                                                 "limit": limit})
        items = data.get("toptracks", {}).get("track", [])
        out = []
        for it in items:
            name = it.get("name")
            if not name:
                continue
            try:
                listeners = int(it.get("listeners") or 0)
            except ValueError:
                listeners = 0
            out.append({"title": name, "listeners": listeners})
        return out

    def artist_tags(self, artist: str, limit: int = 8) -> list[str]:
        """Genre-ish tags for an artist — used when Spotify genres are missing."""
        if not self.enabled or not artist:
            return []
        key = artist.lower()
        if key in self._tag_cache:
            return self._tag_cache[key]
        data = self._get("artist.gettoptags", {"artist": artist})
        items = data.get("toptags", {}).get("tag", [])[:limit]
        tags = [it["name"].lower() for it in items if it.get("name")]
        self._tag_cache[key] = tags
        self._touch()
        return tags
