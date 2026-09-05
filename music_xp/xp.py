"""XP+ — the out-of-comfort-zone button.

The daily run (main.py) plays to your taste. This does the opposite on purpose:
it hunts genres, artists, and languages your taste model has *never* seen, from
any era (no freshness window), and pushes them to a brand-new "XP{n}" playlist —
a fresh surprise every press. Likes still teach the model, so exploring here
gradually widens what the daily run will reach for.

  python -m music_xp.xp        # scout + publish the next XP{n} playlist
"""
from __future__ import annotations

import random
from itertools import zip_longest

from . import score as scoring, sets, store
from .config import load_config
from .lastfm import LastFM

STATE_FILE = "xp_state.json"

# A deliberately wide net of genres spanning cultures and languages. Each press
# samples a handful you don't already have, so the surprise reaches for corners
# of the world you haven't heard — flamenco, fado, enka, gqom, qawwali, and on.
GENRE_POOL = [
    "afrobeat", "highlife", "amapiano", "gqom", "kwaito", "coupe-decale",
    "flamenco", "fado", "rebetiko", "tango", "bossa nova", "samba", "forro",
    "cumbia", "salsa", "vallenato", "bachata", "reggaeton", "dembow",
    "enka", "city pop", "shibuya-kei", "j-jazz", "min'yo",
    "gamelan", "dangdut", "luk thung", "morlam", "cai luong",
    "qawwali", "ghazal", "bhangra", "carnatic", "hindustani", "filmi",
    "raï", "chaabi", "gnawa", "dabke", "mizrahi",
    "throat singing", "mongolian folk", "tuvan", "khoomei",
    "klezmer", "balkan brass", "turbo-folk", "manele", "sevdah",
    "fado", "morna", "benga", "taarab", "soukous", "mbalax",
    "zouk", "kompa", "calypso", "soca", "chutney",
    "polka", "yodeling", "schlager", "chanson", "sea shanty",
    "bluegrass", "cajun", "zydeco", "appalachian", "old-time",
    "gospel", "delta blues", "dixieland", "ragtime",
    "krautrock", "shoegaze", "post-rock", "math rock", "dark ambient",
    "dub", "dancehall", "ragga", "grime", "baile funk",
    "gabber", "psytrance", "vaporwave", "witch house", "phonk",
    "noise", "drone", "free jazz", "spiritual jazz", "afro-cuban jazz",
]

