"""Bootstrap the taste model from your existing YouTube Music history.

Run once (or anytime) to seed the system. It reads your library + play history,
guesses each artist's language from Last.fm tags, and weights artists by how much
you play them. This is the ground truth the daily scorer starts from.
"""
from __future__ import annotations

from .config import load_config, language_names, DATA_DIR
from .lastfm import LastFM
from . import local as local_music
from . import store
from . import taste
from . import ytmusic

# Tag hints -> language. First match wins; falls back to 'english'.
# Order matters: more specific / less ambiguous buckets should come first.
LANG_TAG_HINTS = {
    "korean": ["k-pop", "kpop", "korean", "k-indie", "k-rock", "k-hip hop", "trot"],
    "japanese": ["j-pop", "jpop", "japanese", "j-rock", "anime", "city pop", "vocaloid"],
    "mandarin": ["mandopop", "c-pop", "cpop", "cantopop", "mandarin", "chinese",
                 "taiwanese", "hokkien"],
    "spanish": ["spanish", "latin", "reggaeton", "latino", "flamenco", "bachata",
                "cumbia", "banda", "corridos", "ranchera", "mariachi"],
    "portuguese": ["brazilian", "mpb", "sertanejo", "fado", "funk carioca", "bossa nova",
                   "pagode", "forró", "portuguese", "samba", "kizomba"],
    "french": ["french", "chanson", "francais", "française", "variété française", "zouk"],
    "german": ["german", "deutschrap", "schlager", "neue deutsche welle", "krautrock",
               "deutsch"],
    "dutch": ["dutch", "nederpop", "nederlandstalig", "nederhop", "levenslied"],
    "greek": ["greek", "laiko", "rebetiko", "entechno", "greek pop"],
    "romanian": ["romanian", "manele", "muzica populara"],
    "serbian": ["serbian", "turbo-folk", "turbofolk", "ex-yu", "balkan", "narodna"],
    "albanian": ["albanian", "shqip", "tallava"],
    "armenian": ["armenian", "rabiz"],
    "arabic": ["arabic", "khaleeji", "raï", "rai", "shaabi", "mahraganat", "egyptian",
               "levantine", "arab pop"],
    "thai": ["thai", "luk thung", "mor lam", "t-pop", "thai pop"],
    "vietnamese": ["vietnamese", "v-pop", "vpop", "nhac tre"],
    "yoruba": ["yoruba", "fuji", "juju", "afrobeats", "afrobeat", "afropop"],
    "hausa": ["hausa", "nanaye"],
    "mandinka": ["mandinka", "mande", "griot", "mbalax"],
    "fulani": ["fulani", "fula", "pulaar"],
    "persian": ["persian", "iranian", "farsi", "pop-e irani"],
    "afar": ["afar", "afaraf"],
}


def guess_language(tags: list[str], available: list[str],
                   unknown: str | None = None) -> str | None:
    """Which of your languages these tags point at, or `unknown` if none do.

    Seeding wants a fallback — every artist in your library has to land
    somewhere. Learning a like you gave in the app wants None: a wrong language
    teaches the daily run to scout the wrong storefront, and the artist and
    genre facets carry the signal either way.
    """
    tagset = {t.lower() for t in tags}
    for language, hints in LANG_TAG_HINTS.items():
        if language in available and any(h in tagset for h in hints):
            return language
    return unknown


def run() -> None:
    cfg = load_config()
    available = language_names(cfg)
    lf = LastFM(cfg["_env"]["lastfm_key"], cache_path=DATA_DIR / "lastfm_cache.json")
    yt = ytmusic.data_client()

    # Only look up genres (a network call each) for artists with a real signal —
    # not the long tail of one-off history plays. Speeds seeding ~5x and keeps it
    # robust. Tail artists still count, just default-classified.
    tag_signal_threshold = 2.0

    print("Reading ALL your saved music (liked, playlists, followed artists, "
          "library, history)…")
    counts = ytmusic.taste_artist_counts(yt, verbose=True)

    local_dirs = cfg.get("local_music_dirs") or []
    if local_dirs:
        print("Reading your offline collection…")
        for artist, w in local_music.local_artist_counts(local_dirs, verbose=True).items():
            counts[artist] = counts.get(artist, 0.0) + w

    if not counts:
        print("No artists found. Like some songs / save playlists first, then re-run.")
        return

    model = store.load_taste()
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    to_classify = sum(1 for _, s in ranked if s >= tag_signal_threshold)
    print(f"\nFound {len(counts)} artists. Genre-classifying top {to_classify} "
          f"(fetching from Last.fm, cached)…")

    done = 0
    fallback = "english" if "english" in available else available[0]
    for artist, signal in ranked:
        if lf.enabled and signal >= tag_signal_threshold:
            tags = lf.artist_tags(artist)
            done += 1
            if done % 50 == 0:
                print(f"    …{done}/{to_classify} classified")
        else:
            tags = []
        language = guess_language(tags, available, fallback)
        weight = 1.0 + min(signal, 40) * 0.2
        taste.reinforce(model, taste.facets(artist, tags, language), weight)

    lf.flush()
    store.save_taste(model)
    for language in available:
        per = (model.get("by_language") or {}).get(language) or {}
        print(f"  {language:10s}: {len(per.get('artists', {}))} artists")
    print("Seed complete → data/taste.json")


if __name__ == "__main__":
    run()
