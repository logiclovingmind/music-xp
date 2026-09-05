"""Weekly digest: gather the songs you actually liked and make one keeper playlist.

The daily lists are disposable; this rolls the best-scored *liked* picks from the
last N days into a single playlist so the gems don't get lost. Fully automatic —
it reads the same history/feedback the daily run maintains.

    python -m music_xp.digest             # publish a 7-day digest
    python -m music_xp.digest --days 14   # a fortnight
    python -m music_xp.digest --dry-run   # list keepers, don't publish
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from . import arrange, store, ytmusic
from .config import load_config


def liked_keepers(history: list[dict], days: int) -> list[dict]:
    """Distinct liked tracks from the last `days`, best score first."""
    cutoff = date.today() - timedelta(days=days)
    seen: set[str] = set()
    out: list[dict] = []
    for h in history:
        if h.get("outcome") != "liked":
            continue
        vid = h.get("video_id")
        if not vid or vid in seen:
            continue
        try:
            picked = date.fromisoformat(h.get("date", ""))
        except ValueError:
            continue
        if picked < cutoff:
            continue
        seen.add(vid)
        out.append(h)
    out.sort(key=lambda h: h.get("score", 0.0), reverse=True)
    return out


def run(days: int = 7, limit: int = 50, dry_run: bool = False) -> str | None:
    cfg = load_config()
    keepers = liked_keepers(store.load_history(), days)[:limit]
    if not keepers:
        print(f"No liked tracks in the last {days} days — nothing to digest yet.")
        return None

    keepers = arrange.arrange(keepers)
    video_ids = [h["video_id"] for h in keepers]
    print(f"Weekly digest: {len(keepers)} liked keepers from the last {days} days")
    for h in keepers:
        print(f"  {h.get('score', 0):.2f}  [{h.get('language', '')[:2]}]  "
              f"{h.get('artist_display') or h.get('artist')} — {h.get('title')}")

    if dry_run:
        print("\n(dry-run: nothing published)")
        return None

    today = date.today().isoformat()
    name = f"{cfg['playlist_name_prefix']} Weekly — {today}"
    desc = f"Your liked keepers from the last {days} days, best first."
    ytd = ytmusic.data_client()
    pid, added = ytmusic.create_daily_playlist(
        ytd, name, desc, video_ids, cfg.get("playlist_privacy", "PRIVATE"))
    print(f"\nCreated digest '{name}' ({added}/{len(video_ids)} tracks) → {pid}")

    # Record it so the dashboard can list every digest and link straight to it.
    digests = store.load_digests()
    digests.append({
        "name": name,
        "date": today,
        "days": days,
        "playlist_id": pid,
        "tracks": [{
            "video_id": h.get("video_id", ""),
            "title": h.get("title", ""),
            "artist": h.get("artist_display") or h.get("artist", ""),
            "language": h.get("language", ""),
            "score": round(float(h.get("score", 0.0)), 2),
        } for h in keepers],
    })
    store.save_digests(digests)
    return pid


def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly liked-keepers digest playlist")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(days=args.days, limit=args.limit, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