# Where each genre comes from — (place, country, lat, lon). The XP+ overlay flies
# a world tour along these while it explores, and it doubles as the answer to
# "where on earth did this come from?" for anything the run turns up.
GENRE_HOME = {
    "afrobeat": ("Lagos", "Nigeria", 6.52, 3.38),
    "highlife": ("Accra", "Ghana", 5.60, -0.19),
    "amapiano": ("Johannesburg", "South Africa", -26.20, 28.05),
    "gqom": ("Durban", "South Africa", -29.86, 31.02),
    "kwaito": ("Soweto", "South Africa", -26.27, 27.86),
    "coupe-decale": ("Abidjan", "Ivory Coast", 5.35, -4.02),
    "flamenco": ("Seville", "Spain", 37.39, -5.98),
    "fado": ("Lisbon", "Portugal", 38.72, -9.14),
    "rebetiko": ("Athens", "Greece", 37.98, 23.73),
    "tango": ("Buenos Aires", "Argentina", -34.60, -58.38),
    "bossa nova": ("Rio de Janeiro", "Brazil", -22.91, -43.17),
    "samba": ("Salvador", "Brazil", -12.97, -38.50),
    "forro": ("Recife", "Brazil", -8.05, -34.90),
    "cumbia": ("Barranquilla", "Colombia", 10.96, -74.80),
    "salsa": ("Havana", "Cuba", 23.11, -82.37),
    "vallenato": ("Valledupar", "Colombia", 10.46, -73.25),
    "bachata": ("Santo Domingo", "Dominican Republic", 18.49, -69.90),
    "reggaeton": ("San Juan", "Puerto Rico", 18.47, -66.11),
    "dembow": ("Santiago", "Dominican Republic", 19.45, -70.70),
    "enka": ("Tokyo", "Japan", 35.68, 139.69),
    "city pop": ("Yokohama", "Japan", 35.44, 139.64),
    "shibuya-kei": ("Shibuya", "Japan", 35.66, 139.70),
    "j-jazz": ("Osaka", "Japan", 34.69, 135.50),
    "min'yo": ("Aomori", "Japan", 40.82, 140.75),
    "gamelan": ("Yogyakarta", "Indonesia", -7.80, 110.36),
    "dangdut": ("Jakarta", "Indonesia", -6.21, 106.85),
    "luk thung": ("Bangkok", "Thailand", 13.76, 100.50),
    "morlam": ("Khon Kaen", "Thailand", 16.44, 102.83),
    "cai luong": ("Ho Chi Minh City", "Vietnam", 10.82, 106.63),
    "qawwali": ("Lahore", "Pakistan", 31.55, 74.34),
    "ghazal": ("Lucknow", "India", 26.85, 80.95),
    "bhangra": ("Amritsar", "India", 31.63, 74.87),
    "carnatic": ("Chennai", "India", 13.08, 80.27),
    "hindustani": ("Varanasi", "India", 25.32, 82.97),
    "filmi": ("Mumbai", "India", 19.08, 72.88),
    "raï": ("Oran", "Algeria", 35.70, -0.63),
    "chaabi": ("Casablanca", "Morocco", 33.57, -7.59),
    "gnawa": ("Essaouira", "Morocco", 31.51, -9.77),
    "dabke": ("Beirut", "Lebanon", 33.89, 35.50),
    "mizrahi": ("Tel Aviv", "Israel", 32.08, 34.78),
    "throat singing": ("Altai", "Mongolia", 46.37, 96.26),
    "mongolian folk": ("Ulaanbaatar", "Mongolia", 47.89, 106.91),
    "tuvan": ("Ak-Dovurak", "Russia", 51.18, 90.60),
    "khoomei": ("Kyzyl", "Russia", 51.72, 94.45),
    "klezmer": ("Kraków", "Poland", 50.06, 19.94),
    "balkan brass": ("Guča", "Serbia", 43.78, 20.23),
    "turbo-folk": ("Belgrade", "Serbia", 44.79, 20.45),
    "manele": ("Bucharest", "Romania", 44.43, 26.10),
    "sevdah": ("Sarajevo", "Bosnia", 43.85, 18.41),
    "morna": ("Mindelo", "Cape Verde", 16.89, -24.98),
    "benga": ("Nairobi", "Kenya", -1.29, 36.82),
    "taarab": ("Zanzibar", "Tanzania", -6.16, 39.19),
    "soukous": ("Kinshasa", "DR Congo", -4.44, 15.27),
    "mbalax": ("Dakar", "Senegal", 14.72, -17.47),
    "zouk": ("Pointe-à-Pitre", "Guadeloupe", 16.24, -61.53),
    "kompa": ("Port-au-Prince", "Haiti", 18.59, -72.31),
    "calypso": ("Port of Spain", "Trinidad", 10.65, -61.50),
    "soca": ("San Fernando", "Trinidad", 10.28, -61.47),
    "chutney": ("Georgetown", "Guyana", 6.80, -58.15),
    "polka": ("Prague", "Czechia", 50.08, 14.44),
    "yodeling": ("Appenzell", "Switzerland", 47.33, 9.41),
    "schlager": ("Cologne", "Germany", 50.94, 6.96),
    "chanson": ("Paris", "France", 48.86, 2.35),
    "sea shanty": ("Falmouth", "England", 50.15, -5.07),
    "bluegrass": ("Lexington", "Kentucky", 38.04, -84.50),
    "cajun": ("Lafayette", "Louisiana", 30.22, -92.02),
    "zydeco": ("Opelousas", "Louisiana", 30.53, -92.08),
    "appalachian": ("Asheville", "North Carolina", 35.60, -82.55),
    "old-time": ("Galax", "Virginia", 36.66, -80.92),
    "gospel": ("Chicago", "Illinois", 41.85, -87.65),
    "delta blues": ("Clarksdale", "Mississippi", 34.20, -90.57),
    "dixieland": ("New Orleans", "Louisiana", 29.95, -90.07),
    "ragtime": ("St. Louis", "Missouri", 38.63, -90.20),
    "krautrock": ("Düsseldorf", "Germany", 51.23, 6.78),
    "shoegaze": ("Reading", "England", 51.45, -0.97),
    "post-rock": ("Montreal", "Canada", 45.50, -73.57),
    "math rock": ("Louisville", "Kentucky", 38.25, -85.76),
    "dark ambient": ("Stockholm", "Sweden", 59.33, 18.07),
    "dub": ("Kingston", "Jamaica", 17.97, -76.79),
    "dancehall": ("Spanish Town", "Jamaica", 17.99, -76.95),
    "ragga": ("Montego Bay", "Jamaica", 18.47, -77.92),
    "grime": ("London", "England", 51.51, -0.13),
    "baile funk": ("Rio de Janeiro", "Brazil", -22.87, -43.28),
    "gabber": ("Rotterdam", "Netherlands", 51.92, 4.48),
    "psytrance": ("Goa", "India", 15.30, 74.12),
    "vaporwave": ("Los Angeles", "California", 34.05, -118.24),
    "witch house": ("Portland", "Oregon", 45.52, -122.68),
    "phonk": ("Memphis", "Tennessee", 35.15, -90.05),
    "noise": ("Kyoto", "Japan", 35.01, 135.77),
    "drone": ("Berlin", "Germany", 52.52, 13.40),
    "free jazz": ("New York", "New York", 40.71, -74.01),
    "spiritual jazz": ("Detroit", "Michigan", 42.33, -83.05),
    "afro-cuban jazz": ("Santiago de Cuba", "Cuba", 20.02, -75.83),
}

