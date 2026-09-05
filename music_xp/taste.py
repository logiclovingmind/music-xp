"""One taste model. Every mode feeds it, every mode reads it.

This replaces the per-language profiles. Those partitioned taste by language, so
anything that wasn't a language had nowhere to go: XP+ wrote its genre into the
language slot, Irish wrote its tag, Timeline wrote the year, and each one minted
a profile called "polka" or "celtic harp" or "2009" that no scout ever opened.
Two thirds of the modes were teaching a file nobody read.

Here a track is a bundle of facets — artist, genres, language, era — and a
verdict reinforces every facet it carries. A mode that genuinely cannot know a
facet (XP+ has no language for a Mongolian throat-singing track) leaves it out
rather than guessing, and the remaining facets still land.

Artists and genres are held twice: globally, and again per language. Scoring
backs off from the per-language table to the global one, which is what lets both
things be true at once — Korean taste stays out of French picks where there is
real evidence either way, while a genre you liked once in XP+ still counts for
something in a language that has never been scouted. The old model could only
return 0.0 there, which is why an unseeded language could never bootstrap.
"""
from __future__ import annotations

import math

# How much of a verdict each facet absorbs. The artist is the signal you are
# most sure of — you liked *that song by that person* — so it takes the full
# amount and the broader, blurrier facets take progressively less.
W_ARTIST = 1.0
W_GENRE = 0.5
W_LANGUAGE = 0.3
W_ERA = 0.3

# Backoff: how far to trust the global tables when a language has no opinion of
# its own. Discounted, because "you like this artist somewhere" is weaker
# evidence than "you like this artist here" — but never zero, which is what the
# old per-language profiles were stuck at.
FALLBACK = 0.6

VERSION = 2


def empty() -> dict:
    return {
        "version": VERSION,
        "artists": {},
        "genres": {},
        "languages": {},
        "eras": {},
        "by_language": {},
        "avoid": {"artists": {}, "genres": {}, "by_language": {}},
        "plays": 0,
    }


def ensure(model: dict) -> dict:
    """Fill in any missing tables so a partial file is still usable."""
    base = empty()
    for key, default in base.items():
        model.setdefault(key, default)
    av = model["avoid"]
    for key in ("artists", "genres", "by_language"):
        av.setdefault(key, {})
    model["version"] = VERSION
    return model


def _lang_tables(model: dict, language: str, avoid: bool = False) -> dict:
    root = model["avoid"]["by_language"] if avoid else model["by_language"]
    tables = root.setdefault(language, {})
    tables.setdefault("artists", {})
    tables.setdefault("genres", {})
    return tables


def _bump(table: dict, key: str, amount: float) -> None:
    key = (key or "").strip().lower()
    if not key:
        return
    table[key] = round(max(0.0, table.get(key, 0.0) + amount), 4)


def era_of(value: str | int | None) -> str | None:
    """Decade label for a date or year — "2009-05-01" and 2009 both give "2000s".

    Decades rather than years because a year is too fine to generalise from:
    liking three songs from 2009 says you like that era, not that specific
    trip around the sun.
    """
    if value is None:
        return None
    text = str(value).strip()
    if len(text) < 4 or not text[:4].isdigit():
        return None
    year = int(text[:4])
    if not (1900 <= year <= 2100):
        return None
    return f"{year // 10 * 10}s"


def facets(artist: str, genres: list[str] | None = None,
           language: str | None = None, era: str | None = None) -> dict:
    """A track's taste-bearing attributes. Unknown facets stay absent."""
    return {
        "artist": artist or "",
        "genres": list(genres or []),
        "language": (language or "").strip().lower() or None,
        "era": era,
    }


# Modes that cannot know what language a track is in. Irish reaches for Gaelic
# and English in the same breath and XP+ reaches for anywhere on earth, so the
# language facet is left unset rather than guessed — a wrong language is worse
# than a missing one, because it teaches the daily run to scout the wrong
# storefront.
_NO_LANGUAGE = {"xp", "irish"}


def facets_from_entry(entry: dict) -> dict:
    """Taste facets for a history entry, whatever mode wrote it.

    Entries written before the unified model stored the mode's own idea of
    "language" — a genre for XP+, a tag for Irish, a year for Timeline. This
    reads those back into their real facets instead of taking them at face
    value, which is what stranded them in the first place.
    """
    if entry.get("facets"):
        return entry["facets"]

    source = entry.get("source") or "daily"
    slot = entry.get("language") or ""
    genres = list(entry.get("genres") or [])

    if source in _NO_LANGUAGE:
        # The slot held the genre or tag it was explored under — real signal.
        if slot and slot not in genres:
            genres.append(slot)
        return facets(entry.get("artist", ""), genres, None, None)

    if source == "timeline":
        # The slot held the release year, and the set is English by construction.
        return facets(entry.get("artist", ""), genres, "english", era_of(slot))

    # Daily picks are fresh releases, so the day they were picked stands in for
    # the release date closely enough to place them in a decade.
    return facets(entry.get("artist", ""), genres, slot,
                  era_of(entry.get("date")))


