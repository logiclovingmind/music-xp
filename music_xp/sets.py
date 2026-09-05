"""Shared tail for every "press a button, get a playlist" mode.

XP+ (xp.py), Irish Mode (irish.py) and Timeline (timeline.py) each scout in their
own way, but from the moment they hold a candidate list they all do the same
thing: resolve titles to playable YT Music audio, drop anything already seen,
sequence them, publish a numbered playlist, and record the picks to history so
the dashboard and the feedback loop can see them.

That tail lives here so a fix to resolution or history shape lands in all three.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date

from . import arrange, notify, store, taste, ytmusic
from .config import DATA_DIR
from .score import prominence_from_listeners

# Alternate takes, karaoke and soundtrack filler all sit high in an artist's top
# tracks but are nobody's idea of a discovery. The canonical version outranks
# them anyway, so dropping these leaves the song itself.
JUNK_TITLE = re.compile(
    r"\b(remix|karaoke|instrumental|live at|live in|live from|acoustic version"
    r"|nightcore|sped up|slowed|8d audio|cover version|medley|reprise"
    r"|interlude|skit|intro|outro)\b|\(cv\.|\(live\)|\[live\]|- live\b",
    re.IGNORECASE)


def norm(s: str) -> str:
    """Fold to bare letters — "Oye Cómo Va" and "Oye Como Va" are one song."""
    flat = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in flat
                                            if not unicodedata.combining(c)))


def explore(lf, rng, *, tags: list[str], qualifies, genres_for,
            skip_artists: set[str], seen_pairs: set[tuple[str, str]],
            target: int, min_listeners: int, artists_per_tag: int = 5,
            tracks_per_artist: int = 2, width: int = 16) -> list[dict]:
    """Walk Last.fm tag pages and collect candidate tracks.

    XP+ and Irish Mode both search this way: take the leading names off a tag
    page, shuffle so two presses don't hand back the same artists, check each
    artist's own tags before believing the page — tag pages are full of
    mislabelled strays — then keep only their best-known songs.

    What actually differs between the two is which tags to walk and what counts
    as a genuine hit on them, so those arrive as arguments: `qualifies(tag,
    artist_tags)` decides whether an artist really belongs to the tag, and
    `genres_for(tag, artist_tags)` says what to record them under.
    """
    from .scout import is_live

    candidates: list[dict] = []
    for tag in tags:
        print(f"::stop|{tag}", flush=True)
        picked = 0
        head = lf.tag_top_artists(tag, limit=25)
        rng.shuffle(head)
        for artist in head:
            if picked >= artists_per_tag:
                break
            if artist.lower() in skip_artists:
                continue
            artist_tags = lf.artist_tags(artist)
            if not qualifies(tag, artist_tags):
                continue
            took = 0
            titles: set[str] = set()
            for t in lf.artist_top_tracks(artist, limit=8):
                if took >= tracks_per_artist:
                    break
                title = t["title"]
                if t["listeners"] < min_listeners:
                    break        # ranked by listeners, so the rest are worse
                if JUNK_TITLE.search(title) or is_live(title, ""):
                    continue
                bare = norm(re.sub(r"[(\[].*", "", title))
                if bare in titles:   # "Wololo" and "Wololo (feat. …)" are one song
                    continue
                key = (artist.lower(), title.lower())
                if key in seen_pairs:
                    continue
                titles.add(bare)
                seen_pairs.add(key)
                candidates.append({
                    "title": title,
                    "artist": artist,
                    "language": tag,          # the card's pill, not a language
                    "genres": genres_for(tag, artist_tags),
                    "similar_seed": False,
                    "prominence": prominence_from_listeners(t["listeners"]),
                })
                print(f"::found|{tag}|{artist} — {title}", flush=True)
                took += 1
            if took:
                picked += 1
                skip_artists.add(artist.lower())
        print(f"  {tag:{width}s}: {picked} artists")
        if len(candidates) >= target * 2:
            break
    return candidates


def next_number(state_file: str) -> tuple[int, callable]:
    """Read a mode's playlist counter, returning (n+1, commit).

    The counter is only written once publishing actually succeeded, so a failed
    run doesn't burn a number and leave a gap in the names.
    """
    path = DATA_DIR / state_file
    try:
        n = int(json.loads(path.read_text()).get("n", 0))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        n = 0

    def commit() -> None:
        DATA_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps({"n": n + 1}, indent=2))

    return n + 1, commit


def seen(history: list[dict], dislikes: set[str]) -> tuple[set, set, set]:
    """Everything already spent: artists shown, (artist,title) pairs, video ids."""
    skip_artists = {h.get("artist", "").lower() for h in history if h.get("artist")}
    pairs = {(h.get("artist", "").lower(), h.get("title", "").lower())
             for h in history}
    ids = {h["video_id"] for h in history if h.get("video_id")} | set(dislikes)
    return skip_artists, pairs, ids


def publish(candidates: list[dict], *, name: str, desc: str, source: str,
            target: int, seen_ids: set[str], privacy: str,
            history: list[dict], blurb: str, dry_run: bool = False) -> int:
    """Resolve, publish and record a finished candidate list. Returns count."""
    if dry_run:
        for c in candidates[:target]:
            print(f"  {c['score']:.2f}  {c['language']:14s} "
                  f"{c['artist']} — {c['title']}")
        return len(candidates[:target])

    yt = ytmusic.client()
    ytd = ytmusic.data_client()
    print("::phase|resolving", flush=True)
    resolved: list[dict] = []
    for c in candidates:
        if len(resolved) >= target:
            break
        vid = ytmusic.search_track(yt, c["title"], c["artist"])
        if not vid:
            continue
        vid = ytmusic.audio_version(yt, vid, c["title"], c["artist"])
        if vid in seen_ids:
            continue
        seen_ids.add(vid)
        names = ytmusic.track_credits(yt, vid, c["title"], c["artist"])
        display = ", ".join(names) if names else c["artist"]
        resolved.append({**c, "video_id": vid, "artist_display": display,
                         "score": round(c["score"], 2)})

    if not resolved:
        print("Found tracks but couldn't resolve them on YouTube. Try again.")
        return 0

    resolved = arrange.arrange(resolved)
    video_ids = [c["video_id"] for c in resolved]
    pid, added = ytmusic.create_daily_playlist(ytd, name, desc, video_ids, privacy)
    print(f"\nCreated '{name}' ({added}/{len(video_ids)} tracks added) → {pid}")
    notify.notify("Music XP", blurb, subtitle=name)

    today = date.today().isoformat()
    fresh = []
    for c in resolved:
        entry = {
            "date": today,
            "video_id": c["video_id"],
            "title": c["title"],
            "artist": c["artist"],
            "artist_display": c.get("artist_display") or c["artist"],
            # The mode's own label — a Celtic tag, a year — kept for the card
            # pill in the dashboard. It is not a language, which is exactly why
            # taste facets are worked out separately rather than read off it.
            "language": c["language"],
            "genres": c.get("genres", []),
            "score": c["score"],
            "playlist_id": pid,
            "graded": False,
            "source": source,
            "xp_set": name,
        }
        entry["facets"] = taste.facets_from_entry(entry)
        fresh.append(entry)
    # The caller's copy predates publishing the playlist, so the picks are added
    # to whatever the file holds now rather than overwriting it with that copy.
    with store.transaction():
        current = store.load_history()
        current.extend(fresh)
        store.save_history(current)
    history.extend(fresh)
    print(f"Recorded {len(resolved)} picks → data/history.json")
    return len(resolved)
