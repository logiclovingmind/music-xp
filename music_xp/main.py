"""Daily run: feedback → radio scout → score → publish a YouTube Music playlist.

  python -m music_xp.main            # full daily run
  python -m music_xp.main --dry-run  # score + print picks, don't touch YT Music
"""
from __future__ import annotations

import argparse
from datetime import date

from . import arrange
from . import artistart
from . import notify
from .config import load_config
from .feedback import apply_feedback, learn_outside_likes
from .lastfm import LastFM
from . import radio
from . import score as scoring
from . import store
from . import taste
from . import ytmusic


def run(dry_run: bool = False) -> None:
    cfg = load_config()
    lf = LastFM(cfg["_env"]["lastfm_key"])
    model = store.load_taste()

    if not model.get("artists"):
        raise SystemExit("Taste model is empty. Run:  python -m music_xp.seed")

    yt = ytmusic.client()  # unauthenticated — search / track resolution
    # Always authenticated now: the scout reads your playlists and YouTube's
    # radio, so even a dry run needs the account. Writes stay behind `dry_run`.
    ytd = ytmusic.data_client()

    # 1) Feedback loop (uses your Liked Music as the signal).
    if not dry_run:
        # Age out stale taste first, then let today's feedback land at full weight.
        taste.decay(model, float(cfg.get("taste_decay", 0.0)))
        tracks = ytmusic.liked_tracks(ytd)
        if not tracks:
            # A read that came back with nothing is a read that failed. Grading
            # on it would call every aged-out pick a skip and teach the model
            # you rejected a week of music you never saw.
            print("Couldn't read your Liked Music — skipping feedback this run.")
        else:
            pos, neg = apply_feedback(cfg, model, {t["video_id"] for t in tracks})
            print(f"Feedback: +{pos} liked, -{neg} skipped → taste updated")
            # Anything you liked in the YouTube app counts too — same verdict,
            # and until now the only one of the two that taught nothing.
            outside = learn_outside_likes(cfg, model, lf, tracks)
            if outside:
                print(f"Learned {outside} likes you gave outside Music XP")

    # 2) Scout: seed YouTube's radio with the tracks you chose yourself, and keep
    #    whatever comes back fresh. Songs already picked on ANY previous day are
    #    dropped by (artist, title) as well as by id, because the same song
    #    surfaces under different video ids (audio vs video version).
    history = store.load_history()
    seen_video_ids = {h["video_id"] for h in history if h.get("video_id")}
    seen_pairs = {(h.get("artist", "").lower(), h.get("title", "").lower())
                  for h in history}

    seeds = radio.collect_seeds(ytd, cfg)
    print(f"Seeded from {len(seeds)} of your own tracks; expanding radio "
          f"({'/'.join(cfg.get('radio_prefixes', ['RD']))})…")
    reco_ids = radio.gather_video_ids(ytd, seeds, cfg)
    print(f"Radio returned {len(reco_ids)} distinct videos; fetching metadata…")
    candidates, stats = radio.build_candidates(
        ytd, reco_ids, cfg, lf, seen_video_ids, seen_pairs, store.load_dislikes())
    print(f"Fresh & musical: {stats['kept']} candidates "
          f"(dropped — stale {stats['stale']}, non-music {stats['not_music']}, "
          f"live {stats['live']}, already served {stats['already_seen']}, "
          f"disliked {stats['disliked']}, unnamed {stats['unnamed']})")

    if not candidates:
        print("No fresh releases matched today. Try widening release_window_days.")
        return

    # 3) Score + select, on two ladders: artists you know, and artists you don't.
    ranked = scoring.rank(candidates, model, cfg)
    picks = scoring.select(ranked, model, cfg)
    n_new = sum(1 for c in picks if c.get("is_new_artist"))
    print(f"\nSelected {len(picks)} picks "
          f"({n_new} new-to-you, min_score={cfg['min_score']}):")
    for c in picks:
        flag = "NEW" if c.get("is_new_artist") else "   "
        print(f"  {c['score']:.2f} {flag} [{(c.get('language') or '--')[:2]}]  "
              f"{c['artist']} — {c['title']}")

    if dry_run:
        print("\n(dry-run: nothing published)")
        return

    # 4) Resolve to YT Music and publish.
    # De-dupe on the FINAL playable video id. The same song can be scouted under
    # different artist credits on different days — e.g. a collab attributed to
    # each collaborator in turn ("Dai Dai" as Shakira one day, Burna Boy the next)
    # — so an (artist,title) check can't catch it (the two records share no artist
    # token). The resolved video id is the only reliable signal. We also backfill
    # from the next-best candidates so dropping a repeat doesn't shrink the list.
    min_score = float(cfg.get("min_score", 0.32))
    target = len(picks)
    seen_ids = {h["video_id"] for h in history if h.get("video_id")}
    picked_ids = {id(c) for c in picks}
    backfill = [c for c in ranked
                if id(c) not in picked_ids and c["score"] >= min_score]

    video_ids: list[str] = []
    resolved: list[dict] = []
    for c in list(picks) + backfill:
        if len(video_ids) >= target:
            break
        vid = c.get("video_id") or ytmusic.search_track(yt, c["title"], c["artist"])
        if not vid:
            continue
        # Scouted album leads can be the music video; swap to pure audio.
        vid = ytmusic.audio_version(yt, vid, c["title"], c["artist"])
        if vid in seen_ids:  # already published before, or already added today
            continue
        # Full artist credits (features included) for display/tags. YT-scouted
        # picks already carry them; iTunes-sourced ones get enriched here.
        display = c.get("artist_display")
        if not display:
            names = ytmusic.track_credits(yt, vid, c["title"], c["artist"])
            display = ", ".join(names) if names else c["artist"]
        seen_ids.add(vid)
        video_ids.append(vid)
        resolved.append({**c, "video_id": vid, "artist_display": display})

    if not video_ids:
        print("Could not resolve any picks on YouTube Music today.")
        return

    # Sequence the playlist into an energy arc (ease in → peak → cool down).
    resolved = arrange.arrange(resolved)
    video_ids = [c["video_id"] for c in resolved]

    today = date.today().isoformat()
    name = f"{cfg['playlist_name_prefix']} — {today}"
    desc = f"Auto-generated fresh music for {today}. Like songs you enjoy to train it."
    pid, added = ytmusic.create_daily_playlist(
        ytd, name, desc, video_ids, cfg.get("playlist_privacy", "PRIVATE")
    )
    print(f"\nCreated playlist '{name}' ({added}/{len(video_ids)} tracks added) → {pid}")
    notify.notify("Music XP", f"{added} fresh tracks ready to explore.",
                  subtitle=name)

    # 5) Record picks for tomorrow's feedback pass.
    fresh = []
    for c in resolved:
        fresh.append({
            "date": today,
            "video_id": c["video_id"],
            "title": c["title"],
            "artist": c["artist"],
            "artist_display": c.get("artist_display") or c["artist"],
            "language": c["language"],
            "genres": c.get("genres", []),
            "score": c["score"],
            "playlist_id": pid,
            "graded": False,
            "source": "daily",
            "facets": taste.facets(c["artist"], c.get("genres", []),
                                   c["language"], taste.era_of(c.get("release_date"))),
        })
    # History was read before the playlist was published, minutes of network
    # ago, and the dashboard may have graded a track since. Re-reading under the
    # lock adds today's picks to the current file instead of to a stale copy.
    with store.transaction():
        history = store.load_history()
        history.extend(fresh)
        store.save_history(history)
    print("Recorded picks → data/history.json")

    # Warm the hero-wall photos while nobody is waiting: face-checking every
    # candidate takes minutes on a fresh day, and the dashboard would otherwise
    # stall on the first open of the morning.
    artistart.images_for_artists(list(dict.fromkeys(
        c.get("artist_display") or c["artist"] for c in resolved)))
    print("Cached artist photos → data/artist_images_faces.json")


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily fresh-music playlist builder")
    ap.add_argument("--dry-run", action="store_true",
                    help="Score and print picks without touching YouTube Music")
    args = ap.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