# A track nobody has heard is not a discovery, it's an accident.
_MIN_LISTENERS = 600
# Retire a genre once it has been tried this many times without a single like.
_STRIKES = 3


def _comfort_zone(model: dict) -> tuple[set[str], set[str]]:
    """Everything you already know: every artist and genre in the taste model."""
    return set(model.get("artists", {})), set(model.get("genres", {}))


def _genre_record(history: list[dict]) -> dict[str, dict]:
    """Your verdict on every genre XP+ has already taken you to."""
    rec: dict[str, dict] = {}
    for h in history:
        if h.get("source") != "xp":
            continue
        for g in h.get("genres", []):
            r = rec.setdefault(g, {"liked": 0, "judged": 0, "tried": 0})
            r["tried"] += 1
            if h.get("outcome") == "liked":
                r["liked"] += 1
            if h.get("outcome") in ("liked", "disliked", "skipped"):
                r["judged"] += 1
    return rec


def _pick_genres(rng: random.Random, record: dict[str, dict],
                 n: int = 7) -> list[str]:
    """Choose where to go next, remembering how the last trips went.

    A genre you never warmed to is retired; one that produced a like earns a
    return visit (with different artists); everything else is unexplored
    ground, which is what XP+ is for.
    """
    pool = sorted(set(GENRE_POOL))
    fresh, loved = [], []
    for g in pool:
        r = record.get(g)
        if not r:
            fresh.append(g)
        elif r["liked"]:
            loved.append(g)
        elif r["judged"] < _STRIKES:
            fresh.append(g)          # not enough evidence to write it off yet
    rng.shuffle(fresh)
    rng.shuffle(loved)
    keep = min(2, n // 3, len(loved))          # mostly new ground, some proven
    chosen = loved[:keep] + fresh[:n - keep]
    if len(chosen) < n:                        # nearly everything is retired
        chosen += [g for g in loved[keep:] if g not in chosen][:n - len(chosen)]
    rng.shuffle(chosen)
    return chosen


def _gather(
    lf: LastFM, rng: random.Random, skip_artists: set[str],
    seen_pairs: set[tuple[str, str]], genres: list[str],
    target: int, artists_per_genre: int = 5,
) -> list[dict]:
    """Collect candidate tracks from genres outside your comfort zone."""
    return sets.explore(
        lf, rng, tags=genres, skip_artists=skip_artists, seen_pairs=seen_pairs,
        target=target, min_listeners=_MIN_LISTENERS,
        artists_per_tag=artists_per_genre,
        # The artist has to actually carry the genre's own tag. Nothing looser
        # works here: XP+ walks pages like "polka" and "gnawa" where a single
        # mislabelled stray is enough to turn the trip into ordinary pop.
        qualifies=lambda tag, artist_tags: (
            sets.norm(tag) in {sets.norm(t) for t in artist_tags}),
        genres_for=lambda tag, artist_tags: [tag])


def run(dry_run: bool = False) -> None:
    cfg = load_config()
    lf = LastFM(cfg["_env"]["lastfm_key"])
    if not lf.enabled:
        raise SystemExit("XP+ needs a Last.fm API key (LASTFM_API_KEY) to explore.")

    model = store.load_taste()
    history = store.load_history()
    dislikes = store.load_dislikes()
    comfort_artists, _known_genres = _comfort_zone(model)
    # Every artist you've already been shown is spent: going back to them only
    # digs deeper into their catalogue, which is how discovery gets worse the
    # more you press. Your comfort zone and your dislikes are out too.
    skip_artists = comfort_artists | {h.get("artist", "").lower()
                                      for h in history if h.get("artist")}
    seen_pairs = {(h.get("artist", "").lower(), h.get("title", "").lower())
                  for h in history}
    seen_ids = {h["video_id"] for h in history if h.get("video_id")} | set(dislikes)

    target = int(cfg.get("picks_per_day", 30))
    rng = random.Random()  # unseeded → genuinely different each press

    print("XP+ — reaching outside your comfort zone…")
    record = _genre_record(history)
    genres = _pick_genres(rng, record)
    retired = sorted(g for g, r in record.items()
                     if not r["liked"] and r["judged"] >= _STRIKES)
    if retired:
        print(f"Retired (you didn't take to them): {', '.join(retired)}")
    print(f"Adventure genres: {', '.join(genres)}")
    candidates = _gather(lf, rng, skip_artists, seen_pairs, genres, target)
    lf.flush()
    if not candidates:
        print("Couldn't find anything new to explore right now. Try again.")
        return

    # Best first *within* each genre, then one genre after another. Ordering
    # globally would hand the whole playlist to whichever genre happens to be
    # world-famous, and the point of the trip is that it visits everywhere.
    candidates = scoring.rank(candidates, model, cfg, "xp")
    by_genre: dict[str, list[dict]] = {}
    for c in candidates:
        by_genre.setdefault(c["language"], []).append(c)
    candidates = [c for row in zip_longest(*by_genre.values())
                  for c in row if c]
    n, commit = sets.next_number(STATE_FILE)
    name = f"XP{n}"
    added = sets.publish(
        candidates, name=name, source="xp", target=target,
        seen_ids=seen_ids, history=history, dry_run=dry_run,
        privacy=cfg.get("playlist_privacy", "PRIVATE"),
        desc=("Out-of-comfort-zone discovery by Music XP — genres, artists and "
              "languages you've never had. Like what grabs you to widen your "
              "taste."),
        blurb="Tracks you've never heard, waiting.")
    if added and not dry_run:
        commit()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="XP+ out-of-comfort-zone discovery")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what it would explore without publishing")
    run(dry_run=ap.parse_args().dry_run)