def reinforce(model: dict, f: dict, amount: float) -> None:
    """Apply a verdict to every facet a track carries.

    Positive amounts build the like tables. Negative amounts decay those toward
    zero AND accumulate into the mirrored avoid tables, so scoring can push a
    disliked artist below neutral instead of merely to zero.
    """
    ensure(model)
    artist, genres = f.get("artist", ""), f.get("genres") or []
    language, era = f.get("language"), f.get("era")

    _bump(model["artists"], artist, amount * W_ARTIST)
    for g in genres:
        _bump(model["genres"], g, amount * W_GENRE)
    if language:
        _bump(model["languages"], language, amount * W_LANGUAGE)
        tables = _lang_tables(model, language)
        _bump(tables["artists"], artist, amount * W_ARTIST)
        for g in genres:
            _bump(tables["genres"], g, amount * W_GENRE)
    if era:
        _bump(model["eras"], era, amount * W_ERA)

    if amount < 0:
        av = model["avoid"]
        _bump(av["artists"], artist, -amount * W_ARTIST)
        for g in genres:
            _bump(av["genres"], g, -amount * W_GENRE)
        if language:
            tables = _lang_tables(model, language, avoid=True)
            _bump(tables["artists"], artist, -amount * W_ARTIST)
            for g in genres:
                _bump(tables["genres"], g, -amount * W_GENRE)

    if amount > 0:
        model["plays"] = model.get("plays", 0) + 1


def _all_tables(model: dict) -> list[dict]:
    tables = [model["artists"], model["genres"], model["languages"],
              model["eras"], model["avoid"]["artists"], model["avoid"]["genres"]]
    for root in (model["by_language"], model["avoid"]["by_language"]):
        for per in root.values():
            tables += [per.get("artists", {}), per.get("genres", {})]
    return tables


def decay(model: dict, factor: float) -> None:
    """Shrink every weight so stale taste fades unless you keep engaging."""
    if factor <= 0:
        return
    ensure(model)
    keep = 1.0 - factor
    for table in _all_tables(model):
        for key in list(table):
            value = round(table[key] * keep, 4)
            if value < 0.01:
                del table[key]
            else:
                table[key] = value


def _share(value: float, top: float) -> float:
    """Where a weight sits between nothing and the top of its table, log-scaled.

    These tables are power-law shaped: the genre table's leader is "pop" at 3942
    — seeded from a whole YouTube library — against a median of 3. Reading a
    weight as value/max therefore collapsed everything outside the mainstream to
    nothing. Afrobeat, with a real 73 behind it, came out at 0.019 and scored
    indistinguishably from a genre never heard of, which is why the daily run
    could only ever recognise your most obvious taste and why XP+ and Irish
    candidates all scored a flat zero. Log keeps the ordering intact and gives
    the long tail back its resolution, and the long tail is most of what the
    model knows.
    """
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / (math.log1p(top) or 1.0))


def _relative(table: dict, key: str) -> float:
    """A key's standing within its table, 0-1."""
    if not table:
        return 0.0
    return _share(table.get((key or "").strip().lower(), 0.0), max(table.values()))


def _relative_best(table: dict, keys: list[str]) -> float:
    if not table or not keys:
        return 0.0
    top = max(table.values())
    return max((_share(table.get((k or "").strip().lower(), 0.0), top)
                for k in keys), default=0.0)


def _backoff(joint: float, marginal: float, language: str | None) -> float:
    """Take the stronger of specific evidence and discounted general evidence.

    Blending the two instead would tax every language that *does* have its own
    data: the global table is normalised against your most-played artist
    overall, so anything outside English scores near zero on it and would drag
    a well-evidenced local match down. Taking the max can only ever lift a
    score above what the per-language table alone would have said, which is the
    point — a discovery from XP+ or Irish stops being invisible.
    """
    if not language:
        return marginal * FALLBACK
    return max(joint, marginal * FALLBACK)


def artist_affinity(model: dict, artist: str, language: str | None = None) -> float:
    marginal = _relative(model.get("artists", {}), artist)
    joint = 0.0
    if language:
        per = (model.get("by_language") or {}).get(language) or {}
        joint = _relative(per.get("artists", {}), artist)
    return _backoff(joint, marginal, language)


def genre_overlap(model: dict, genres: list[str],
                  language: str | None = None) -> float:
    marginal = _relative_best(model.get("genres", {}), genres)
    joint = 0.0
    if language:
        per = (model.get("by_language") or {}).get(language) or {}
        joint = _relative_best(per.get("genres", {}), genres)
    return _backoff(joint, marginal, language)


def _avoidance(avoid_table: dict, like_table: dict, keys: list[str]) -> float:
    """0-1 avoid signal, scaled against your positives so mild stays mild."""
    if not avoid_table or not keys:
        return 0.0
    base = max(like_table.values()) if like_table else 0.0
    scores = []
    for key in keys:
        a = avoid_table.get((key or "").strip().lower(), 0.0)
        if a > 0:
            scores.append(_share(a, max(base, a)))
    return max(scores) if scores else 0.0


