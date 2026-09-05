"""Order a day's picks into a listenable arc instead of raw score order.

We have no true audio features (no Spotify), so 'energy' is a heuristic read off
each track's genre tags. Picks are sequenced to ease in, build to a peak, then
cool down — and same-artist tracks are spread apart so the list feels varied.
"""
from __future__ import annotations

# Rough 0-1 energy by genre tag. Coarse on purpose — it only needs to sort a
# chill acoustic track below a techno banger, not be scientifically exact.
_GENRE_ENERGY = {
    "ambient": 0.10, "classical": 0.15, "acoustic": 0.20, "lo-fi": 0.20,
    "lofi": 0.20, "ballad": 0.25, "chill": 0.25, "singer-songwriter": 0.30,
    "folk": 0.30, "jazz": 0.40, "soul": 0.40, "r&b": 0.45, "rnb": 0.45,
    "indie": 0.50, "country": 0.50, "reggae": 0.50, "blues": 0.45,
    "pop": 0.60, "funk": 0.65, "latin": 0.65, "disco": 0.70, "hip hop": 0.70,
    "hip-hop": 0.70, "rap": 0.70, "electronic": 0.70, "rock": 0.70,
    "afrobeats": 0.70, "afrobeat": 0.70, "k-pop": 0.75, "reggaeton": 0.75,
    "trap": 0.75, "dance": 0.80, "house": 0.80, "edm": 0.85, "techno": 0.85,
    "punk": 0.85, "drum and bass": 0.90, "dubstep": 0.90, "metal": 0.90,
    "hardcore": 0.95,
}
_DEFAULT_ENERGY = 0.50


def track_energy(genres: list[str]) -> float:
    """Mean energy of a track's recognized genre tags (0.5 if none match)."""
    vals = [_GENRE_ENERGY[g.strip().lower()]
            for g in (genres or []) if g.strip().lower() in _GENRE_ENERGY]
    return sum(vals) / len(vals) if vals else _DEFAULT_ENERGY


def _spread(seq: list[dict]) -> list[dict]:
    """Nudge tracks so the same artist isn't back-to-back, keeping order close."""
    res = list(seq)
    for i in range(1, len(res)):
        if res[i]["artist"].lower() == res[i - 1]["artist"].lower():
            for j in range(i + 1, len(res)):
                if res[j]["artist"].lower() != res[i - 1]["artist"].lower():
                    res[i], res[j] = res[j], res[i]
                    break
    return res


def arrange(picks: list[dict]) -> list[dict]:
    """Reorder picks into a rise-peak-fall energy arc, artists spread out."""
    if len(picks) < 3:
        return list(picks)
    items = sorted(picks, key=lambda p: track_energy(p.get("genres", [])))
    # Even indices climb, odd indices come back down → a single energy hump.
    rising = items[0::2]
    falling = items[1::2][::-1]
    return _spread(rising + falling)
