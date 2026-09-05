"""Automatic feedback loop.

No manual rating needed: past picks that land in your Liked Music are positive
signals; picks older than feedback_window_days that were never liked are mild
negatives. Each history entry is processed once (marked 'graded').

Every mode grades identically and feeds the one taste model. What differs is
only the facets a mode can honestly report — see taste.facets_from_entry.

A like is a like wherever you gave it, so `learn_outside_likes` closes the last
gap: songs you liked on YouTube that Music XP never handed you.
"""
from __future__ import annotations

from datetime import date, timedelta

from . import store, taste

# What one deliberate like is worth. The same as liking a pick, because it is
# the same act — you heard the song and kept it.
LIKE_WEIGHT = 1.5

# Not getting around to a track is only a negative signal if you were likely to
# play the playlist at all. The daily list is the one you actually live with;
# the discovery modes are opt-in trips you press when you're in the mood, so an
# unplayed track there says nothing about your taste. Grading them the same way
# was scoring Irish 19 dislikes against 11 likes off nothing but neglect.
EXPLORATORY = {"xp", "irish", "timeline"}


def apply_feedback(cfg: dict, model: dict, liked_ids: set[str]) -> tuple[int, int]:
    """Grade ungraded history entries. Returns (positives, negatives)."""
    window = int(cfg.get("feedback_window_days", 4))
    # Grading rewrites entries in place, so the file must not change underneath
    # it — the dashboard grades the same entries whenever a track is rated.
    with store.transaction():
        history = store.load_history()
        dislikes = store.load_dislikes()
        today = date.today()
        pos = neg = 0

        for entry in history:
            if entry.get("graded"):
                continue
            vid = entry.get("video_id")
            f = taste.facets_from_entry(entry)

            # Explicit thumbs-down from the UI — strongest negative signal.
            if vid and vid in dislikes:
                taste.reinforce(model, f, amount=-1.5)
                entry["graded"] = True
                entry["outcome"] = "disliked"
                neg += 1
                continue

            if vid and vid in liked_ids:
                taste.reinforce(model, f, amount=LIKE_WEIGHT)
                entry["graded"] = True
                entry["outcome"] = "liked"
                pos += 1
                continue

            if (entry.get("source") or "daily") in EXPLORATORY:
                continue  # stays ungraded until you actually act on it

            try:
                picked_on = date.fromisoformat(entry.get("date", ""))
            except ValueError:
                picked_on = today
            if today - picked_on >= timedelta(days=window):
                taste.reinforce(model, f, amount=-0.35)
                entry["graded"] = True
                entry["outcome"] = "skipped"
                neg += 1
        store.save_history(history)
        store.save_taste(model)
    return pos, neg


def learn_outside_likes(cfg: dict, model: dict, lf, tracks: list[dict]) -> int:
    """Teach the model from likes Music XP never handed you. Returns how many.

    A like given in the YouTube app is the same verdict as a like given on a
    pick, and until now only the second one taught anything — so half the
    evidence about your taste never reached the model that spends it.

    Recorded into history as source "youtube" so a like counts exactly once,
    raises your XP level like any other keeper, and takes the artist out of
    circulation for the discovery modes: you already found them yourself.
    """
    from .config import language_names
    from .seed import guess_language

    # First run has no baseline, and the backlog behind it is not new evidence:
    # `seed` read your whole library, likes included, so learning it now would
    # count all of it twice. The exception is an artist the model has never
    # heard of, who by definition cannot have been in the library then — those
    # are a real gap and are learned even on the baseline run.
    baseline = store.load_seen_likes()
    seen = set() if baseline is None else baseline

    history = store.load_history()
    fresh: list[dict] = []
    known = {h["video_id"] for h in history if h.get("video_id")}
    pairs = {(h.get("artist", "").lower(), h.get("title", "").lower())
             for h in history}
    available = language_names(cfg)
    today = date.today().isoformat()
    learned = 0

    for t in tracks:
        vid, artist, title = t.get("video_id"), t.get("artist"), t.get("title")
        if not vid or vid in seen or not artist:
            continue
        seen.add(vid)
        # Already in history means the normal grader owns this one.
        if vid in known or (artist.lower(), (title or "").lower()) in pairs:
            continue
        if baseline is None and taste.is_known_artist(model, artist):
            continue
        genres = lf.artist_tags(artist) if lf and lf.enabled else []
        # No fallback language here. Half of what you like in the app is
        # something the daily run has no storefront for, and filing a Panamanian
        # reggaeton act under English because nothing else matched would teach
        # the English scout to go looking for more of it.
        language = guess_language(genres, available)
        when = t.get("liked_at") or today
        f = taste.facets(artist, genres, language, taste.era_of(when))
        taste.reinforce(model, f, amount=LIKE_WEIGHT)
        fresh.append({
            "date": when,
            "video_id": vid,
            "title": title or "",
            "artist": artist,
            "artist_display": artist,
            "language": language or "",
            "genres": genres,
            "score": 1.0,
            "playlist_id": None,
            "graded": True,
            "outcome": "liked",
            "source": "youtube",
            "facets": f,
        })
        learned += 1

    store.save_seen_likes(seen | {t["video_id"] for t in tracks if t.get("video_id")})
    if learned:
        # Tag lookups took a while; the file is re-read so these likes join
        # whatever else was written meanwhile instead of replacing it.
        with store.transaction():
            current = store.load_history()
            current.extend(fresh)
            store.save_history(current)
            store.save_taste(model)
    return learned
