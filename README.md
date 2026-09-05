# Daily Fresh Music

Finds newly-released tracks you'll actually like — across **English, Spanish,
French, Korean, Japanese** (and any language you add) — and builds a fresh,
audio-only **YouTube Music** playlist every day. It learns your taste per language
and sharpens itself automatically from the songs you Like.

## How it works

```
seed once ─▶ ┌─────────────────────── daily ───────────────────────┐
             │ feedback  →  scout      →  score      →  publish      │
your history │ (Liked      (fresh new    (unified       (new dated   │
  ─────────▶ │  Music)      releases)     taste match)   playlist)   │
             └──────────────────────────────────────────────────────┘
```

- **One taste model** — a single profile tracking artists, genres, languages and
  eras. Every mode writes to it, so a like in XP+ or Irish counts everywhere. It
  still keeps per-language tables, so your Korean taste never picks your French
  tracks; those tables just fall back on the global one when they're thin.
- **Automatic feedback loop** — a pick you Like within a few days reinforces the
  model; picks you ignore gently fade. No manual rating.
- **Discovery anchored to you** — new-to-you artists surface via Last.fm
  "similar artists" of your favourites, gated by genre match + an adventurousness dial.
- **Free & keyless** — runs on your Mac; new releases via Apple/iTunes (no signup),
  optional Last.fm for discovery, `ytmusicapi` to publish.

## Setup (one time)

1. **Install deps** (a virtualenv is recommended):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Keys** — none required. The new-releases source (Apple/iTunes) is keyless.
   Optionally add a free **Last.fm** key for better discovery + genre matching:
   ```bash
   cp .env.example .env    # then paste LASTFM_API_KEY if you have one
   ```
   Get one at https://www.last.fm/api/account/create (optional).

3. **Authenticate YouTube Music** (creates `browser.json`):
   ```bash
   ytmusicapi browser
   ```
   Follow the prompt (paste request headers from music.youtube.com). Save as
   `browser.json` in this folder.

4. **Verify setup** (auth + a peek at today's releases):
   ```bash
   python -m music_xp.check
   ```

5. **Seed your taste** from your listening history:
   ```bash
   python -m music_xp.seed
   ```

## Run it

```bash
python -m music_xp.main --dry-run   # preview today's picks, publishes nothing
python -m music_xp.main             # build & publish today's playlist
```

## Automate (every morning at 7am)

```bash
cp com.zei.musicxp.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.zei.musicxp.plist
```
Logs land in `data/run.log`.

## Tuning (`config.yaml`)

| Setting | What it does |
|---|---|
| `languages` | Which languages + which country storefronts to scout |
| `release_window_days` | How "fresh" (1 = today only; 3–5 avoids empty days) |
| `picks_per_day` | Playlist size |
| `min_score` | Pickiness gate (higher = stricter) |
| `adventurousness` | 0 = safe/familiar, 1 = exploratory/more discovery |
| `feedback_window_days` | Grace period before an un-liked pick counts as a skip |

## Notes & limits

- `ytmusicapi` is unofficial (YouTube Music has no official write API). This is a
  personal automation — keep it for your own account.
- Accuracy starts ~50% and climbs to ~70–75% over a few weeks of Liking songs.
- Some days yield few non-English day-precise releases — that's what the release
  window and per-language balancing are for.
```