def _avoidance_backoff(model: dict, table: str, keys: list[str],
                       language: str | None) -> float:
    """Strongest avoid signal, whether it was recorded here or anywhere.

    Both readings are needed. The global one alone is normalised against your
    most-played artist overall, so a thoroughly disliked Yoruba act reads as
    0.03 and escapes its penalty entirely. The per-language one alone can't see
    that you already rejected the same artist in another language. Taking the
    stronger keeps the old penalty intact and lets a dislike travel.
    """
    per = ((model.get("by_language") or {}).get(language) or {}) if language else {}
    per_avoid = (((model["avoid"].get("by_language") or {}).get(language) or {})
                 if language else {})
    local = _avoidance(per_avoid.get(table, {}), per.get(table, {}), keys)
    overall = _avoidance(model["avoid"].get(table, {}), model.get(table, {}), keys)
    return max(local, overall)


def artist_avoidance(model: dict, artist: str, language: str | None = None) -> float:
    return _avoidance_backoff(model, "artists", [artist], language)


def genre_avoidance(model: dict, genres: list[str],
                    language: str | None = None) -> float:
    return _avoidance_backoff(model, "genres", genres, language)


def is_known_artist(model: dict, artist: str) -> bool:
    return (artist or "").strip().lower() in model.get("artists", {})


def unplaced_artists(model: dict) -> dict:
    """Artists you like that belong to no language — XP+ and Irish finds.

    These are the discoveries the old model stranded. They are the only names
    safe to lend to another language's scout: an artist already filed under
    English is English, and seeding them into the Greek storefront just returns
    English tracks wearing a Greek label.
    """
    placed: set[str] = set()
    for per in (model.get("by_language") or {}).values():
        placed.update(per.get("artists", {}))
    return {a: w for a, w in model.get("artists", {}).items() if a not in placed}


def seed_artists(model: dict, language: str, limit: int) -> list[str]:
    """Top artists to scout for a language, strongest first.

    The language's own names come first — they are what this storefront can
    actually serve. Any shortfall is backfilled with language-unattached
    discoveries, which is how something XP+ or Irish found for you finally
    reaches the daily run instead of dying in the mode that found it.
    """
    per = ((model.get("by_language") or {}).get(language) or {}).get("artists", {})
    ranked = [a for a, _ in sorted(per.items(), key=lambda kv: kv[1], reverse=True)]
    if len(ranked) < limit:
        spare = sorted(unplaced_artists(model).items(),
                       key=lambda kv: kv[1], reverse=True)
        taken = set(ranked)
        ranked += [a for a, _ in spare if a not in taken][:limit - len(ranked)]
    return ranked[:limit]


def affinity(model: dict, f: dict, cfg: dict) -> float:
    """0-1 measure of how much this track looks like something you'd keep.

    The positive half of the score, kept separate from the penalty so a mode can
    decide how much of its ranking comes from taste without also deciding how
    much of your dislikes to honour. Those are different questions: XP+ is meant
    to hand you things you have no history with, but nothing is served by
    handing you something you have already rejected.
    """
    w = cfg["weights"]
    adventurousness = float(cfg.get("adventurousness", 0.35))
    artist, genres = f.get("artist", ""), f.get("genres") or []
    language = f.get("language")

    affin = artist_affinity(model, artist, language)
    overlap = genre_overlap(model, genres, language)

    # Novelty rewards new-to-you artists, but only if they're plausibly your
    # taste (similar-artist seed or matching genres) — not random noise.
    novelty = 0.0
    if not is_known_artist(model, artist):
        plausible = 1.0 if f.get("similar_seed") else overlap
        novelty = plausible * adventurousness

    # Language and era are recorded on every verdict but deliberately not scored.
    # Not for want of signal in them — measured over the graded history you keep
    # 82% of romanian picks and 27% of yoruba ones, 76% of the 2010s and 50% of
    # the 2020s. Scoring that lifts the liked tracks and drops the disliked ones
    # and looks like a clear win, right up until it is trained on the past and
    # tested on the future, where it is worth nothing: AUC 0.7576 against 0.7585,
    # 0.7776 against 0.7771, sign flipping between splits. The information is
    # already in the artists and genres those verdicts also reinforced, so
    # scoring it again only counts the same evidence twice.
    raw = (w["artist_affinity"] * affin
           + w["genre_overlap"] * overlap
           + w["novelty"] * novelty)
    total = w["artist_affinity"] + w["genre_overlap"] + w["novelty"]
    return raw / total if total else 0.0


def penalty(model: dict, f: dict, cfg: dict) -> float:
    """How much this track should be pushed down for resembling your dislikes."""
    w = cfg["weights"]
    language = f.get("language")
    return float(w.get("dislike_penalty", 0.5)) * (
        0.7 * artist_avoidance(model, f.get("artist", ""), language)
        + 0.3 * genre_avoidance(model, f.get("genres") or [], language))


def score(model: dict, f: dict, cfg: dict) -> float:
    """0-1 taste score for one track, from every facet it carries."""
    return round(max(0.0, min(1.0,
                              affinity(model, f, cfg) - penalty(model, f, cfg))), 4)
