"""One scoring formula, shared by every mode.

    score = pull × taste affinity  +  (1 - pull) × prominence  -  dislike penalty

Every mode computes that same expression. What a mode chooses is `pull`: how
much of its ranking should come from resembling what you already like, versus
how established a track is in whatever corner it was found in.

That single dial is what keeps the modes different without letting them drift
apart. Ranking XP+ by taste match would quietly turn it into a second Today's
Picks, which is the opposite of the button's job — so XP+ leans on prominence
and accepts a low hit rate as the price of going somewhere new. The daily run
has no prominence signal to lean on and doesn't want one.

The penalty sits outside the blend, at full strength for everyone. How much of
your taste a mode chases is a matter of what it's for; whether it should hand
you something you've already rejected is not.
"""
from __future__ import annotations

import math

from . import taste


def prominence_from_listeners(listeners: int) -> float:
    """How established a track is, read off its Last.fm audience."""
    return min(0.98, 0.40 + 0.10 * math.log10(max(int(listeners), 1)))


def prominence_from_rank(position: int) -> float:
    """How established a track is, read off its place on a chart page."""
    return max(0.55, 0.95 - position * 0.004)

# Measured over the graded history, prominence is a poor predictor of what you
# keep — AUC 0.58 in XP+, and 0.42 in Timeline, where chart rank predicts the
# wrong way. So no mode is given a pull below 0.3 purely on prominence's merits;
# XP+ sits there because exploring is its purpose, not because popularity works.
PULL = {
    "daily": 1.0,      # no prominence signal exists for a fresh release
    "timeline": 0.7,   # you opted into the era; chart position anti-predicts
    "irish": 0.6,      # you opted into the genre, so taste leads
    "xp": 0.3,         # deliberately not optimising for what you'd already like
}


def candidate_facets(cand: dict) -> dict:
    """Taste facets for a freshly scouted candidate."""
    f = taste.facets(cand["artist"], cand.get("genres", []),
                     cand.get("language"), taste.era_of(cand.get("release_date")))
    f["similar_seed"] = cand.get("similar_seed", False)
    return f


def score_candidate(cand: dict, model: dict, cfg: dict,
                    mode: str = "daily") -> float:
    """Return a 0-1 score for one candidate, on the same scale in every mode."""
    f = candidate_facets(cand)
    pull = PULL.get(mode, 1.0)
    base = pull * taste.affinity(model, f, cfg)
    if pull < 1.0:
        base += (1.0 - pull) * float(cand.get("prominence") or 0.0)
    return round(max(0.0, min(1.0, base - taste.penalty(model, f, cfg))), 4)


def rank(candidates: list[dict], model: dict, cfg: dict,
         mode: str = "daily") -> list[dict]:
    """Score, de-duplicate by (artist,title), and sort best-first."""
    seen: set[tuple[str, str]] = set()
    scored: list[dict] = []
    for c in candidates:
        key = (c["artist"].lower(), c["title"].lower())
        if key in seen:
            continue
        seen.add(key)
        c = dict(c)
        c["score"] = score_candidate(c, model, cfg, mode)
        c["is_new_artist"] = not taste.is_known_artist(model, c["artist"])
        scored.append(c)
    # Prominence breaks ties. In XP+ a great many candidates are things the model
    # has no opinion at all about, and among those "more people have heard it"
    # is the only thing left to go on.
    scored.sort(key=lambda c: (c["score"], float(c.get("prominence") or 0.0)),
                reverse=True)
    return scored


# A name the taste model cannot know, used to measure the unknown-artist ceiling.
_NOBODY = "\x00 nobody \x00"


