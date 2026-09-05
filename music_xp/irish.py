"""Irish Mode — fiddles, pipes, whistles and the whole Celtic session.

Where XP+ reaches for anywhere on earth, this points at one place and goes deep:
Irish and Celtic traditional music, plus anything carried by the instruments that
define it — fiddle, uilleann pipes, bagpipes, tin whistle, bodhrán, accordion.

Instrument tags on their own are treacherous (half of Last.fm's "fiddle" page is
American bluegrass), so an artist only counts if their own tags also place them
in the Irish/Celtic world. That's what keeps this Irish Mode rather than a
generic folk shuffle.

  python -m music_xp.irish        # scout + publish the next Irish{n} playlist
"""
from __future__ import annotations

import random
from itertools import zip_longest

from . import score as scoring, sets, store
from .config import load_config
from .lastfm import LastFM

STATE_FILE = "irish_state.json"

# The tradition itself — these tag pages are Irish/Celtic by definition.
TRAD_TAGS = [
    "irish folk", "irish traditional", "celtic", "celtic folk", "celtic rock",
    "celtic punk", "irish rebel", "sean nos", "irish", "scottish folk",
    "traditional irish", "irish punk",
]

# The sound of the session. Broad pages, so every hit is checked against the
# markers below before it earns a place.
INSTRUMENT_TAGS = [
    "fiddle", "uilleann pipes", "bagpipes", "tin whistle", "bodhran",
    "celtic harp", "irish accordion", "banjo", "concertina", "pennywhistle",
]

# An artist qualifies as "full on Irish mode" if any of their own top tags
# lands in here. Scottish trad is in because the pipes are half the point.
# Bare "folk" is deliberately absent: it let American folk-punk and Appalachian
# fiddlers in off the instrument pages, which is the exact failure this guards.
MARKERS = {
    "irish", "ireland", "irish folk", "irish traditional", "traditional irish",
    "celtic", "celtic folk", "celtic rock", "celtic punk", "celtic metal",
    "irish rebel", "irish punk", "sean nos", "scottish", "scotland",
    "scottish folk", "gaelic", "trad", "irish music",
}

_MIN_LISTENERS = 400          # trad is a small world; a lower bar than XP+
_TAGS_PER_RUN = 8
_ARTISTS_PER_TAG = 5


def _qualifies(tags: list[str]) -> bool:
    """Does this artist actually sit in the Irish/Celtic world?"""
    return any(t.lower() in MARKERS for t in tags)


def _gather(lf: LastFM, rng: random.Random, skip_artists: set[str],
            seen_pairs: set[tuple[str, str]], tags: list[str],
            target: int) -> list[dict]:
    return sets.explore(
        lf, rng, tags=tags, skip_artists=skip_artists, seen_pairs=seen_pairs,
        target=target, min_listeners=_MIN_LISTENERS,
        artists_per_tag=_ARTISTS_PER_TAG, width=20,
        qualifies=lambda tag, artist_tags: _qualifies(artist_tags),
        # Record them under whichever parts of the tradition they actually
        # carry, not the page they happened to be found on — a fiddler reached
        # via "banjo" is still Irish trad, and that's what the model should
        # learn from a verdict on them.
        genres_for=lambda tag, artist_tags: (
            sorted(set(artist_tags) & MARKERS) or [tag]))


def run(dry_run: bool = False) -> None:
    cfg = load_config()
    lf = LastFM(cfg["_env"]["lastfm_key"])
    if not lf.enabled:
        raise SystemExit("Irish Mode needs a Last.fm API key (LASTFM_API_KEY).")

    model = store.load_taste()
    history = store.load_history()
    dislikes = store.load_dislikes()
    skip_artists, seen_pairs, seen_ids = sets.seen(history, dislikes)

    target = int(cfg.get("picks_per_day", 30))
    rng = random.Random()

    # Mostly the tradition, with a couple of instrument pages mixed in so a
    # session fiddler outside the usual trad names can still turn up.
    trad = TRAD_TAGS[:]
    inst = INSTRUMENT_TAGS[:]
    rng.shuffle(trad)
    rng.shuffle(inst)
    tags = trad[:_TAGS_PER_RUN - 3] + inst[:3]
    rng.shuffle(tags)

    print("Irish Mode — fiddles, pipes and whistles…")
    print(f"Session tags: {', '.join(tags)}")
    candidates = _gather(lf, rng, skip_artists, seen_pairs, tags, target)
    lf.flush()
    if not candidates:
        print("Couldn't find anything new in the session right now. Try again.")
        return

    # Best first within each tag, then one tag after another, so the set doesn't
    # hand itself entirely to whichever page is most famous.
    candidates = scoring.rank(candidates, model, cfg, "irish")
    by_tag: dict[str, list[dict]] = {}
    for c in candidates:
        by_tag.setdefault(c["language"], []).append(c)
    candidates = [c for row in zip_longest(*by_tag.values()) for c in row if c]

    n, commit = sets.next_number(STATE_FILE)
    name = f"Irish{n}"
    added = sets.publish(
        candidates, name=name, source="irish", target=target,
        seen_ids=seen_ids, history=history, dry_run=dry_run,
        privacy=cfg.get("playlist_privacy", "PRIVATE"),
        desc=("Irish Mode by Music XP — Irish and Celtic traditional music, "
              "fiddles, uilleann pipes, whistles and the whole session."),
        blurb="The session is in. Fiddles, pipes and whistles.")
    if added and not dry_run:
        commit()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Irish Mode — Celtic discovery")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what it would explore without publishing")
    run(dry_run=ap.parse_args().dry_run)
