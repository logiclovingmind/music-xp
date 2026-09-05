"""Timeline — the English-language hits of 2008 to 2013.

One press walks the whole window and comes back with a period mixtape: roughly
five tracks a year, spread across the genres that defined the era (electropop,
dubstep, indie, pop-punk, crunk, R&B).

Last.fm has no chart-by-year endpoint, so the popularity signal comes from its
year tag pages ("2009") — which are user-applied and let strays through. Every
shortlisted track is therefore checked against the iTunes Search API and dropped
unless its earliest release really does land inside the window.

  python -m music_xp.timeline        # scout + publish the next Time{n} playlist
"""
from __future__ import annotations

import random
import re
from itertools import zip_longest

from . import itunes, score as scoring, sets, store
from .config import load_config
from .lastfm import LastFM
from .scout import is_live

STATE_FILE = "timeline_state.json"

START_YEAR, END_YEAR = 2008, 2013
YEARS = list(range(START_YEAR, END_YEAR + 1))

# Anything outside the Latin alphabet isn't what "just english songs" meant.
_NON_LATIN = re.compile(r"[^\x00-\x7F\u00C0-\u024F]")

# Scenes that are defined by singing in another language. Nationality tags
# (swedish, canadian) are deliberately absent — plenty of those charted in
# English.
_OTHER_LANGUAGE = {
    "j-pop", "jpop", "j-rock", "japanese", "visual kei", "k-pop", "kpop",
    "korean", "mandopop", "cantopop", "c-pop", "chinese", "bollywood",
    "hindi", "bhangra", "reggaeton", "latin pop", "musica latina",
    "chanson francaise", "french pop", "rap francais", "deutschrock",
    "deutsch", "german pop", "italo pop", "russian", "turkish", "arabic",
    "mpb", "sertanejo", "thai", "vietnamese",
}

# How many chart rows to pull per year, and how many survivors to keep.
_PER_YEAR_POOL = 100
_PER_YEAR_KEEP = 6
# Cap the iTunes verification work so a run can't stall for minutes.
_MAX_CHECKS_PER_YEAR = 34


def _english(title: str, artist: str) -> bool:
    return not (_NON_LATIN.search(title) or _NON_LATIN.search(artist))


def _release(artist: str, title: str) -> tuple[str, str]:
    """Earliest iTunes release date + genre for a track ("" if unknown).

    The earliest of all matching releases is what counts: a song's own single
    predates the greatest-hits compilation it later turned up on, and the
    compilation's date would wrongly push it out of the window.
    """
    hits = itunes.search_songs(f"{artist} {title}", "us", limit=25,
                               by_artist=False)
    want_t, want_a = sets.norm(title), sets.norm(artist)
    matches = [h for h in hits
               if sets.norm(h["title"]) == want_t and want_a in sets.norm(h["artist"])]
    dated = sorted((h for h in matches if h.get("release_date")),
                   key=lambda h: h["release_date"])
    if not dated:
        return "", ""
    return dated[0]["release_date"], dated[0].get("itunes_genre", "")


def _gather(lf: LastFM, rng: random.Random, skip_pairs: set[tuple[str, str]],
            year: int) -> list[dict]:
    """The year's biggest tagged tracks, verified to actually be from it."""
    print(f"::stop|{year}", flush=True)
    rows = lf.tag_top_tracks(str(year), limit=_PER_YEAR_POOL)
    kept: list[dict] = []
    checked = 0
    titles: set[str] = set()
    for row in rows:
        if len(kept) >= _PER_YEAR_KEEP or checked >= _MAX_CHECKS_PER_YEAR:
            break
        title, artist = row["title"], row["artist"]
        if not _english(title, artist):
            continue
        if sets.JUNK_TITLE.search(title) or is_live(title, ""):
            continue
        bare = sets.norm(re.sub(r"[(\[].*", "", title))
        if bare in titles:
            continue
        key = (artist.lower(), title.lower())
        if key in skip_pairs:
            continue
        if any(t in _OTHER_LANGUAGE for t in lf.artist_tags(artist)):
            continue
        checked += 1
        released, genre = _release(artist, title)
        if not released:
            continue
        if not (START_YEAR <= int(released[:4]) <= END_YEAR):
            continue
        titles.add(bare)
        skip_pairs.add(key)
        kept.append({
            "title": title,
            "artist": artist,
            "language": released[:4],          # the card's pill = the year
            "release_date": released,
            "genres": [genre.lower()] if genre else [],
            "similar_seed": False,
            "prominence": scoring.prominence_from_rank(row["rank"]),
        })
        print(f"::found|{year}|{artist} — {title}", flush=True)
    print(f"  {year}: {len(kept)} tracks ({checked} checked)")
    return kept


def run(dry_run: bool = False) -> None:
    cfg = load_config()
    lf = LastFM(cfg["_env"]["lastfm_key"])
    if not lf.enabled:
        raise SystemExit("Timeline needs a Last.fm API key (LASTFM_API_KEY).")

    model = store.load_taste()
    history = store.load_history()
    dislikes = store.load_dislikes()
    _skip_artists, seen_pairs, seen_ids = sets.seen(history, dislikes)

    target = int(cfg.get("picks_per_day", 30))
    rng = random.Random()

    print(f"Timeline — the hits of {START_YEAR} to {END_YEAR}…")
    found: list[dict] = []
    for year in YEARS:
        found.extend(_gather(lf, rng, seen_pairs, year))
    lf.flush()
    if not found:
        print("Couldn't pin down anything from the window right now. Try again.")
        return

    # Round-robin the years so the set walks the period instead of front-loading
    # whichever year happens to be best tagged.
    by_year: dict[str, list[dict]] = {}
    for c in scoring.rank(found, model, cfg, "timeline"):
        by_year.setdefault(c["language"], []).append(c)
    candidates = [c for row in zip_longest(*by_year.values()) for c in row if c]

    n, commit = sets.next_number(STATE_FILE)
    name = f"Time{n}"
    added = sets.publish(
        candidates, name=name, source="timeline", target=target,
        seen_ids=seen_ids, history=history, dry_run=dry_run,
        privacy=cfg.get("playlist_privacy", "PRIVATE"),
        desc=(f"Timeline by Music XP — the English-language hits of "
              f"{START_YEAR}–{END_YEAR}, the biggest songs of their time."),
        blurb=f"Back to {START_YEAR}–{END_YEAR}. The hits of their time.")
    if added and not dry_run:
        commit()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description=f"Timeline — {START_YEAR}-{END_YEAR} hits")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what it would find without publishing")
    run(dry_run=ap.parse_args().dry_run)