def unknown_ceiling(model: dict, cfg: dict,
                    language: str | None = None) -> float | None:
    """The best score an artist with no history could reach in this language.

    Measured rather than assumed: score a name the model cannot know, carrying
    the genre it loves most. Genre overlap takes the best match instead of
    summing, so nothing an unknown can do beats this.

    It has to be per-language, because scoring backs off from the per-language
    genre table to the global one and the reachable maximum differs between them.
    A single global ceiling came out at 0.19 while real French candidates scored
    0.39 — above their own supposed maximum, which makes the gate meaningless.

    Returns None when the model has no table for this language. The global table
    alone cannot stand in: spread across 2,815 genres its best weight is half
    that of any language's own table, so a ceiling read off it (0.19 against
    0.35-0.43) measures how diffuse the table is, not how good a track is — and
    it hands the tracks we know least about the softest gate of all.
    """
    per_lang = ((model.get("by_language") or {}).get(language) or {}).get("genres")
    if not per_lang:
        return None

    best = 0.0
    for table in (model.get("genres") or {}, per_lang):
        if not table:
            continue
        genre = max(table.items(), key=lambda kv: kv[1])[0]
        f = taste.facets(_NOBODY, [genre], language, None)
        best = max(best, taste.affinity(model, f, cfg) - taste.penalty(model, f, cfg))
    return max(0.0, best)


def _interleave(known: list[dict], new: list[dict]) -> list[dict]:
    """Spread discoveries through the playlist instead of burying them at the end.

    Their scores are not comparable to the familiar picks — different ladders —
    so ordering the merged list by score would always sink every new artist to
    the bottom, which is where you stop listening.
    """
    if not new or not known:
        return known or new
    out: list[dict] = []
    step = (len(known) + len(new)) / len(new)
    next_new, n_taken = step, 0
    for i, track in enumerate(known, start=1):
        out.append(track)
        while n_taken < len(new) and i >= next_new:
            out.append(new[n_taken])
            n_taken += 1
            next_new += step
    out.extend(new[n_taken:])
    return out


def select(candidates: list[dict], model: dict, cfg: dict) -> list[dict]:
    """Fill the playlist from two ladders: artists you know, and artists you don't.

    A single threshold cannot serve both. An unknown artist has no artist-level
    signal to earn score with, so their ceiling sits *below* `min_score` and they
    could never be picked at all — which made `discovery_ratio` unreachable dead
    config for as long as it existed. Judging unknowns against each other instead
    is what makes discovery possible without weakening the familiar half.
    """
    target = int(cfg.get("picks_per_day", 30))
    max_per_artist = int(cfg.get("max_per_artist", 2))
    min_score = float(cfg.get("min_score", 0.32))
    quality = float(cfg.get("discovery_quality", 0.80))
    reserved = int(float(cfg.get("discovery_ratio", 0.0)) * target)

    ceilings: dict[str | None, float | None] = {}

    def clears_discovery(c: dict) -> bool:
        lang = c.get("language")
        if lang not in ceilings:
            ceilings[lang] = unknown_ceiling(model, cfg, lang)
        # No table for this language means no peers to judge it against. An
        # unknown artist in an unknown corner is absence of evidence twice over.
        return ceilings[lang] is not None and c["score"] >= ceilings[lang] * quality

    known_pool = [c for c in candidates
                  if not c.get("is_new_artist") and c["score"] >= min_score]
    # An unknown with no genre tags has no evidence at all, only absence.
    new_pool = [c for c in candidates
                if c.get("is_new_artist") and c.get("genres")
                and clears_discovery(c)]

    picked_known: list[dict] = []
    picked_new: list[dict] = []
    artist_counts: dict[str, int] = {}
    taken: set[int] = set()

    def fill(pool: list[dict], into: list[dict], room: int) -> None:
        for c in pool:
            if len(into) >= room:
                break
            if id(c) in taken:
                continue
            artist = c["artist"].lower()
            if artist_counts.get(artist, 0) >= max_per_artist:
                continue
            into.append(c)
            taken.add(id(c))
            artist_counts[artist] = artist_counts.get(artist, 0) + 1

    fill(known_pool, picked_known, target - reserved)
    fill(new_pool, picked_new, reserved)
    # Neither side is a quota. If one runs dry, the other may use the space.
    spare = target - len(picked_known) - len(picked_new)
    if spare > 0:
        fill(known_pool, picked_known, len(picked_known) + spare)
        spare = target - len(picked_known) - len(picked_new)
    if spare > 0:
        fill(new_pool, picked_new, len(picked_new) + spare)

    return _interleave(picked_known, picked_new)
