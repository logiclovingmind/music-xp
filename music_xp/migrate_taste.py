"""One-shot: fold data/profiles.json into the unified data/taste.json.

Run once. Keys that name a configured language become that language's detail
tables and also feed the global ones. Keys that don't — the 46 genre and year
profiles that XP+, Irish and Timeline created because they had nowhere else to
write — feed the global tables only, which is what brings their likes back into
play. Year keys additionally become era weight.

A straight replay of history.json would have been simpler but wrong: the bulk of
the taste came from seed.py reading your YouTube library and local music folder,
and none of that was ever written to history. Migrating the profiles preserves
it, and the profiles already have every graded verdict applied to them.

  python -m music_xp.migrate_taste            # write data/taste.json
  python -m music_xp.migrate_taste --dry-run  # report only
"""
from __future__ import annotations

import argparse

from . import store, taste
from .config import language_names, load_config


def _merge(into: dict, source: dict) -> None:
    for key, weight in (source or {}).items():
        key = key.strip().lower()
        if key:
            into[key] = round(into.get(key, 0.0) + float(weight), 4)


def _seed_eras(model: dict, history: list[dict], stats: dict) -> None:
    """Learn the era table from graded picks.

    The profiles never held a release date, so era has to come from history —
    and it has to come from somewhere, because a facet the model knows nothing
    about contributes nothing and every candidate carrying it looks worse for
    it. Only the era facet is taken here; artists and genres already arrived
    with the profiles and would double-count.
    """
    weights = {"liked": 1.5, "disliked": -1.5, "skipped": -0.35}
    for entry in history:
        amount = weights.get(entry.get("outcome"))
        if amount is None:
            continue
        era = taste.facets_from_entry(entry).get("era")
        if era:
            taste._bump(model["eras"], era, amount * taste.W_ERA)
            stats["era_verdicts"] += 1


def migrate(profiles: dict, languages: set[str],
            history: list[dict] | None = None) -> tuple[dict, dict]:
    model = taste.empty()
    stats = {"languages": 0, "orphans": 0, "artists_recovered": 0,
             "genres_recovered": 0, "eras": 0, "era_verdicts": 0}

    for key, prof in profiles.items():
        artists = prof.get("artists", {}) or {}
        genres = prof.get("genres", {}) or {}
        avoid = prof.get("avoid") or {}
        avoid_artists = avoid.get("artists", {}) or {}
        avoid_genres = avoid.get("genres", {}) or {}

        _merge(model["artists"], artists)
        _merge(model["genres"], genres)
        _merge(model["avoid"]["artists"], avoid_artists)
        _merge(model["avoid"]["genres"], avoid_genres)
        model["plays"] = model.get("plays", 0) + int(prof.get("plays", 0) or 0)

        if key in languages:
            stats["languages"] += 1
            per = taste._lang_tables(model, key)
            _merge(per["artists"], artists)
            _merge(per["genres"], genres)
            if avoid_artists or avoid_genres:
                per_avoid = taste._lang_tables(model, key, avoid=True)
                _merge(per_avoid["artists"], avoid_artists)
                _merge(per_avoid["genres"], avoid_genres)
            # How much you like a language, proxied by the weight standing
            # behind its artists — there was never an explicit signal for this.
            total = sum(artists.values())
            if total:
                taste._bump(model["languages"], key, round(total * 0.1, 4))
            continue

        # An orphan. Its signal was real; only its filing was wrong.
        stats["orphans"] += 1
        stats["artists_recovered"] += len(artists)
        stats["genres_recovered"] += len(genres)
        era = taste.era_of(key)
        if era:
            stats["eras"] += 1
            weight = sum(artists.values()) or float(prof.get("plays", 0) or 0)
            if weight:
                taste._bump(model["eras"], era, round(weight * 0.1, 4))
        else:
            # The key itself is the genre XP+ or Irish explored under.
            taste._bump(model["genres"], key, 0.0)

    _seed_eras(model, history or [], stats)
    return model, stats


def run(dry_run: bool = False) -> None:
    cfg = load_config()
    languages = set(language_names(cfg))
    profiles = store.load_profiles()
    if not profiles:
        raise SystemExit("No data/profiles.json to migrate.")

    model, stats = migrate(profiles, languages, store.load_history())

    print(f"Profiles read      : {len(profiles)}")
    print(f"  language profiles: {stats['languages']}")
    print(f"  orphan profiles  : {stats['orphans']} "
          f"({stats['eras']} of them years)")
    print(f"Recovered into the global tables: "
          f"{stats['artists_recovered']} artist weights, "
          f"{stats['genres_recovered']} genre weights")
    print()
    print(f"Unified model:")
    print(f"  artists          : {len(model['artists'])}")
    print(f"  genres           : {len(model['genres'])}")
    print(f"  languages        : {len(model['languages'])}")
    print(f"  eras             : {len(model['eras'])}  "
          f"{sorted(model['eras'])}  (from {stats['era_verdicts']} verdicts)")
    print(f"  per-language sets : {len(model['by_language'])}")
    print(f"  avoid artists    : {len(model['avoid']['artists'])}")

    if dry_run:
        print("\n(dry-run: data/taste.json not written)")
        return

    store.save_taste(model)
    print("\nWrote data/taste.json")
    print("data/profiles.json left in place as a backup — nothing reads it now.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fold profiles.json into taste.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be migrated without writing")
    run(dry_run=ap.parse_args().dry_run)
