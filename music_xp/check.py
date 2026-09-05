"""Verify your setup before the first real run.

  python -m music_xp.check

Checks: iTunes (free, no key) reachable + shows fresh sample; Last.fm optional
key; YouTube Music auth file present.
"""
from __future__ import annotations

from .config import load_config
from .lastfm import LastFM
from . import itunes
from . import ytmusic

OK = "\u2713"
BAD = "\u2717"
WARN = "\u2192"


def _check_itunes(cfg: dict) -> bool:
    lang = cfg["languages"][0]
    store = (lang.get("markets") or ["us"])[0]
    songs = itunes.search_songs("the", store, limit=10, by_artist=False)
    if not songs:
        # Fallback probe: the RSS feed.
        albums = itunes.recent_albums(store, count=5)
        if not albums:
            print(f"  {BAD} iTunes/Apple not reachable for storefront '{store}'. "
                  f"Check your internet connection.")
            return False
        print(f"  {OK} Apple RSS OK — {len(albums)} albums for '{store}'")
        return True

    window = int(cfg.get("release_window_days", 5))
    fresh = [s for s in songs if itunes.within_window(s["release_date"], window)]
    print(f"  {OK} iTunes OK (no key needed) — storefront '{store}', "
          f"{len(songs)} songs, {len(fresh)} within {window}-day window")
    for s in fresh[:5]:
        print(f"       • {s['artist']} — {s['title']}  ({s['release_date']})")
    if not fresh:
        print(f"       {WARN} none day-fresh for this probe term — normal; the real "
              f"run seeds searches with your artists")
    return True


def _check_lastfm(cfg: dict) -> bool:
    lf = LastFM(cfg["_env"]["lastfm_key"])
    if not lf.enabled:
        print(f"  {WARN} Last.fm: key missing (optional, improves discovery + "
              f"genre matching). Add LASTFM_API_KEY to .env")
        return True
    try:
        sim = lf.similar_artists("Daft Punk", limit=3)
    except Exception as e:
        print(f"  {BAD} Last.fm call failed: {e}")
        return False
    if sim:
        print(f"  {OK} Last.fm OK — similar to Daft Punk: {', '.join(sim)}")
    else:
        print(f"  {WARN} Last.fm responded but returned no data — check the key")
    return True


def _check_ytmusic() -> bool:
    if ytmusic.AUTH_FILE.exists():
        print(f"  {OK} YouTube Music auth found ({ytmusic.AUTH_FILE.name})")
    else:
        print(f"  {WARN} YouTube Music not authenticated yet — run:  ytmusicapi browser")
        print(f"       (needed to publish; not needed for --dry-run)")
    return True


def run() -> None:
    cfg = load_config()
    print("Checking Daily Fresh Music setup:\n")
    itunes_ok = _check_itunes(cfg)
    _check_lastfm(cfg)
    _check_ytmusic()
    print()
    if itunes_ok:
        print(f"{OK} Ready. Next: python -m music_xp.seed  then  "
              f"python -m music_xp.main --dry-run")
    else:
        print(f"{BAD} Fix the connection issue above, then re-run: python -m music_xp.check")


if __name__ == "__main__":
    run()
