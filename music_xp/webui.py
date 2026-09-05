"""Local web dashboard for the daily fresh-music system.

A zero-dependency control panel (Python's stdlib http.server only, so it runs on
the pinned old-hardware setup). View today's picks, history, and the per-language
taste tables; adjust settings; toggle languages; run the builder; thumbs-down
tracks you don't want.

    python -m music_xp.webui         # then open http://localhost:8000

Config edits are written to data/overrides.json (merged on top of config.yaml at
load time), so your commented config.yaml is never rewritten.
"""
from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import yaml

from . import artistart
from . import download as downloader
from . import library
from . import store
from .config import (CONFIG_PATH, OVERRIDABLE, ROOT, load_config,
                     load_overrides, save_overrides)

PORT = 8000
_run_lock = threading.Lock()
ASSETS_DIR = Path(__file__).parent / "assets"

# History sources you build on demand. Each owns a tab listing its playlists, so
# none of them belong in the daily "Today's Picks" list.
ON_DEMAND = ("xp", "irish", "timeline")

# Nor does anything Music XP never picked. "youtube" entries are likes you gave
# in the app yourself; they belong in Liked and in the totals, not in a list of
# what the system chose for you.
NOT_A_PICK = ON_DEMAND + ("youtube",)

# Shared, live snapshot of the current/last download run so the Downloads tab
# can show per-track progress regardless of which client started it.
_dl_lock = threading.Lock()
_dl_state: dict = {"active": False, "total": 0, "items": [], "updated": 0.0}

# Downloads are serialized through one worker so a second request queues behind
# the first instead of being rejected. Each job carries an output queue; its HTTP
# handler streams those lines back to whoever asked for it.
_dl_jobs: "queue.Queue[dict]" = queue.Queue()
_dl_worker_started = False
_dl_worker_lock = threading.Lock()


def _dl_worker_loop() -> None:
    while True:
        job = _dl_jobs.get()
        out: queue.Queue = job["out"]
        # Hold _run_lock during the actual work so a download and a daily run
        # never overlap (both are heavy and both touch history.json).
        _run_lock.acquire()
        try:
            for line in downloader.download(date_str=job["date"],
                                            only_ids=job["ids"],
                                            progress=_dl_progress):
                out.put(line)
        except Exception as e:  # never let one job kill the worker
            out.put(f"download error: {e}\n")
        finally:
            _run_lock.release()
            out.put(None)  # sentinel: this job is finished


def _ensure_dl_worker() -> None:
    global _dl_worker_started
    with _dl_worker_lock:
        if _dl_worker_started:
            return
        _dl_worker_started = True
        threading.Thread(target=_dl_worker_loop, daemon=True).start()


# Library scans run on one background thread; the tab polls this snapshot.
_lib_lock = threading.Lock()
_lib_state: dict = {"active": False, "done": 0, "total": 0, "error": ""}


def _lib_scan_bg() -> None:
    def progress(p: dict) -> None:
        with _lib_lock:
            _lib_state["done"] = p["done"]
            _lib_state["total"] = p["total"]
    try:
        library.scan(progress)
    except Exception as e:
        with _lib_lock:
            _lib_state["error"] = str(e)
    finally:
        with _lib_lock:
            _lib_state["active"] = False


def _start_lib_scan() -> dict:
    with _lib_lock:
        if _lib_state["active"]:
            return {"ok": True, "already": True}
        _lib_state.update({"active": True, "done": 0, "total": 0, "error": ""})
    threading.Thread(target=_lib_scan_bg, daemon=True).start()
    return {"ok": True}


def _dl_progress(ev: dict) -> None:
    """Fold a download event (from downloader.download) into _dl_state."""
    with _dl_lock:
        kind = ev.get("type")
        if kind == "init":
            _dl_state["items"] = ev["items"]
            _dl_state["total"] = ev["total"]
            _dl_state["active"] = True
        elif kind in ("status", "pct"):
            items = _dl_state["items"]
            i = ev.get("index", -1)
            if 0 <= i < len(items):
                if "status" in ev:
                    items[i]["status"] = ev["status"]
                if "pct" in ev:
                    items[i]["pct"] = ev["pct"]
        elif kind == "done":
            _dl_state["active"] = False
        _dl_state["updated"] = time.time()


# ── data assembly ─────────────────────────────────────────────────────────────
def _raw_languages() -> list[dict]:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f).get("languages", [])


def _build_state() -> dict:
    cfg = load_config()
    overrides = load_overrides()
    disabled = {d.lower() for d in overrides.get("disabled_languages", [])}
    model = store.load_taste()
    history = store.load_history()
    dislikes = store.load_dislikes()

    languages = []
    for lang in _raw_languages():
        name = lang["name"]
        per = (model.get("by_language") or {}).get(name) or {}
        artists = per.get("artists", {})
        top = sorted(artists.items(), key=lambda kv: kv[1], reverse=True)[:6]
        languages.append({
            "name": name,
            "markets": lang.get("markets", []),
            "enabled": name.lower() not in disabled,
            "artist_count": len(artists),
            "top_artists": [{"artist": a, "weight": round(w, 1)} for a, w in top],
        })

    # Today's picks = entries from the most recent pick date. Every on-demand
    # mode has its own tab, so keep those out of the daily list.
    daily = [h for h in history if h.get("source") not in NOT_A_PICK]
    latest = max((h.get("date", "") for h in daily), default="")
    today = [_pick_view(h, dislikes) for h in daily if h.get("date") == latest]
    today.sort(key=lambda p: p["score"], reverse=True)

    def _sets_for(source: str) -> list[dict]:
        """Every playlist one mode has built, newest set first."""
        groups: dict[str, dict] = {}
        for h in history:
            if h.get("source") != source:
                continue
            name = h.get("xp_set") or source
            g = groups.setdefault(name, {"name": name, "playlist_id": None,
                                         "date": h.get("date", ""), "picks": []})
            g["playlist_id"] = h.get("playlist_id") or g["playlist_id"]
            g["picks"].append(_pick_view(h, dislikes))
        for g in groups.values():
            g["picks"].sort(key=lambda p: p["score"], reverse=True)

        def num(n: str) -> int:
            m = re.search(r"(\d+)", n or "")
            return int(m.group(1)) if m else 0

        return sorted(groups.values(), key=lambda g: num(g["name"]), reverse=True)

    # History grouped by date, newest first.
    by_date: dict[str, dict] = {}
    for h in history:
        d = h.get("date", "?")
        g = by_date.setdefault(d, {"date": d, "count": 0, "liked": 0,
                                   "disliked": 0, "skipped": 0, "playlist_id": None})
        g["count"] += 1
        g["playlist_id"] = h.get("playlist_id") or g["playlist_id"]
        outcome = h.get("outcome")
        if outcome in ("liked", "disliked", "skipped"):
            g[outcome] += 1
    days = sorted(by_date.values(), key=lambda x: x["date"], reverse=True)[:30]

    liked = [dict(_pick_view(h, dislikes), date=h.get("date", ""))
             for h in history if h.get("outcome") == "liked"]
    liked.sort(key=lambda p: p["date"], reverse=True)

    # Weekly-digest playlists, newest first, each with its keeper tracks.
    digests = []
    for d in sorted(store.load_digests(), key=lambda x: x.get("date", ""),
                    reverse=True):
        digests.append({
            "name": d.get("name", "Digest"),
            "date": d.get("date", ""),
            "days": d.get("days", 7),
            "playlist_id": d.get("playlist_id"),
            "picks": [_pick_view(t, dislikes) for t in d.get("tracks", [])],
        })

    return {
        "config": {k: cfg.get(k) for k in OVERRIDABLE},
        "languages": languages,
        "today": {"date": latest, "picks": today},
        "xp_sets": _sets_for("xp"),
        "irish_sets": _sets_for("irish"),
        "time_sets": _sets_for("timeline"),
        "digests": digests,
        "liked": liked,
        "history": days,
        "dislikes": sorted(dislikes),
    }


def _hero_art(limit: int = 24) -> dict:
    """One photo per artist for today's picks, best-scored artist first. Album
    covers repeat when a day is heavy on one artist, so the hero wall keys off
    the *artist* instead — deduped, and only artists we can find a photo for."""
    picks = _build_state()["today"]["picks"]  # already sorted by score desc
    seen: set[str] = set()
    names: list[str] = []
    for p in picks:
        artist = (p.get("artist") or "").strip()
        if not artist:
            continue
        # "rares" and "rares, Tzanca Uraganu" are the same face on the wall, so a
        # credit is skipped once any of its members has already been placed.
        members = {m.strip().lower() for m in re.split(r",|&|\bfeat\.?\b|\bwith\b|\bx\b",
                                                       artist, flags=re.I) if m.strip()}
        if not members or members & seen:
            continue
        seen |= members
        names.append(artist)
        if len(names) >= limit:
            break
    images = artistart.images_for_artists(names)
    out = []
    used: set[str] = set()  # two collabs can resolve to the same shared member
    for a in names:
        url = images.get(a)
        if url and url not in used:
            used.add(url)
            # Tiles are served pre-shrunk from our own cache: full-HD originals
            # decode too slowly to paint ~100 of them without stuttering.
            out.append({"artist": a, "image": "/api/thumb?u=" + quote(url, "")})
    return {"artists": out}


def _pick_view(h: dict, dislikes: set[str]) -> dict:
    vid = h.get("video_id", "")
    return {
        "video_id": vid,
        "title": h.get("title", ""),
        "artist": h.get("artist_display") or h.get("artist", ""),
        "language": h.get("language", ""),
        "score": round(float(h.get("score", 0.0)), 2),
        "outcome": h.get("outcome") or ("disliked" if vid in dislikes else "pending"),
        "url": f"https://music.youtube.com/watch?v={vid}" if vid else "",
        "source": h.get("source", ""),
        "xp_set": h.get("xp_set", ""),
    }


def _day_picks(date: str) -> dict:
    """All tracks for one history date, highest-scoring first."""
    dislikes = store.load_dislikes()
    picks = [_pick_view(h, dislikes) for h in store.load_history()
             if h.get("date") == date]
    picks.sort(key=lambda p: p["score"], reverse=True)
    return {"date": date, "picks": picks}


def _stats() -> dict:
    """Aggregate insight metrics over the whole pick history."""
    from collections import Counter

    history = store.load_history()
    liked = [h for h in history if h.get("outcome") == "liked"]
    disliked = [h for h in history if h.get("outcome") == "disliked"]
    skipped = [h for h in history if h.get("outcome") == "skipped"]
    graded = len(liked) + len(disliked) + len(skipped)

    # Discovery = liked tracks from an artist you'd never liked before (by date).
    seen: set[str] = set()
    discoveries = 0
    for h in sorted(liked, key=lambda h: h.get("date", "")):
        a = (h.get("artist") or "").strip().lower()
        if a and a not in seen:
            discoveries += 1
        seen.add(a)

    gen, art, langc = Counter(), Counter(), Counter()
    for h in liked:
        for g in (h.get("genres") or []):
            g = (g or "").strip().lower()
            if g:
                gen[g] += 1
        a = (h.get("artist_display") or h.get("artist") or "").strip()
        if a:
            art[a] += 1
        lg = (h.get("language") or "").strip()
        if lg:
            langc[lg] += 1

    def top(counter: Counter, n: int = 8) -> list[dict]:
        return [{"name": k, "count": v} for k, v in counter.most_common(n)]

    dates = sorted({h.get("date", "") for h in history if h.get("date")})
    return {
        "total_picks": len(history),
        "graded": graded,
        "liked": len(liked),
        "disliked": len(disliked),
        "skipped": len(skipped),
        "like_rate": round(len(liked) / graded, 3) if graded else 0.0,
        "skip_rate": round(len(skipped) / graded, 3) if graded else 0.0,
        "discovery_rate": round(discoveries / len(liked), 3) if liked else 0.0,
        "unique_artists": len({(h.get("artist") or "").strip().lower()
                               for h in history if h.get("artist")}),
        "top_genres": top(gen),
        "top_artists": top(art),
        "top_languages": top(langc),
        "active_days": len(dates),
        "first_day": dates[0] if dates else "",
        "last_day": dates[-1] if dates else "",
    }


_yt_liked_cache: dict = {"at": 0.0, "tracks": None}


def _learn_likes(tracks: list[dict]) -> int:
    """Fold likes you gave in the app into the taste model. Never fatal."""
    try:
        from .feedback import learn_outside_likes
        from .lastfm import LastFM
        cfg = load_config()
        model = store.load_taste()
        return learn_outside_likes(cfg, model, LastFM(cfg["_env"]["lastfm_key"]),
                                   tracks)
    except Exception:
        return 0


def _liked_youtube() -> dict:
    """Your YouTube Music Liked songs (playlist LM), cached for 10 minutes."""
    if (_yt_liked_cache["tracks"] is not None
            and time.time() - _yt_liked_cache["at"] < 600):
        return {"tracks": _yt_liked_cache["tracks"], "cached": True}
    try:
        from . import ytdata
        tracks = ytdata.YouTubeData().liked_music_tracks()
        _yt_liked_cache.update(at=time.time(), tracks=tracks)
        # Refreshing here is the fastest route from "I liked something in the
        # app" to the model knowing it — sooner than waiting for tomorrow's run.
        return {"tracks": tracks, "cached": False, "learned": _learn_likes(tracks)}
    except Exception as e:  # no oauth.json / network down — degrade gracefully
        return {"tracks": [], "error": str(e)}


# ── mutations ─────────────────────────────────────────────────────────────────
def _save_config(payload: dict) -> dict:
    overrides = load_overrides()
    for key in OVERRIDABLE:
        if key in payload and payload[key] is not None:
            overrides[key] = payload[key]
    if "disabled_languages" in payload:
        overrides["disabled_languages"] = [
            str(x).lower() for x in payload["disabled_languages"]]
    save_overrides(overrides)
    return {"ok": True, "overrides": overrides}


def _rate_on_youtube(video_id: str, rating: str) -> bool:
    """Best-effort account rating; the local feedback still applies if this fails."""
    try:
        from .ytdata import YouTubeData
        return YouTubeData().rate_video(video_id, rating)
    except (Exception, SystemExit):
        return False


def _rate_on_youtube_bg(video_id: str, rating: str) -> None:
    """Mirror the rating to YouTube off the request thread.

    The round-trip to YouTube (token refresh + /videos/rate) used to block every
    like/dislike POST, making the heart feel sluggish. The local grade is what
    the UI reflects; the YouTube side syncs a beat later in the background.
    """
    threading.Thread(target=_rate_on_youtube, args=(video_id, rating),
                     daemon=True).start()


def _grade(video_id: str, outcome: str, amount: float) -> bool:
    from . import taste
    # Reading, grading and saving is one indivisible act: the daily run appends
    # to the same two files at 07:00, and a rating applied to a copy it never
    # saw would be erased the moment either side saved.
    with store.transaction():
        history = store.load_history()
        model = store.load_taste()
        applied = False
        for entry in history:
            if entry.get("video_id") == video_id and entry.get("outcome") != outcome:
                taste.reinforce(model, taste.facets_from_entry(entry), amount=amount)
                entry["graded"] = True
                entry["outcome"] = outcome
                applied = True
        if applied:
            store.save_history(history)
            store.save_taste(model)
    return applied


def _like(video_id: str) -> dict:
    applied = _grade(video_id, "liked", 1.5)
    _rate_on_youtube_bg(video_id, "like")
    return {"ok": True, "video_id": video_id, "applied": applied, "youtube": "pending"}


def _dislike(video_id: str) -> dict:
    store.add_dislike(video_id)
    applied = _grade(video_id, "disliked", -1.5)
    _rate_on_youtube_bg(video_id, "dislike")
    return {"ok": True, "video_id": video_id, "applied": applied, "youtube": "pending"}


def _clear(video_id: str) -> dict:
    """Back to neutral: revert the taste reinforcement and the YouTube rating."""
    from . import taste
    with store.transaction():
        history = store.load_history()
        model = store.load_taste()
        applied = False
        for entry in history:
            if entry.get("video_id") == video_id and entry.get("outcome") in ("liked", "disliked"):
                undo = -1.5 if entry["outcome"] == "liked" else 1.5
                taste.reinforce(model, taste.facets_from_entry(entry), amount=undo)
                entry["graded"] = False
                entry["outcome"] = "pending"
                applied = True
        if applied:
            store.save_history(history)
            store.save_taste(model)
    store.remove_dislike(video_id)
    _rate_on_youtube_bg(video_id, "none")
    return {"ok": True, "video_id": video_id, "applied": applied, "youtube": "pending"}


# ── keep the display awake during full-screen lyrics ─────────────────────────
# The lyrics view is a fullscreen *web page*, not fullscreen video, so macOS
# never fires its own display-awake assertion (that only happens for real video
# playback). We replicate what players like IINA do: hold a caffeinate
# assertion while the overlay is open, release it when it closes.
_caf_lock = threading.Lock()
_caf: subprocess.Popen | None = None


def _awake(on: bool) -> dict:
    global _caf
    with _caf_lock:
        if on:
            if _caf is None or _caf.poll() is not None:
                try:
                    _caf = subprocess.Popen(
                        ["caffeinate", "-d", "-i"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    return {"on": False, "ok": False}
            return {"on": True, "ok": True}
        if _caf is not None and _caf.poll() is None:
            _caf.terminate()
            try:
                _caf.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _caf.kill()
        _caf = None
        return {"on": False, "ok": True}


# ── lyrics ────────────────────────────────────────────────────────────────────
def _cache_lyrics(key: str, entry: dict) -> dict:
    from . import lyrics
    entry = {k: v for k, v in entry.items() if k not in ("state", "key")}
    entry["ok"] = True
    entry["at"] = time.time()
    return lyrics._put(key, entry)


def _lyr_key(b: dict) -> str:
    from . import lyrics
    return (b.get("key") or b.get("vid")
            or f'{lyrics._fold(b.get("artist", ""))}|'
               f'{lyrics._fold(b.get("title", ""))}').strip()


def _num(v: object) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _lyr_out(entry: dict, key: str) -> dict:
    """One cache entry as the browser wants it.

    The whisper segments stay on disk: they are a second copy of every line and
    only the server ever re-times against them. What the studio needs to know is
    that they exist, so a re-align can be offered without another twenty minutes.
    """
    from . import transcribe
    out = {k: v for k, v in entry.items() if k not in ("whisper", "prev")}
    return dict(out, key=key, state=transcribe.status(key),
                can_transcribe=transcribe.available(),
                has_whisper=bool(entry.get("whisper")),
                has_prev=bool(entry.get("prev")))


def _keep_prev(old: dict, new: dict) -> dict:
    """Stash what is being replaced, so the studio's Revert has somewhere to go.

    One step only. A deeper history would mean carrying every draft of every song
    in a file that is already megabytes, and the mistake worth undoing is always
    the last one.
    """
    if not (old.get("synced") or old.get("plain")):
        return {k: v for k, v in new.items() if k != "prev"}
    return dict(new, prev={"synced": old.get("synced") or [],
                           "plain": old.get("plain") or "",
                           "source": old.get("source") or ""})


def _timed_lyrics(artist: str, title: str, dur: float, vid: str, key: str) -> dict:
    """Lyrics for one track, timed against a transcription if one already exists.

    Words and a clock are separate problems. A source can give us either: flat
    text with no stamps, or (via whisper) stamps whose words are wrong. Whichever
    half is already on hand gets combined here — but earning a *new* clock costs
    twenty minutes of CPU, so that only ever happens when zEi asks for it.
    """
    from . import lyrics, transcribe
    got = lyrics.for_track(artist, title, dur, key=key)
    if got.get("synced"):
        return _lyr_out(got, key)
    sheet = got.get("sheet") or got.get("plain")
    if sheet:
        timed = transcribe.apply_sheet(got, sheet, dur)
        if timed:
            return _lyr_out(_cache_lyrics(key, timed), key)
    return _lyr_out(got, key)


def _paste_lyrics(b: dict) -> dict:
    """Take a lyric sheet typed in by hand and give it the track's timings."""
    from . import lyrics, transcribe
    key, text = _lyr_key(b), (b.get("text") or "").strip()
    if not key.strip("|") or not text:
        return {"error": "need a track and some lyrics"}
    label = "your lyrics, timed against the audio"
    old = lyrics._load().get(key) or {}
    timed = transcribe.apply_sheet(old, text, _num(b.get("dur")), source=label)
    if timed:
        return _lyr_out(_cache_lyrics(key, _keep_prev(old, timed)), key)
    # No clock for this track yet. Keep the words — they are readable as they
    # are — and leave starting a transcription to him.
    kept = _cache_lyrics(key, _keep_prev(old, dict(
        old, sheet=text, sheet_source=label, plain=text,
        source="your lyrics", synced=[])))
    return _lyr_out(kept, key)


def _start_transcribe(b: dict) -> dict:
    """Queue a whisper pass — only ever reached by someone pressing the button."""
    from . import lyrics, transcribe
    key = _lyr_key(b)
    if not key.strip("|"):
        return {"error": "need a track"}
    entry = lyrics._load().get(key) or {}
    if not entry.get("sheet") and entry.get("plain"):
        # Words already in hand are what whisper is being asked to time, not
        # replace, so they are handed to the queue as the sheet.
        _cache_lyrics(key, dict(entry, sheet=entry["plain"],
                                sheet_source=entry.get("source")))
    return {"key": key, "state": transcribe.request(
        key, b.get("artist", ""), b.get("title", ""), b.get("vid", ""),
        _num(b.get("dur")))}


# ── lyrics studio ─────────────────────────────────────────────────────────────
def _lyr_save(b: dict) -> dict:
    """Write lyrics the studio has edited by hand.

    Whatever comes back from here is final: no aligner gets a second opinion on
    timings someone sat and tapped in. The whisper segments are kept regardless,
    since they are the only clock this track will ever have for free.
    """
    from . import lyrics
    key = _lyr_key(b)
    if not key.strip("|"):
        return {"error": "need a track"}
    synced = sorted(
        ({"t": round(max(0.0, _num(l.get("t"))), 2),
          "line": str(l.get("line") or "").strip()}
         for l in (b.get("synced") or []) if isinstance(l, dict)),
        key=lambda l: l["t"])
    plain = (b.get("plain") or "\n".join(l["line"] for l in synced)).strip()
    if not synced and not plain:
        return {"error": "nothing to save"}
    old = lyrics._load().get(key) or {}
    entry = dict(old, synced=synced, plain=plain,
                 source=(b.get("source") or "").strip() or "your lyrics")
    if not synced:
        # Untimed words are still a sheet: if a transcription lands later they
        # are what it should be timed against.
        entry["sheet"], entry["sheet_source"] = plain, "your lyrics"
    else:
        # Timed by hand — drop the sheet so nothing re-aligns over the top.
        entry.pop("sheet", None)
        entry.pop("sheet_source", None)
    return _lyr_out(_cache_lyrics(key, _keep_prev(old, entry)), key)


def _lyr_revert(b: dict) -> dict:
    """Put back whatever the last save replaced."""
    from . import lyrics
    key = _lyr_key(b)
    old = lyrics._load().get(key) or {}
    prev = old.get("prev")
    if not prev:
        return {"error": "nothing to go back to"}
    entry = {k: v for k, v in old.items() if k != "prev"}
    entry.update(synced=prev.get("synced") or [], plain=prev.get("plain") or "",
                 source=prev.get("source") or "")
    return _lyr_out(_cache_lyrics(key, entry), key)


def _lyr_sources(artist: str, title: str, dur: float, key: str) -> dict:
    """The rival matches, live from the services — never the cache.

    Pressing this is how a wrong pick gets corrected, so a cached answer would
    just hand back the wrong one again.
    """
    from . import lyrics
    return {"key": key,
            "items": lyrics.candidates(artist, title, dur)}


# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet console
        pass

    def _json(self, obj: dict, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return {}

    def do_GET(self) -> None:
        if self.path.split("?")[0] in ("/", "/index", "/index.html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(_build_state())
        elif self.path == "/api/liked_youtube":
            self._json(_liked_youtube())
        elif self.path == "/api/hero-art":
            self._json(_hero_art())
        elif self.path == "/api/genre-home":
            from . import xp  # imported lazily: pulls in the YT Music client
            self._json(xp.GENRE_HOME)
        elif self.path.startswith("/api/day"):
            date = (parse_qs(urlparse(self.path).query).get("date") or [""])[0]
            self._json(_day_picks(date))
        elif self.path == "/api/stats":
            self._json(_stats())
        elif self.path == "/api/schedule":
            from . import schedule
            self._json(schedule.status())
        elif self.path.startswith("/api/awake"):
            q = parse_qs(urlparse(self.path).query)
            on = (q.get("on") or ["0"])[0] not in ("0", "", "false", "False")
            self._json(_awake(on))
        elif self.path.startswith("/api/lyrics/sources"):
            from . import lyrics
            q = parse_qs(urlparse(self.path).query)
            one = lambda k: (q.get(k) or [""])[0]
            try:
                dur = float(one("dur") or 0)
            except ValueError:
                dur = 0.0
            key = one("key") or one("vid") or (
                f'{lyrics._fold(one("artist"))}|{lyrics._fold(one("title"))}')
            self._json(_lyr_sources(one("artist"), one("title"), dur, key))
        elif self.path.startswith("/api/lyrics"):
            from . import lyrics
            q = parse_qs(urlparse(self.path).query)
            one = lambda k: (q.get(k) or [""])[0]
            try:
                dur = float(one("dur") or 0)
            except ValueError:
                dur = 0.0
            # Keyed on the video id so replays never re-ask LRCLIB, even though
            # the lookup itself goes out on artist + title.
            key = one("vid") or f'{lyrics._fold(one("artist"))}|{lyrics._fold(one("title"))}'
            self._json(_timed_lyrics(one("artist"), one("title"), dur,
                                     one("vid"), key))
        elif self.path.startswith("/api/transcribe"):
            from . import lyrics, transcribe
            q = parse_qs(urlparse(self.path).query)
            one = lambda k: (q.get(k) or [""])[0]
            key = one("key")
            state = transcribe.status(key)
            if state == "done":
                self._json(dict(lyrics._load().get(key, {}), state=state))
            else:
                self._json({"synced": [], "plain": "", "state": state})
        elif self.path == "/api/downloads":
            with _dl_lock:
                self._json({"active": _dl_state["active"],
                            "total": _dl_state["total"],
                            "updated": _dl_state["updated"],
                            "items": [dict(x) for x in _dl_state["items"]]})
        elif self.path == "/api/library":
            with _lib_lock:
                scan = dict(_lib_state)
            self._json(dict(library.summary(), scan=scan,
                            playlists=library.playlists(),
                            artrev=library.art_rev()))
        elif self.path.startswith("/api/local/audio"):
            self._serve_local_audio(
                (parse_qs(urlparse(self.path).query).get("id") or [""])[0])
        elif self.path.startswith("/api/local/cover"):
            self._serve_local_cover(
                (parse_qs(urlparse(self.path).query).get("id") or [""])[0])
        elif self.path.startswith("/api/thumb"):
            self._serve_thumb((parse_qs(urlparse(self.path).query).get("u")
                               or [""])[0])
        elif self.path.startswith("/assets/"):
            self._serve_asset(self.path[len("/assets/"):])
        else:
            self._json({"error": "not found"}, 404)

    def _serve_local_audio(self, tid: str) -> None:
        """Stream an indexed file, honouring Range so the seek bar works.

        Only ids present in the library index resolve to a path, so this can't
        be pointed at a file outside the folders you added.
        """
        t = library.track(tid)
        path = Path(t["path"]) if t else None
        if not path or not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        partial = False
        rng = self.headers.get("Range", "")
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", library.mime_for(path.suffix))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player seeked or moved on; nothing to clean up

    def _serve_local_cover(self, tid: str) -> None:
        got = library.cover(tid)
        if not got:
            self.send_response(302)
            self.send_header("Location", "/assets/logo-256.png?v=3")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data, ctype = got
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_thumb(self, url: str) -> None:
        # Only images this app resolved may be fetched, so the endpoint can't be
        # used to pull arbitrary URLs through the machine.
        if not artistart.is_known(url):
            self._json({"error": "not found"}, 404)
            return
        try:
            data = artistart.thumb(url)
        except Exception:
            self._json({"error": "thumb failed"}, 502)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        self.end_headers()
        self.wfile.write(data)

    def _serve_asset(self, name: str) -> None:
        # Resolve within ASSETS_DIR only — reject traversal / absolute paths.
        name = name.split("?", 1)[0]  # drop cache-busting query string
        target = (ASSETS_DIR / name).resolve()
        if ASSETS_DIR not in target.parents or not target.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = {"png": "image/png", "svg": "image/svg+xml",
                 "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "ico": "image/x-icon"}.get(target.suffix.lstrip(".").lower(),
                                            "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if self.path in ("/api/lyrics/paste", "/api/lyrics/save",
                         "/api/lyrics/revert", "/api/transcribe/start"):
            res = {"/api/lyrics/paste": _paste_lyrics,
                   "/api/lyrics/save": _lyr_save,
                   "/api/lyrics/revert": _lyr_revert,
                   "/api/transcribe/start": _start_transcribe,
                   }[self.path](self._body())
            self._json(res, 400 if "error" in res else 200)
        elif self.path == "/api/awake":
            self._json(_awake(bool(self._body().get("on"))))
        elif self.path == "/api/config":
            self._json(_save_config(self._body()))
        elif self.path == "/api/schedule":
            from . import schedule
            b = self._body()
            self._json(schedule.apply(bool(b.get("enabled")),
                                      b.get("hour", 7), b.get("minute", 0)))
        elif self.path in ("/api/dislike", "/api/like", "/api/clear"):
            vid = self._body().get("video_id", "")
            if not vid:
                self._json({"error": "video_id required"}, 400)
            else:
                fn = {"/api/like": _like, "/api/dislike": _dislike,
                      "/api/clear": _clear}[self.path]
                _yt_liked_cache["tracks"] = None  # ratings change YT likes
                self._json(fn(vid))
        elif self.path == "/api/library/roots":
            b = self._body()
            path = (b.get("path") or "").strip()
            res = (library.remove_root(path) if b.get("action") == "remove"
                   else library.add_root(path))
            if "error" in res:
                self._json(res, 400)
                return
            if b.get("action") != "remove":
                _start_lib_scan()   # a new folder is useless until it's indexed
            self._json(res)
        elif self.path == "/api/library/scan":
            self._json(_start_lib_scan())
        elif self.path == "/api/playlists":
            b = self._body()
            act = b.get("action") or ""
            pid = b.get("id") or ""
            fn = {"create": lambda: library.pl_create(b.get("name", "")),
                  "rename": lambda: library.pl_rename(pid, b.get("name", "")),
                  "delete": lambda: library.pl_delete(pid),
                  "add": lambda: library.pl_add(pid, b.get("tracks") or []),
                  "remove": lambda: library.pl_remove(pid, b.get("track", "")),
                  "reorder": lambda: library.pl_reorder(pid, b.get("tracks") or []),
                  }.get(act)
            if not fn:
                self._json({"error": "unknown action"}, 400)
                return
            res = fn()
            self._json(res, 400 if "error" in res else 200)
        elif self.path == "/api/run":
            cmd = [sys.executable, "-m", "music_xp.main"]
            if self._body().get("dry_run", False):
                cmd.append("--dry-run")
            self._stream_cmd(cmd)
        elif self.path == "/api/xp":
            self._stream_cmd([sys.executable, "-m", "music_xp.xp"])
        elif self.path == "/api/irish":
            self._stream_cmd([sys.executable, "-m", "music_xp.irish"])
        elif self.path == "/api/timeline":
            self._stream_cmd([sys.executable, "-m", "music_xp.timeline"])
        elif self.path == "/api/digest":
            days = int(self._body().get("days", 7) or 7)
            self._stream_cmd([sys.executable, "-m", "music_xp.digest",
                              "--days", str(days)])
        elif self.path == "/api/download":
            body = self._body()
            ids = body.get("video_ids") or None
            self._stream_download(set(ids) if ids else None, body.get("date"))
        elif self.path == "/api/get":
            body = self._body()
            url = (body.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                self._json({"error": "enter a valid http(s) link"}, 400)
                return
            mode = "video" if body.get("mode") == "video" else "audio"
            height = body.get("height")
            self._stream_lines(downloader.url_download(url, mode, height))
        else:
            self._json({"error": "not found"}, 404)

    def _stream_download(self, only_ids: set[str] | None = None,
                         date: str | None = None) -> None:
        # Enqueue the job; the single worker runs it after any in-flight one,
        # so a second download queues instead of being turned away.
        _ensure_dl_worker()
        out: queue.Queue = queue.Queue()
        with _dl_lock:
            busy = _dl_state["active"]
        ahead = _dl_jobs.qsize() + (1 if busy else 0)
        _dl_jobs.put({"ids": only_ids, "date": date, "out": out})

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if ahead:
            # First bytes right away so the connection isn't idle while waiting,
            # and so the UI can show a "Queued" state until this job starts.
            try:
                self.wfile.write(
                    f"Queued behind {ahead} download(s)…\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        while True:
            line = out.get()
            if line is None:  # sentinel: job done
                break
            try:
                self.wfile.write(line.encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # Client left; keep the worker running (progress still shows in
                # the Downloads tab) but stop trying to write to a dead socket.
                break

    def _stream_cmd(self, cmd: list[str]) -> None:
        def lines():
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            try:
                yield from proc.stdout
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.terminate()
        self._stream_lines(lines())

    def _stream_lines(self, lines) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        # Runs are serialized rather than refused: each one rewrites history.json
        # whole, so two at once would lose a set. Queue behind whatever is going
        # and say so, the way downloads do — a refusal just looks like the button
        # did nothing.
        if not _run_lock.acquire(blocking=False):
            try:
                self.wfile.write(b"Another run is going - waiting for it to "
                                 b"finish, this one starts right after.\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            _run_lock.acquire()
        try:
            for line in lines:
                try:
                    self.wfile.write(line.encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    lines.close()
                    break
        finally:
            _run_lock.release()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Music XP dashboard → http://localhost:{PORT}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.shutdown()
    _awake(False)   # never leave the display-awake assertion behind


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Music XP Explorer</title>
<link rel="icon" href="/assets/logo-64.png?v=3">
<style>
:root{
  --bg:#070304;--bg2:#0e0508;--panel:#110709;--card:#180a0e;--card2:#221016;
  --line:#371921;--txt:#fff0f2;--dim:#c08d97;--faint:#8a5a64;
  --accent:#e8223c;--accent2:#ff5c72;--glow:rgba(232,34,60,.5);
  --good:#37e28a;--bad:#ff4d5e;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  color:var(--txt);background:
  radial-gradient(1100px 600px at 82% -10%,rgba(232,34,60,.14),transparent 60%),
  radial-gradient(800px 500px at 0% 110%,rgba(232,34,60,.08),transparent 60%),
  var(--bg);}
a{color:var(--accent2);text-decoration:none}a:hover{text-decoration:underline}
.app{display:grid;grid-template-columns:236px 1fr;min-height:100vh}
/* ── sidebar ─────────────────────────────────────────── */
.side{background:linear-gradient(180deg,var(--panel),var(--bg2));
  border-right:1px solid var(--line);padding:16px 12px 14px;display:flex;flex-direction:column;gap:4px;
  position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:11px;padding:0 8px 12px}
.brand img{width:44px;height:44px;filter:drop-shadow(0 0 12px var(--glow))}
.brand{justify-content:center;flex-direction:column;gap:5px}
.brand .wm{width:132px;height:auto;flex:none;
  filter:drop-shadow(0 0 10px rgba(232,34,60,.35))}
/* The nav scrolls, the player doesn't: the now-playing card is tall enough that
   on a short window it would otherwise push itself off the bottom. */
.side nav{display:flex;flex-direction:column;gap:2px;margin-top:2px;
  flex:1 1 auto;min-height:0;overflow-y:auto;scrollbar-width:none}
.side nav::-webkit-scrollbar{display:none}
.side nav button{display:flex;align-items:center;gap:11px;background:none;border:0;
  color:var(--dim);padding:8px 12px;border-radius:10px;cursor:pointer;font-size:13px;
  font-weight:500;text-align:left;width:100%}
svg.ic{width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.7;
  stroke-linecap:round;stroke-linejoin:round;flex:none}
.side nav button:hover{color:var(--txt);background:rgba(232,34,60,.07)}
.side nav button.active{color:#fff;background:linear-gradient(90deg,var(--accent),#8f1425);
  box-shadow:0 6px 22px -8px var(--glow)}
.livepill{font-size:8.5px;font-weight:800;letter-spacing:1.2px;background:var(--accent);
  color:#fff;padding:2.5px 7px;border-radius:20px;margin-left:auto;
  box-shadow:0 0 10px var(--glow)}
.ext{margin-left:auto;color:var(--faint);font-size:12px}
/* ── now playing, Apple Music style: art on top, then title, scrubber and
   transport, all in the foot of the sidebar. The same element moves into the
   full-screen overlay when expanded, so there is only ever one player. ── */
/* Fade the nav out into the player rather than slicing the last item in half
   when the list is long enough to scroll. */
.np-host{margin-top:auto;flex:none;padding-top:12px;position:relative}
.np-host::before{content:"";position:absolute;left:0;right:0;top:-26px;height:26px;
  pointer-events:none;background:linear-gradient(180deg,transparent,var(--bg2))}
.nowplay{background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.014));
  border:1px solid var(--line);border-radius:16px;padding:10px 10px 12px;
  display:flex;flex-direction:column;gap:9px}
.np-art{position:relative;width:100%;aspect-ratio:1;border-radius:12px;overflow:hidden;
  background:#000;cursor:pointer;
  box-shadow:0 16px 34px -16px #000,inset 0 0 0 1px rgba(255,255,255,.06)}
.np-art img{width:100%;height:100%;object-fit:cover;transform:scale(1.34);display:block}
.np-art img.sq{transform:none}
.np-exp{position:absolute;top:7px;right:7px;width:26px;height:26px;border-radius:8px;border:0;
  background:rgba(8,3,6,.66);color:#fff;font-size:12px;cursor:pointer;opacity:0;
  display:flex;align-items:center;justify-content:center;transition:opacity .16s}
.np-art:hover .np-exp{opacity:1}
.np-info{min-width:0;padding:0 2px}
.nowplay .np-t{font-weight:650;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nowplay .np-a{color:var(--dim);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.np-times{display:flex;justify-content:space-between;gap:8px;margin-top:2px;
  font-size:10px;color:var(--faint);font-variant-numeric:tabular-nums}
.np-tr{display:flex;align-items:center;justify-content:center;gap:5px}
.np-vol{display:flex;align-items:center;gap:4px}
.nowplay .pbtn{width:30px;height:30px;background:transparent;border-color:transparent;
  color:var(--dim);font-size:12.5px}
.nowplay .pbtn:hover{color:var(--txt);background:rgba(255,255,255,.06)}
.nowplay .pbtn.big{width:40px;height:40px;font-size:14px;background:var(--accent);
  border-color:var(--accent);color:#fff}
.nowplay .pbtn.big:hover{background:var(--accent2)}
.nowplay .pbtn.sm{width:25px;height:25px;font-size:11px}
/* Idle = nothing loaded yet, so the card is a suggestion: art, name, play. */
.nowplay.idle .np-prog,.nowplay.idle .np-vol,.nowplay.idle .np-exp{display:none}
.nowplay.idle .np-tr .pbtn:not(.big){opacity:.3;pointer-events:none}
/* ── full-screen now playing ── */
#npfull{position:fixed;inset:0;z-index:200;display:none}
#npfull.on{display:block}
/* Apple's artwork wash: the cover blown up and blurred three times over, each
   copy drifting on its own long orbit so the colour breathes. The blur is
   rasterised once per layer — only transform animates, so this is compositor
   work, not a per-frame filter pass. */
/* The blur sits on the inner <i>, the drift on the wrapper. Chrome rasterises
   the blurred surface once and the animation only moves that finished texture;
   put both on one element and every scale step re-runs the blur. */
.nf-bg{position:absolute;inset:-25%;will-change:transform;
  animation:nfd1 44s ease-in-out infinite alternate}
.nf-bg>i{position:absolute;inset:0;display:block;
  background-size:cover;background-position:center;
  filter:blur(64px) saturate(2) brightness(.6)}
.nf-bg.b2{opacity:.6;animation:nfd2 63s ease-in-out infinite alternate}
.nf-bg.b2>i{filter:blur(90px) saturate(2.6) brightness(.72)}
.nf-bg.b3{opacity:.42;animation:nfd3 81s ease-in-out infinite alternate}
.nf-bg.b3>i{filter:blur(116px) saturate(3) brightness(.8)}
@keyframes nfd1{
  from{transform:scale(1.3) translate3d(-3%,-2%,0) rotate(0deg)}
  to{transform:scale(1.5) translate3d(4%,3%,0) rotate(9deg)}}
@keyframes nfd2{
  from{transform:scale(1.7) translate3d(5%,4%,0) rotate(0deg)}
  to{transform:scale(1.4) translate3d(-6%,-3%,0) rotate(-11deg)}}
@keyframes nfd3{
  from{transform:scale(1.5) translate3d(-6%,5%,0) rotate(6deg)}
  to{transform:scale(1.9) translate3d(6%,-5%,0) rotate(-6deg)}}
@media (prefers-reduced-motion:reduce){.nf-bg{animation:none}}
/* Monochrome covers blur to flat grey, so a trace of the brand keeps the room
   from going colourless — but the artwork leads. */
.nf-scrim{position:absolute;inset:0;background:
  radial-gradient(70% 90% at 18% 38%,rgba(232,34,60,.07),transparent 62%),
  linear-gradient(180deg,rgba(5,2,4,.46),rgba(5,2,4,.7)),
  radial-gradient(120% 110% at 50% 45%,transparent 26%,rgba(0,0,0,.5) 78%,rgba(0,0,0,.9) 100%)}
/* Apple Music's proportions: a modest cover with its controls tucked beneath it
   on the left, and the whole rest of the window given to the lyrics. The player
   is deliberately quiet here — the words are what you look at. */
.nf-in{position:relative;height:100%;display:flex;align-items:center;justify-content:center;
  gap:clamp(40px,6vw,96px);padding:clamp(28px,5vh,64px) clamp(32px,5vw,72px)}
.nf-left{width:clamp(300px,30vw,392px);flex:none;display:flex;flex-direction:column;gap:20px;
  position:relative}
/* One soft wash sits behind the whole column — cover and controls together —
   so the artwork reads as seated in the page instead of pasted on it. Painted
   once, never animated. */
.nf-left::before{content:"";position:absolute;
  inset:clamp(-30px,-3vh,-18px) clamp(-26px,-2vw,-16px);z-index:0;
  pointer-events:none;border-radius:clamp(20px,2.4vw,32px);
  background:radial-gradient(86% 78% at 50% 50%,rgba(0,0,0,.5),
             rgba(0,0,0,.3) 68%,transparent 100%)}
.nf-left>*{position:relative;z-index:1}
.nf-art{width:100%;aspect-ratio:1;border-radius:14px;overflow:hidden;flex:none;
  background:#000;box-shadow:0 34px 76px -30px #000,inset 0 0 0 1px rgba(255,255,255,.07)}
.nf-art img{width:100%;height:100%;object-fit:cover;transform:scale(1.34);display:block}
.nf-art img.sq{transform:none}
#nf-host{flex:none}
/* Lyrics scroll by translating the list, not by rewriting it: one transform per
   line change, so a 2012 GPU has nothing to do between lines. */
.nf-lyrwrap{flex:1;min-width:0;position:relative}
.nf-lyr{height:min(88vh,860px);overflow:hidden;position:relative;
  -webkit-mask-image:linear-gradient(180deg,transparent,#000 17%,#000 83%,transparent);
  mask-image:linear-gradient(180deg,transparent,#000 17%,#000 83%,transparent)}
/* Sits straight on the artwork like the rest of the chrome — no pill, no box.
   It lives outside .nf-lyr so the lyric mask doesn't fade it out. */
.lyr-add{position:absolute;left:0;bottom:2px;background:none;border:0;padding:4px 2px;
  color:rgba(255,255,255,.34);font:inherit;font-size:12.5px;letter-spacing:.02em;
  cursor:pointer;transition:color .2s,opacity .4s}
.lyr-add:hover{color:#fff}
#npfull.hidecur .lyr-add{opacity:0;pointer-events:none}
.nf-lyrwrap.drop .lyr-add{color:#fff}
.nf-lyrwrap.drop:after{content:'';position:absolute;inset:-10px;border-radius:12px;
  border:1px dashed rgba(255,255,255,.45);pointer-events:none}
.lyr-hint{flex:1;color:var(--dim);font-size:12px;min-width:0}
/* The list slides as a whole so the live line sits dead centre; only the
   transform moves, never the text itself. */
.ll-in{transition:transform .5s cubic-bezier(.22,.61,.36,1);will-change:transform}
.ll{font-size:clamp(20px,2.1vw,31px);font-weight:750;line-height:1.26;
  padding:clamp(6px,.85vh,12px) 0;letter-spacing:-.012em;
  color:rgba(255,255,255,.24);transition:color .35s}
.ll.done{color:rgba(255,255,255,.15)}
.ll.on{font-size:clamp(26px,3vw,44px);color:#fff}
@media (prefers-reduced-motion:reduce){.ll-in{transition:none}}
/* Untimed lyrics: nothing to follow, so the song scrolls as one readable block.
   Set a shade below the synced size — it's read, not glanced at. */
.ll-plain{height:100%;overflow-y:auto;padding:7vh 0;scrollbar-width:none;
  overscroll-behavior:contain}
.ll-plain::-webkit-scrollbar{display:none}
.lp{font-size:clamp(18px,1.8vw,26px);font-weight:700;line-height:1.44;
  padding:clamp(3px,.4vh,7px) 0;letter-spacing:-.012em;color:rgba(255,255,255,.84)}
.lp-gap{height:clamp(10px,1.4vh,20px);padding:0}
.lp-note{font-size:13px;font-weight:600;color:rgba(255,255,255,.42);padding-bottom:14px}
/* The offer to spend twenty minutes of CPU: visible, but never pressed for him. */
.lp-act{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:18px 0 4px}
.lyr-go{background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.18);
  color:#fff;border-radius:8px;padding:8px 16px;font:inherit;font-size:13px;cursor:pointer}
.lyr-go:hover{background:var(--accent);border-color:var(--accent)}
.lyr-go[disabled]{opacity:.5;cursor:default;background:rgba(255,255,255,.09)}
.nf-lyr>.nf-msg{height:100%;display:flex;align-items:center}
.nf-msg{color:var(--dim);font-size:15px}
.nf-x{position:absolute;top:20px;right:24px;z-index:3;width:36px;height:36px;border-radius:50%;
  border:1px solid rgba(255,255,255,.14);background:rgba(8,3,6,.5);color:#fff;font-size:14px;
  cursor:pointer;display:flex;align-items:center;justify-content:center}
.nf-x:hover{background:var(--accent);border-color:var(--accent)}
/* Idle in fullscreen: cursor and close button fade out, lyrics stand alone. */
#npfull.hidecur{cursor:none}
#npfull.hidecur .nf-x{opacity:0;pointer-events:none}
.nf-x{transition:opacity .4s,background .15s}
.nowplay.full{background:none;border:0;padding:0;gap:14px;width:100%}
.nowplay.full .np-art{display:none}
.nowplay.full .np-info{padding:0 2px}
.nowplay.full .np-t{font-size:19px;font-weight:750;white-space:nowrap;line-height:1.25;
  letter-spacing:-.012em}
.nowplay.full .np-a{font-size:14px;color:rgba(255,255,255,.55);margin-top:2px}
/* Scrubber: same red, no neon — the halo smeared into the artwork behind it. */
.nowplay.full .np-prog{padding:0 2px}
.nowplay.full .pbar{height:16px}
.nowplay.full .pbar .rail{height:5px;border-radius:99px;background:rgba(255,255,255,.18)}
.nowplay.full .pbar:hover .rail{height:7px}
.nowplay.full .pbar .fill{box-shadow:none;border-radius:99px}
.nowplay.full .np-times{font-size:11.5px;color:rgba(255,255,255,.45);margin-top:5px}
/* Nothing boxes the controls in: they sit straight on the artwork, and the
   scrim under #nf-host is what keeps them legible over a bright cover. */
.nowplay.full .np-tr,
.nowplay.full .np-vol{background:none;border:0;box-shadow:none;border-radius:0}
.nowplay.full .np-tr{justify-content:center;gap:clamp(6px,1.4vw,16px);padding:2px 0;
  margin-top:4px}
.nowplay.full .np-vol{gap:12px;padding:6px 0;justify-content:center}
.nowplay.full .pbtn{width:44px;height:44px;font-size:19px;color:rgba(255,255,255,.82);
  background:none;border:0;border-radius:50%;
  filter:drop-shadow(0 2px 6px rgba(0,0,0,.55));
  transition:color .16s,transform .12s}
.nowplay.full .pbtn:hover{background:none;color:#fff;transform:scale(1.08)}
/* The UA focus ring is a blue rectangle over a round button — swap it for a
   white halo that only shows for keyboard users. */
.pbtn:focus{outline:none}
.pbtn:focus-visible{outline:none;box-shadow:0 0 0 2px rgba(255,255,255,.85)}
.nowplay.full .pbtn.big:focus-visible{
  box-shadow:0 10px 26px -10px rgba(0,0,0,.75),0 0 0 3px rgba(255,255,255,.55)}
.nowplay.full .pbtn:active{transform:scale(.9)}
/* Play is the only solid mark on the screen — a plain white disc, no red halo. */
.nowplay.full .pbtn.big{width:60px;height:60px;font-size:23px;color:#0b0709;
  background:#fff;border:0;filter:none;
  box-shadow:0 10px 26px -10px rgba(0,0,0,.75)}
.nowplay.full .pbtn.big:hover{background:#fff;transform:scale(1.06)}
.nowplay.full .pbtn.sm{width:34px;height:34px;font-size:14px}
/* Stop belongs to the sidebar — in full screen Esc already leaves, so the row
   is just the one add-to-playlist button, centred under the transport. */
.nowplay.full #p-close{display:none}
.nowplay.full #p-pl{width:42px;height:42px;font-size:24px;font-weight:300;
  color:rgba(255,255,255,.72)}
.nowplay.full #p-pl:hover{color:#fff}
.nowplay.full .pbtn.on{color:var(--accent2);text-shadow:none}
.nowplay.full #p-like.on,.nowplay.full #p-dis.on{box-shadow:none;border-color:transparent}
/* ── main ────────────────────────────────────────────── */
.main{padding:0 28px 36px;min-width:0}
.topbar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:12px;
  padding:14px 0;background:rgba(7,3,4,.96)}
.topbar .sp{flex:1}
.search{width:340px;display:flex;align-items:center;gap:9px;background:var(--card);
  border:1px solid var(--line);border-radius:22px;padding:9px 15px;color:var(--dim)}
.search input{flex:1;background:none;border:0;color:var(--txt);outline:none;font-size:13px;min-width:0}
.tab{display:none}.tab.active{display:block;animation:fade .25s}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1}}
/* hero */
.hero{position:relative;overflow:hidden;border-radius:22px;padding:28px 34px;margin-bottom:20px;
  display:flex;align-items:center;justify-content:space-between;gap:20px;
  background:radial-gradient(640px 340px at 82% 45%,rgba(232,34,60,.35),transparent 65%),
  radial-gradient(400px 200px at 70% 110%,rgba(232,34,60,.25),transparent 70%),
  linear-gradient(115deg,#1e030b,#0d0407 72%);
  box-shadow:0 24px 70px -34px var(--glow)}
/* artist-tile wall — the day's artists as square facets, the whole band rolling
   sideways at a constant rate like the earth's surface passing by. Tiles stay
   upright and hold a fixed brightness (any pulsing light reads as photos fading
   in and out), the sequence is duplicated so the loop is seamless, and
   object-fit:cover means no photo stretches or leaves a gap. Transform only. */
.hero .hero-bg{position:absolute;inset:0;z-index:0;overflow:hidden;border-radius:22px}
/* --tile is sized in JS so a whole number of rows fills the card exactly — no
   half-cropped row at the top or bottom edge. */
.hero-mosaic{--tile:96px;--gap:3px;
  position:absolute;top:0;left:0;height:100%;display:grid;gap:var(--gap);
  grid-auto-flow:column;grid-auto-columns:var(--tile);
  grid-template-rows:repeat(var(--rows),var(--tile));
  will-change:transform;animation:heroroll var(--roll,72s) linear infinite}
.hero:hover .hero-mosaic{animation-play-state:paused}
.hero-facet{width:var(--tile);height:var(--tile);object-fit:cover;border-radius:3px;
  filter:saturate(1.05) brightness(.55);opacity:.95;pointer-events:none;
  -webkit-user-drag:none;user-select:none}
/* half the track = one full copy of the sequence, plus the gap that joins them */
@keyframes heroroll{from{transform:translateX(0)}
  to{transform:translateX(calc(-50% - var(--gap)/2))}}
@media (prefers-reduced-motion:reduce){.hero-mosaic{animation:none}}
.hero .hero-tint{position:absolute;inset:0;z-index:0;pointer-events:none;
  background:
    /* left scrim — headline needs a near-solid bed, not a wash */
    linear-gradient(90deg,rgba(7,2,5,.93) 0%,rgba(7,2,5,.88) 26%,rgba(7,2,5,.5) 44%,transparent 62%),
    radial-gradient(115% 150% at 6% 82%,rgba(9,3,6,.92) 0%,rgba(9,3,6,.6) 32%,transparent 62%),
    linear-gradient(0deg,rgba(7,2,5,.72) 0%,rgba(7,2,5,.3) 24%,transparent 50%),
    /* bloom behind the ball */
    radial-gradient(420px 420px at 82% 44%,rgba(255,60,90,.3),rgba(232,34,60,.12) 45%,transparent 72%),
    /* vignette */
    radial-gradient(125% 115% at 50% 46%,transparent 42%,rgba(0,0,0,.42) 78%,rgba(0,0,0,.72) 100%)}
/* giant "zEi" watermark behind the hero content */
.hero>*{position:relative;z-index:1}
.hl{max-width:68%}
.greet{color:var(--dim);font-size:13px;margin-bottom:8px;text-shadow:0 1px 12px rgba(0,0,0,.7)}
.hero h2{margin:0;font-size:36px;font-weight:800;line-height:1.06;letter-spacing:-.6px;text-shadow:0 2px 22px rgba(0,0,0,.72)}
.hero h2 em{font-style:normal;color:var(--accent2);text-shadow:0 0 26px var(--glow)}
.hero p{color:#e9dfe2;margin:11px 0 18px;font-size:13.5px;max-width:92%;text-shadow:0 1px 12px rgba(0,0,0,.75)}
.cta{display:inline-flex;align-items:center;gap:8px;white-space:nowrap;background:linear-gradient(90deg,var(--accent),#c11830);
  border:0;color:#fff;padding:12px 18px;border-radius:12px;cursor:pointer;font-weight:700;font-size:13.5px;
  box-shadow:0 10px 30px -10px var(--glow)}
.cta:hover{filter:brightness(1.12)}
.cta:disabled{opacity:.55;cursor:default}
.cta.ghost{background:rgba(255,255,255,.05);color:var(--txt);border:1px solid var(--line);box-shadow:none}
.cta.ghost:hover{border-color:var(--accent);filter:none}
.cta.sm{padding:8px 14px;font-size:12.5px}
/* XP+ — the out-of-comfort-zone button: iridescent, set apart from the safe picks */
.cta.xp{background:linear-gradient(90deg,#7b2ff7,#e8223c 55%,#ff7a18);
  box-shadow:0 10px 30px -10px rgba(123,47,247,.6)}
.cta.xp:hover{filter:brightness(1.14) saturate(1.1)}
/* Irish Mode / Timeline get their own identity so the three discovery buttons
   never read as the same action. */
.cta.irish{background:linear-gradient(90deg,#0f7a3d,#2fbf6b)}
.cta.irish:hover{filter:brightness(1.12)}
.cta.time{background:linear-gradient(90deg,#5a3bd6,#b148d8)}
.cta.time:hover{filter:brightness(1.12)}
.nav-irish .xppill{margin-left:auto;font-size:11px;color:#5fd694}
.nav-time .xppill{margin-left:auto;font-size:11px;color:#c79bf0}
.toolbar{display:flex;gap:10px;flex-wrap:nowrap;align-items:center}
/* section titles */
.sec-h{display:flex;align-items:center;justify-content:space-between;margin:24px 2px 14px}
.sec-h h3{margin:0;font-size:18px;font-weight:700}
.sec-h .spark{color:var(--accent2)}
.sec-h .cnt{color:var(--dim);font-size:12.5px}
.sec-h .sh-r{display:flex;align-items:center;gap:12px}
/* explore two-col */
.explore{display:grid;grid-template-columns:1fr 292px;gap:24px;align-items:start}
@media(max-width:1100px){.explore{grid-template-columns:1fr}}
/* pick cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(162px,1fr));gap:16px}
.pcard{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden;
  transition:transform .15s,box-shadow .15s,border-color .15s}
.pcard:hover{transform:translateY(-4px);border-color:rgba(232,34,60,.55);
  box-shadow:0 18px 44px -22px var(--glow)}
.pcard.disliked{opacity:.4}
.thumb{position:relative;aspect-ratio:1;border-radius:12px 12px 0 0;
  background:var(--card2) center/178% no-repeat}
.thumb .sc{position:absolute;top:9px;left:9px;background:rgba(7,3,4,.82);
  color:var(--accent2);font-weight:700;font-size:11.5px;padding:3px 8px;border-radius:20px;
  font-variant-numeric:tabular-nums}
.thumb .xpb{position:absolute;top:9px;right:9px;
  background:linear-gradient(90deg,#7b2ff7,#e8223c 60%,#ff7a18);
  color:#fff;font-weight:800;font-size:10.5px;letter-spacing:.4px;
  padding:3px 8px;border-radius:20px;box-shadow:0 4px 12px -4px rgba(123,47,247,.7)}
/* heart chip: cycles blank ♡ → red ♥ (like) → black ♥ (dislike) → blank */
.hchip{position:absolute;right:9px;bottom:9px;width:36px;height:36px;border-radius:50%;
  background:rgba(7,3,4,.78);border:1px solid rgba(255,255,255,.14);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;
  transition:background .15s,transform .15s,color .15s}
.hchip:hover{transform:scale(1.1)}
.hchip.lk{color:#ff2d4d;border-color:var(--accent);text-shadow:0 0 10px var(--glow)}
.hchip.dk{color:#0b0b0b;background:rgba(232,232,232,.9);border-color:rgba(255,255,255,.5)}
.pchip{position:absolute;left:9px;bottom:9px;width:36px;height:36px;border-radius:50%;
  background:rgba(7,3,4,.78);border:1px solid rgba(255,255,255,.14);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:12px;
  transition:background .15s,transform .15s}
.pchip:hover{background:var(--accent);transform:scale(1.1);text-decoration:none;
  box-shadow:0 0 18px -2px var(--glow)}
.dchip{position:absolute;right:9px;top:9px;width:30px;height:30px;border-radius:50%;
  background:rgba(7,3,4,.78);border:1px solid rgba(255,255,255,.14);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:13px;cursor:pointer;
  transition:background .15s,transform .15s}
.dchip:hover{background:var(--accent);transform:scale(1.1)}
.dchip.done{color:var(--accent2);border-color:var(--accent2);pointer-events:none}
.pcard .meta{padding:11px 13px 13px}
.pcard .pt{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pcard .pa{color:var(--dim);font-size:12px;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pcard .pl{display:inline-block;margin-top:8px;font-size:9.5px;letter-spacing:.8px;text-transform:uppercase;
  color:var(--accent2);background:rgba(232,34,60,.12);padding:2.5px 8px;border-radius:20px}
/* right rail */
.rail .card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:16px}
.db-h{display:flex;align-items:center;gap:9px}
.dbt{font-size:16px;font-weight:700}
.dbsub{color:var(--faint);font-size:12px;margin:3px 0 14px}
/* ── disco bowl scene: hanging ball, sweeping beams, sparkles ── */
.dscene{position:relative;height:196px;margin:2px 0 16px;border-radius:16px;overflow:hidden;
  background:radial-gradient(130% 100% at 50% 0%,#30081293,transparent 75%),
  radial-gradient(90% 70% at 50% 115%,rgba(232,34,60,.20),transparent 70%),#0b0305;
  border:1px solid var(--line)}
.dwire{position:absolute;left:50%;top:0;width:1px;height:24px;z-index:3;
  background:linear-gradient(180deg,rgba(255,255,255,.35),rgba(232,34,60,.5))}
.dball{width:132px;height:132px;position:absolute;left:50%;top:18px;margin-left:-66px;
  z-index:3;transform-origin:50% -18px;filter:drop-shadow(0 0 28px var(--glow));
  will-change:transform;animation:dsway 9s ease-in-out infinite}
@keyframes dsway{0%,100%{transform:rotate(-3deg)}50%{transform:rotate(3deg)}}
.spk{position:absolute;width:4px;height:4px;border-radius:50%;background:#fff;z-index:2;
  box-shadow:0 0 9px 2px rgba(255,130,148,.85);opacity:0;
  animation:twinkle 2.8s ease-in-out infinite}
@keyframes twinkle{0%,100%{opacity:0;transform:scale(.3)}50%{opacity:.95;transform:scale(1)}}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;text-align:center}
.stat .n{font-size:21px;font-weight:800;color:var(--accent2)}
.stat .l{font-size:9.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.xp .lv{color:var(--accent2);font-weight:800}
.bar{height:10px;border-radius:20px;background:var(--card2);overflow:hidden;
  border:1px solid var(--line);margin-top:10px}
.bar i{display:block;height:100%;border-radius:20px;
  background:linear-gradient(90deg,var(--accent),var(--accent2));box-shadow:0 0 14px var(--glow)}
/* generic cards */
.gcard{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:16px 18px;margin-bottom:12px}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px}
.t{font-weight:600}.a{color:var(--dim);font-size:13px}.muted{color:var(--dim)}
.badge{font-size:11px;padding:2px 8px;border-radius:20px}
.b-liked{background:rgba(55,226,138,.14);color:var(--good)}
.b-disliked{background:rgba(255,77,94,.15);color:var(--bad)}
.b-skipped{background:var(--card2);color:var(--dim)}.b-pending{color:var(--faint)}
.pctl{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.chip{font-size:12px;background:var(--card2);padding:3px 10px;border-radius:20px;color:var(--dim)}
label.ctl{display:block;margin:16px 0}
label.ctl .row span:last-child{color:var(--accent2);font-variant-numeric:tabular-nums;font-weight:600}
input[type=range]{width:100%;accent-color:var(--accent)}
.langgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:9px}
.langtoggle{display:flex;align-items:center;gap:9px;padding:10px;border:1px solid var(--line);
  border-radius:11px;background:var(--card);cursor:pointer}.langtoggle small{color:var(--dim)}
.langtoggle input{accent-color:var(--accent)}
select{background:var(--card2);color:var(--txt);border:1px solid var(--line);border-radius:9px;padding:8px}
pre#log,pre#llog,pre#getlog,pre#runlog,pre[id^="hlog-"],pre[id$="-setlog"]{background:#050203;border:1px solid var(--line);border-radius:14px;padding:14px;
  max-height:320px;overflow:auto;white-space:pre-wrap;font-size:12.5px;color:#eccad0;
  margin:0 0 22px;display:none}
.getin{width:100%;box-sizing:border-box;background:var(--card2);color:var(--txt);
  border:1px solid var(--line);border-radius:11px;padding:12px 14px;font-size:14px;margin-bottom:12px}
.getin:focus{outline:none;border-color:var(--accent)}
.getopts{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin-bottom:14px}
.getseg{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--dim)}
.getseg select{min-width:150px}
.gethint{font-size:12px;color:var(--faint);margin-bottom:10px}
pre[id^="hlog-"]{margin:12px 0 0}
.hact{display:flex;gap:12px;align-items:center;white-space:nowrap}
.hhead{cursor:pointer;flex:1;user-select:none}
.hhead .exp{color:var(--faint);font-size:12px;margin-left:4px}
.tapx{color:var(--faint)}
.daywrap{display:none;margin-top:14px}
/* ── queue panel ─────────────────────────────────────── */
#queue{position:fixed;right:20px;bottom:20px;z-index:250;width:322px;
  max-height:min(64vh,540px);display:none;flex-direction:column;background:var(--panel);
  border:1px solid var(--line);border-radius:16px;overflow:hidden;
  box-shadow:0 22px 70px rgba(0,0,0,.6)}
#queue.on{display:flex}
.qh{display:flex;align-items:center;gap:10px;padding:13px 15px;border-bottom:1px solid var(--line)}
.qh h3{margin:0;font-size:14px}
.qsub{font-size:11px;color:var(--faint);margin-left:auto}
.qlist{overflow:auto;padding:6px}
.qrow{display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:10px;cursor:pointer}
.qrow:hover{background:var(--card)}
.qrow.done{opacity:.4}
.qrow.now{background:rgba(255,255,255,.07)}
.qrow.now .qt{color:var(--accent2)}
.qn{width:22px;flex:none;text-align:right;font-size:11px;color:var(--faint)}
.qrow.now .qn{color:var(--accent2)}
.qth{width:34px;height:34px;border-radius:7px;flex:none;
  background-size:cover;background-position:center}
.qm{flex:1;min-width:0}
.qt{font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.qa{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* ── lyrics studio ───────────────────────────────────────
   A workshop, not player chrome: it has edges, panels and buttons, because
   everything here is a job you do once and leave. Nothing of the artwork wash
   shows through, so its three blurred layers are parked while this is open —
   a 2012 GPU has better uses for those frames than compositing what's hidden. */
body.lst-on .nf-bg{animation-play-state:paused}
.lst-ovl{position:fixed;inset:0;z-index:400;background:rgba(4,2,3,.94);
  display:flex;align-items:center;justify-content:center;
  padding:min(4vh,30px) min(4vw,40px)}
.lst-ovl[hidden]{display:none}
.lst{width:min(1080px,100%);height:100%;background:var(--panel);
  border:1px solid var(--line);border-radius:18px;display:flex;flex-direction:column;
  overflow:hidden;box-shadow:0 30px 90px rgba(0,0,0,.7)}
.lst-ovl.drop .lst{border-color:var(--accent)}
.lst-h{display:flex;align-items:center;gap:10px;padding:13px 16px;
  border-bottom:1px solid var(--line);flex:none}
.lst-id{flex:1;min-width:0}
.lst-t{font-size:15px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lst-a{font-size:12px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lst-badge{font-size:11.5px;color:var(--faint);text-align:right;max-width:34%;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lst-btn{background:rgba(255,255,255,.07);border:1px solid var(--line);color:var(--txt);
  border-radius:8px;padding:6px 12px;font:inherit;font-size:12.5px;cursor:pointer;
  white-space:nowrap;transition:background .15s,border-color .15s,color .15s}
.lst-btn:hover{background:rgba(255,255,255,.14);border-color:var(--accent)}
.lst-btn.prim{background:var(--accent);border-color:var(--accent);color:#fff}
.lst-btn.prim:hover{background:var(--accent2);border-color:var(--accent2)}
.lst-btn.on{background:var(--accent);border-color:var(--accent);color:#fff}
.lst-btn[hidden]{display:none}
.lst-btn[disabled]{opacity:.38;cursor:default;background:rgba(255,255,255,.07);
  border-color:var(--line)}
.lst-x{background:none;border:0;color:var(--faint);font-size:22px;line-height:1;
  cursor:pointer;padding:0 2px}
.lst-x:hover{color:var(--txt)}
.lst-tabs{display:flex;align-items:center;gap:3px;padding:8px 16px;
  border-bottom:1px solid var(--line);flex:none}
.lst-tabs button{background:none;border:0;color:var(--dim);font:inherit;font-size:13px;
  padding:6px 13px;border-radius:8px;cursor:pointer}
.lst-tabs button:hover{color:var(--txt);background:rgba(255,255,255,.05)}
.lst-tabs button.on{color:var(--txt);background:rgba(255,255,255,.09)}
.lst-note{margin-left:auto;font-size:11.5px;color:var(--faint)}
.lst-note.warn{color:var(--accent2)}
.lst-body{flex:1;min-height:0;display:flex}
.lst-pane{flex:1;min-width:0;display:flex;flex-direction:column;gap:10px;padding:12px 16px}
.lst-pane[hidden]{display:none}
.lst-tools{display:flex;align-items:center;gap:6px;flex-wrap:wrap;flex:none}
.lst-lbl{font-size:12px;color:var(--dim);margin-right:2px}
.lst-off{font-size:12.5px;color:var(--txt);font-variant-numeric:tabular-nums;
  min-width:56px;text-align:center}
.lst-sep{width:1px;height:20px;background:var(--line);margin:0 3px}
/* The list is the editor and the preview at once: it lights up with the audio,
   so a time that is off announces itself instead of having to be hunted. */
.lst-lines{flex:1;min-height:0;overflow-y:auto;border:1px solid var(--line);
  border-radius:12px;background:var(--bg2);padding:4px}
.lrow{display:flex;align-items:center;gap:8px;padding:3px 6px;border-radius:8px}
.lrow:hover{background:rgba(255,255,255,.04)}
.lrow.sel{background:rgba(255,255,255,.09)}
.lrow.on{background:rgba(232,34,60,.18)}
.lrow.on .lx{color:#fff}
.lt{width:80px;flex:none;background:rgba(255,255,255,.05);border:1px solid transparent;
  border-radius:6px;color:var(--dim);font:inherit;font-size:12px;
  font-variant-numeric:tabular-nums;text-align:center;padding:4px 2px;outline:none}
.lt:focus{border-color:var(--accent);color:var(--txt)}
.lt.none{color:var(--faint)}
.lx{flex:1;min-width:0;font-size:13.5px;color:rgba(255,240,242,.84);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lx.blank{color:var(--faint);font-style:italic}
/* Per-line controls stay out of the way until the line is under the pointer —
   a hundred rows of buttons reads as noise, not as power. */
.lb{opacity:0;background:none;border:0;color:var(--faint);font:inherit;font-size:11.5px;
  cursor:pointer;padding:3px 7px;border-radius:6px;flex:none}
.lrow:hover .lb,.lrow.sel .lb{opacity:1}
.lb:hover{color:#fff;background:rgba(255,255,255,.1)}
.lst-taphint{flex:none;font-size:12px;color:var(--dim);text-align:center;padding:1px}
.lst-taphint[hidden]{display:none}
.lst-taphint b{color:var(--txt);font-weight:700}
#lst-ta{flex:1;min-height:0;width:100%;resize:none;background:var(--bg2);
  border:1px solid var(--line);border-radius:12px;color:var(--txt);font:inherit;
  font-size:13.5px;line-height:1.65;padding:12px 14px;outline:none}
#lst-ta:focus{border-color:rgba(255,255,255,.34)}
.lst-row{display:flex;align-items:center;gap:9px;flex:none}
.lst-hint{flex:1;min-width:0;color:var(--dim);font-size:12px}
.lst-cands{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:8px}
.cand{border:1px solid var(--line);border-radius:12px;background:var(--card);
  padding:10px 12px;display:flex;align-items:center;gap:12px}
.cand-m{flex:1;min-width:0}
.cand-t{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cand-s{font-size:11.5px;color:var(--dim);margin-top:2px}
.cand-p{font-size:11.5px;color:var(--faint);margin-top:5px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.cand-w{font-size:10px;font-weight:800;letter-spacing:.8px;padding:3px 8px;
  border-radius:20px;background:rgba(255,255,255,.07);color:var(--dim);flex:none}
.lst-foot{display:flex;align-items:center;gap:10px;padding:10px 16px;
  border-top:1px solid var(--line);flex:none}
.lst-play{width:34px;height:34px;padding:0;border-radius:50%;background:var(--accent);
  border-color:var(--accent);color:#fff;font-size:13px}
.lst-play:hover{background:var(--accent2);border-color:var(--accent2)}
.lst-time{font-size:11.5px;color:var(--faint);font-variant-numeric:tabular-nums;
  min-width:34px;text-align:center}
.lst-bar{flex:1;height:6px;border-radius:3px;background:rgba(255,255,255,.1);
  cursor:pointer;position:relative}
.lst-fill{height:100%;border-radius:3px;background:var(--accent);width:0}
/* ── download picker modal ───────────────────────────── */
.ovl{position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.62);
  display:flex;align-items:center;justify-content:center;padding:24px}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:18px;
  width:min(560px,100%);max-height:84vh;display:flex;flex-direction:column;
  box-shadow:0 24px 80px rgba(0,0,0,.6)}
.modal .mh{display:flex;align-items:center;justify-content:space-between;
  padding:18px 20px;border-bottom:1px solid var(--line)}
.modal .mh h3{margin:0;font-size:16px}
.mx{cursor:pointer;color:var(--faint);font-size:18px;line-height:1;background:none;border:none}
.mtools{display:flex;gap:8px;padding:12px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.mchip{font-size:12px;padding:5px 11px;border-radius:20px;cursor:pointer;
  background:rgba(255,255,255,.05);border:1px solid var(--line);color:var(--dim)}
.mchip:hover{border-color:var(--accent);color:var(--txt)}
.mlist{overflow:auto;padding:6px 10px}
.mrow{display:flex;align-items:center;gap:12px;padding:8px 10px;border-radius:11px;cursor:pointer}
.mrow:hover{background:var(--card)}
.mrow input{width:17px;height:17px;accent-color:var(--good);cursor:pointer;flex:none}
.mrow .mth{width:38px;height:38px;border-radius:7px;background-size:cover;background-position:center;flex:none}
.mrow .mmeta{flex:1;min-width:0}
.mrow .mt{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mrow .ma{font-size:11px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.modal .mf{display:flex;align-items:center;justify-content:space-between;gap:12px;
  padding:14px 20px;border-top:1px solid var(--line)}
.mcount{color:var(--faint);font-size:12px}
.cta[disabled]{opacity:.45;cursor:default;filter:none}
/* ── downloads tab ───────────────────────────────────── */
.dlpill{display:none;margin-left:auto;min-width:18px;padding:1px 6px;border-radius:10px;
  background:var(--accent);color:#fff;font-size:11px;text-align:center;font-weight:700}
.dllist{display:flex;flex-direction:column;gap:10px}
.dlrow{display:flex;align-items:center;gap:14px;background:var(--card);
  border:1px solid var(--line);border-radius:13px;padding:12px 14px}
.dlth{width:44px;height:44px;border-radius:8px;background-size:cover;background-position:center;flex:none}
.dlmeta{flex:1;min-width:0}
.dlt{font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dla{font-size:12px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:7px}
.dlbar{height:7px;border-radius:6px;background:#2a141a;overflow:hidden}
.dlbar>i{display:block;height:100%;border-radius:6px;background:linear-gradient(90deg,var(--accent),var(--accent2));
  transition:width .3s ease}
.dlbar.dl-done>i,.dlbar.dl-have>i{background:linear-gradient(90deg,var(--good),#2bd97e)}
.dlbar.dl-failed>i{background:var(--bad)}
.dlbar.dl-downloading>i{background-size:24px 24px;
  background-image:linear-gradient(90deg,var(--accent),var(--accent2)),
    repeating-linear-gradient(45deg,rgba(255,255,255,.14) 0 6px,transparent 6px 12px);
  animation:dlstripe 1s linear infinite}
@keyframes dlstripe{to{background-position:24px 0,24px 0}}
.dlstat{font-size:12px;min-width:74px;text-align:right;color:var(--dim)}
.dlstat.dl-done,.dlstat.dl-have{color:var(--good)}
.dlstat.dl-failed{color:var(--bad)}
/* ── library tab ─────────────────────────────────────── */
.nav-lib .xppill{margin-left:auto;font-size:11px;color:var(--dim);
  font-variant-numeric:tabular-nums}
.librow{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.librow input[type=text]{flex:1;min-width:240px;background:var(--card2);
  border:1px solid var(--line);border-radius:11px;color:var(--txt);
  padding:11px 14px;font-size:13px;font-family:inherit;outline:none}
.librow input[type=text]:focus{border-color:var(--accent)}
.libroots{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.libroot{display:inline-flex;align-items:center;gap:9px;background:var(--card2);
  border:1px solid var(--line);border-radius:20px;padding:5px 6px 5px 13px;
  font-size:12px;color:var(--dim);max-width:100%}
.libroot span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:rtl;
  text-align:left}
.libroot button{width:20px;height:20px;border-radius:50%;border:0;flex:none;
  background:rgba(255,255,255,.08);color:var(--dim);cursor:pointer;font-size:11px;
  line-height:1}
.libroot button:hover{background:var(--accent);color:#fff}
.libbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:18px 2px 16px}
.libseg{display:inline-flex;background:var(--card2);border:1px solid var(--line);
  border-radius:11px;overflow:hidden}
.libseg button{background:none;border:0;color:var(--dim);font:inherit;font-size:12.5px;
  padding:8px 15px;cursor:pointer}
.libseg button.on{background:var(--accent);color:#fff}
.libfind{flex:1;min-width:180px;max-width:340px;background:var(--card2);
  border:1px solid var(--line);border-radius:11px;color:var(--txt);padding:9px 13px;
  font-size:13px;font-family:inherit;outline:none}
.libfind:focus{border-color:var(--accent)}
.libscan{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums}
.libgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:16px}
.libcard{background:var(--card);border:1px solid var(--line);border-radius:16px;
  overflow:hidden;cursor:pointer;
  transition:transform .15s,box-shadow .15s,border-color .15s}
.libcard:hover{transform:translateY(-4px);border-color:rgba(232,34,60,.55);
  box-shadow:0 18px 44px -22px var(--glow)}
.libcard.open{border-color:var(--accent)}
.libart{position:relative;aspect-ratio:1;background:var(--card2) center/cover no-repeat}
.libart .n{position:absolute;right:9px;top:9px;background:rgba(7,3,4,.82);
  color:var(--accent2);font-size:11px;font-weight:700;padding:3px 8px;border-radius:20px}
.libart .go{position:absolute;left:9px;bottom:9px;width:34px;height:34px;border-radius:50%;
  background:rgba(7,3,4,.78);border:1px solid rgba(255,255,255,.14);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:12px;
  opacity:0;transition:opacity .15s,transform .15s,background .15s}
.libcard:hover .libart .go{opacity:1}
.libart .go:hover{background:var(--accent);transform:scale(1.1)}
.libcard .meta{padding:11px 13px 13px}
.libcard .lt{font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.libcard .la{color:var(--dim);font-size:12px;margin-top:1px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.libpanel{background:var(--card);border:1px solid var(--accent);border-radius:16px;
  padding:14px 16px;margin:16px 0}
.libpanel .ph{display:flex;align-items:center;gap:14px;margin-bottom:12px}
.libpanel .ph .pa2{width:64px;height:64px;border-radius:11px;flex:none;
  background:var(--card2) center/cover no-repeat}
.libpanel .ph .pn{font-size:16px;font-weight:700}
.libpanel .ph .ps{font-size:12px;color:var(--dim);margin-top:2px}
.libpanel .ph .grow{flex:1}
.libtracks{display:flex;flex-direction:column}
.ltrow{display:flex;align-items:center;gap:12px;padding:8px 10px;border-radius:10px;
  cursor:pointer}
.ltrow:hover{background:var(--card2)}
.ltrow.playing{background:rgba(232,34,60,.14)}
.ltrow.playing .ltt{color:var(--accent2)}
.ltrow .ltn{width:22px;text-align:right;color:var(--faint);font-size:12px;flex:none;
  font-variant-numeric:tabular-nums}
.ltrow .ltth{width:34px;height:34px;border-radius:7px;flex:none;
  background:var(--card2) center/cover no-repeat}
.ltrow .ltm{flex:1;min-width:0}
.ltrow .ltt{font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ltrow .lta{font-size:11.5px;color:var(--dim);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.ltrow .ltd{font-size:12px;color:var(--faint);font-variant-numeric:tabular-nums;flex:none}
/* The +/✕ only appears on hover so a long list stays quiet, but it keeps its
   width reserved — rows must not shift under the cursor. */
.ltrow .ltadd{width:26px;height:26px;flex:none;border-radius:50%;border:0;cursor:pointer;
  background:none;color:var(--faint);font-size:15px;line-height:1;opacity:0;
  transition:opacity .13s,background .13s,color .13s}
.ltrow:hover .ltadd{opacity:1}
.ltrow .ltadd:hover{background:var(--card);color:var(--txt)}
.ltrow .ltadd:focus-visible{opacity:1}
.plhead{display:flex;justify-content:flex-end;margin:-4px 0 12px}
.pl-modal .plsub{font-size:12px;color:var(--dim);padding:0 2px 8px}
.plpick{display:flex;flex-direction:column;gap:4px}
.plopt{display:flex;align-items:center;gap:10px;width:100%;text-align:left;cursor:pointer;
  padding:10px 12px;border-radius:10px;border:1px solid transparent;
  background:var(--card2);color:var(--txt);font:inherit;font-size:13.5px}
.plopt:hover{border-color:var(--accent);background:var(--card)}
.plopt:disabled{opacity:.5;cursor:default}
.plopt .pln{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.plopt .plc{font-size:11.5px;color:var(--faint);font-variant-numeric:tabular-nums}
.plnew{display:flex;gap:8px;margin-top:12px}
.plnew .plname{flex:1;min-width:0;background:var(--card2);border:1px solid var(--line);
  border-radius:10px;color:var(--txt);padding:11px 14px;font-size:13px;
  font-family:inherit;outline:none}
.plnew .plname:focus{border-color:var(--accent)}
.ltrow .ltx{font-size:9.5px;letter-spacing:.6px;text-transform:uppercase;color:var(--faint);
  border:1px solid var(--line);border-radius:20px;padding:2px 7px;flex:none}
.libmore{display:block;margin:16px auto 0}
.dlstat.dl-downloading{color:var(--accent2)}
/* a trigger button that fills up as its batch downloads (var --dlp = %) */
.dl-run{position:relative;overflow:hidden;cursor:default!important;color:#fff!important;
  border-color:var(--accent)!important;
  background:linear-gradient(90deg,var(--accent),var(--accent2)) no-repeat left center/var(--dlp,0%) 100%,#3a1420!important}
.dl-run.dl-fail{background:var(--bad)!important;border-color:var(--bad)!important}
.dlretry{margin-left:12px;font-size:12px;padding:6px 12px;border-radius:9px;cursor:pointer;flex:none;
  background:rgba(255,90,120,.1);border:1px solid var(--bad);color:var(--bad);white-space:nowrap}
.dlretry:hover{background:var(--bad);color:#fff}
.dlhead-retry{margin-left:12px;font-size:12px;padding:5px 12px;border-radius:20px;cursor:pointer;
  background:rgba(255,90,120,.12);border:1px solid var(--bad);color:var(--bad)}
.dlhead-retry:hover{background:var(--bad);color:#fff}
/* ── player controls ─────────────────────────────────── */
/* audio-only: keep the YouTube iframe alive but invisible. It lives at body
   level and never moves — re-parenting an iframe reloads it, which would
   restart the track every time the player expands. */
#pframe{position:fixed;left:-9999px;bottom:0;width:2px;height:2px;border:0;opacity:.01;
  pointer-events:none}
.pbtn{width:38px;height:38px;border-radius:50%;border:1px solid var(--line);background:var(--card);
  color:var(--txt);cursor:pointer;font-size:14px;display:flex;align-items:center;justify-content:center;
  flex:none}
.pbtn:hover{border-color:var(--accent);text-decoration:none}
.pbtn.big{width:46px;height:46px;background:var(--accent);border-color:var(--accent);color:#fff;
  font-size:16px;box-shadow:0 0 18px -4px var(--glow)}
.pbtn.big:hover{background:var(--accent2)}
.pbtn.on{color:var(--accent);text-shadow:0 0 12px var(--glow)}
.pbtn.off{opacity:.4}
#p-like.on{background:var(--accent);border-color:var(--accent);color:#fff;
  box-shadow:0 0 16px -2px var(--glow)}
#p-dis.on{background:var(--bad);border-color:var(--bad);color:#fff}
.pbar{width:100%;height:12px;display:flex;align-items:center;cursor:pointer;position:relative}
.pbar .rail{width:100%;height:4px;border-radius:2px;background:#33141d;overflow:hidden}
.pbar .fill{height:100%;width:0%;border-radius:2px;background:var(--accent);
  box-shadow:0 0 10px -2px var(--glow)}
.pbar:hover .rail{height:6px}
.rep1{font-size:9px;vertical-align:super;margin-left:1px}
.pcard.playing{border-color:var(--accent);box-shadow:0 0 22px -8px var(--glow)}
.pchip{cursor:pointer}
.lk{color:var(--accent2);margin-left:7px;font-size:12px}
/* XP+ nav item — iridescent to match the button */
.nav-xp .xppill{margin-left:auto;font-size:11px;color:#ff8a97}
.nav-xp.active,.nav-xp:hover{color:#fff}
.nav-xp.active .ic{filter:drop-shadow(0 0 6px rgba(232,34,60,.8))}
/* XP+ set cards */
.xpset{margin-bottom:26px}
.xpset .xph{display:flex;align-items:center;gap:12px;margin:0 2px 12px}
.xpset .xpname{font-size:20px;font-weight:800;letter-spacing:-.3px;
  background:linear-gradient(90deg,#c7a3ff,#ff8a97,#ffc078);-webkit-background-clip:text;background-clip:text;color:transparent}
.xpset .xpmeta{color:var(--dim);font-size:12.5px}
.xpset .xph .grow{flex:1}
@keyframes cardin{from{opacity:0;transform:translateY(16px) scale(.97)}to{opacity:1;transform:none}}
.pcard.reveal{animation:cardin .55s cubic-bezier(.2,.7,.2,1) both}
/* XP+ world tour: a globe in deep space that turns to face wherever each genre
   comes from. A sphere has no edges, so nothing can pan off into empty black. */
#xpfx{position:fixed;inset:0;z-index:300;display:none;overflow:hidden;
  background:radial-gradient(120% 90% at 50% 42%,#280711 0%,#0e0308 58%,#050104 100%)}
#xpfx.on{display:block;animation:xpfade .5s ease both}
#xpfx.out{animation:xpout .55s ease forwards}
@keyframes xpfade{from{opacity:0}to{opacity:1}}
@keyframes xpout{to{opacity:0;visibility:hidden}}
.xpstars{position:absolute;inset:0;pointer-events:none}
.xpstars i{position:absolute;width:2px;height:2px;border-radius:50%;
  background:#dbe9ff;animation:xptwinkle 4s ease-in-out infinite}
@keyframes xptwinkle{0%,100%{opacity:.15}50%{opacity:.9}}
.xpstage{position:absolute;inset:0;display:grid;place-items:center;
  transition:transform 1.4s cubic-bezier(.4,0,.2,1)}
.xpstage canvas{display:block}
.xpveil{position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(0deg,rgba(2,4,10,.92),transparent 30%)}
.xptop{position:absolute;top:28px;left:0;right:0;text-align:center;
  color:rgba(255,255,255,.45);font-size:12px;letter-spacing:.24em;text-transform:uppercase}
.xphud{position:absolute;left:46px;right:46px;bottom:44px;
  display:flex;align-items:flex-end;gap:30px}
.xpwhere{min-width:0}
.xpgenre{font-size:42px;font-weight:800;letter-spacing:-.9px;line-height:1.04;
  background:linear-gradient(90deg,#ff5f75,#ff9a8a,#ffd6a0);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.xpplace{color:rgba(255,255,255,.55);font-size:14px;margin-top:9px;
  font-variant-numeric:tabular-nums}
.xpfeed{flex:1;min-width:0;list-style:none;margin:0;padding:0;text-align:right;
  font-size:13px;color:rgba(255,255,255,.4)}
.xpfeed li{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:3px 0;
  animation:xpfeedin .45s cubic-bezier(.2,.7,.2,1) both}
.xpfeed li:first-child{color:rgba(255,255,255,.88)}
@keyframes xpfeedin{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){
  .xpstage{transition:none}.xpstars i{animation:none}}
</style></head><body>
<div class="app">
<aside class="side">
  <div class="brand"><img class="wm" src="/assets/wordmark-420.png" alt="Music XP"></div>
  <nav>
    <button data-tab="today" class="active">
      <svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M14.8 9.2l-1.9 4.5-4.5 1.9 1.9-4.5z"/></svg>
      Explore</button>
    <button data-tab="xp" class="nav-xp">
      <svg class="ic" viewBox="0 0 24 24"><path d="M12 2l2.4 6.9L21 9.6l-5.2 4.3 1.7 6.9L12 17.3 6.5 20.8l1.7-6.9L3 9.6l6.6-.7z"/></svg>
      XP+<span class="xppill">✦</span></button>
    <button data-tab="irish" class="nav-irish">
      <svg class="ic" viewBox="0 0 24 24"><path d="M12 21c-1.2-3.6-4.5-4.8-4.5-8.2A4.5 4.5 0 0112 8.3a4.5 4.5 0 014.5 4.5c0 3.4-3.3 4.6-4.5 8.2z"/><path d="M12 8.3V3M12 5.2c1.6 0 2.9-.9 2.9-2.2-1.6 0-2.9.9-2.9 2.2zM12 5.2c-1.6 0-2.9-.9-2.9-2.2 1.6 0 2.9.9 2.9 2.2z"/></svg>
      Irish Mode<span class="xppill">☘</span></button>
    <button data-tab="timeline" class="nav-time">
      <svg class="ic" viewBox="0 0 24 24"><path d="M3 12h18"/><circle cx="7" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="17" cy="12" r="2"/><path d="M7 8V6M12 8V6M17 8V6"/></svg>
      Timeline<span class="xppill">’08</span></button>
    <button data-tab="digest" class="nav-digest">
      <svg class="ic" viewBox="0 0 24 24"><path d="M4 5h16M4 5v14a1 1 0 001 1h14a1 1 0 001-1V5M8 10h8M8 14h5"/></svg>
      Weekly Digest</button>
    <button data-tab="liked">
      <svg class="ic" viewBox="0 0 24 24"><path d="M12 20.5S4 15.2 4 9.7C4 6.9 6.1 5 8.5 5c1.5 0 2.8.8 3.5 2 .7-1.2 2-2 3.5-2C17.9 5 20 6.9 20 9.7c0 5.5-8 10.8-8 10.8z"/></svg>
      Liked</button>
    <button data-tab="history">
      <svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/></svg>
      History</button>
    <button data-tab="downloads">
      <svg class="ic" viewBox="0 0 24 24"><path d="M12 3v11M7 10l5 5 5-5M4 20h16"/></svg>
      Downloads<span class="dlpill" id="dlpill"></span></button>
    <button data-tab="get">
      <svg class="ic" viewBox="0 0 24 24"><path d="M10 13a5 5 0 007 0l2-2a5 5 0 00-7-7l-1 1"/><path d="M14 11a5 5 0 00-7 0l-2 2a5 5 0 007 7l1-1"/></svg>
      Get</button>
    <button data-tab="library" class="nav-lib">
      <svg class="ic" viewBox="0 0 24 24"><path d="M9 18V6l11-2v12"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/><path d="M9 9l11-2"/></svg>
      Library<span class="xppill" id="libpill"></span></button>
    <button data-tab="profiles">
      <svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.8 2.4 2.8 15.6 0 18M12 3c-2.8 2.4-2.8 15.6 0 18"/></svg>
      Disco Bowl<span class="livepill">LIVE</span></button>
    <button data-tab="stats">
      <svg class="ic" viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M4 20h16"/></svg>
      Stats</button>
    <button id="nav-playlist" title="Open today's playlist on YouTube Music">
      <svg class="ic" viewBox="0 0 24 24"><path d="M4 6h11M4 11h11M4 16h6"/><circle cx="16.5" cy="16.5" r="2.5"/><path d="M19 16.5V8.5l3 1"/></svg>
      My Playlist<span class="ext">↗</span></button>
    <button data-tab="controls">
      <svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1"/></svg>
      Settings</button>
  </nav>
  <div class="np-host" id="np-host">
    <div class="nowplay idle" id="nowplay">
      <div class="np-art" id="np-art" title="Double-click for full screen">
        <img id="p-art" alt="">
        <button class="np-exp" id="p-exp" title="Full screen (F)">⤢</button>
      </div>
      <div class="np-info">
        <div class="np-t" id="p-t"></div>
        <div class="np-a" id="p-a"></div>
      </div>
      <div class="np-prog">
        <div class="pbar" id="p-bar"><div class="rail"><div class="fill" id="p-fill"></div></div></div>
        <div class="np-times"><span id="p-cur">0:00</span><span id="p-q"></span>
          <span id="p-dur">0:00</span></div>
      </div>
      <div class="np-tr">
        <button class="pbtn" id="p-shuf" title="Shuffle (S)">⤨</button>
        <button class="pbtn" id="p-prev" title="Previous (P)">⏮</button>
        <button class="pbtn big" id="p-play" title="Play / pause (Space)">▶</button>
        <button class="pbtn" id="p-next" title="Next (N)">⏭</button>
        <button class="pbtn" id="p-rep" title="Repeat (R)">↻</button>
      </div>
      <div class="np-vol">
        <button class="pbtn sm" id="p-like" title="Like — also saves to your YouTube likes (L)">♥</button>
        <button class="pbtn sm" id="p-dis" title="Not for me (X)">✕</button>
        <button class="pbtn sm" id="p-pl" title="Add to a playlist">+</button>
        <button class="pbtn sm" id="p-que" title="Queue (Q)">≣</button>
        <a class="pbtn sm" id="p-yt" target="_blank" title="Open in YT Music">↗</a>
        <button class="pbtn sm" id="p-close" title="Stop">▾</button>
      </div>
    </div>
  </div>
</aside>
<div class="main">
  <div class="topbar">
    <div class="sp"></div>
    <div class="search">
      <svg class="ic" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M16.5 16.5L21 21"/></svg>
      <input id="q" placeholder="Search artists, tracks, languages…"></div>
  </div>
  <section id="today" class="tab active"></section>
  <section id="xp" class="tab"></section>
  <section id="irish" class="tab"></section>
  <section id="timeline" class="tab"></section>
  <section id="digest" class="tab"></section>
  <section id="liked" class="tab"></section>
  <section id="history" class="tab"></section>
  <section id="downloads" class="tab"></section>
  <section id="get" class="tab"></section>
  <section id="library" class="tab"></section>
  <section id="profiles" class="tab"></section>
  <section id="stats" class="tab"></section>
  <section id="controls" class="tab"></section>
</div>
</div>
<div id="xpfx" aria-hidden="true">
  <div class="xpstars" id="xpfx-stars"></div>
  <div class="xpstage" id="xpfx-stage"></div>
  <div class="xpveil"></div>
  <div class="xptop" id="xpfx-title">World tour</div>
  <div class="xphud">
    <div class="xpwhere">
      <div class="xpgenre" id="xpfx-genre">Plotting a route</div>
      <div class="xpplace" id="xpfx-place">warming up the globe…</div>
    </div>
    <ul class="xpfeed" id="xpfx-feed"></ul>
  </div>
</div>
<div id="queue">
  <div class="qh"><h3>Queue</h3><span class="qsub" id="q-sub"></span>
    <button class="mx" id="q-x" title="Close">✕</button></div>
  <div class="qlist" id="q-list"></div>
</div>
<iframe id="pframe" allow="autoplay; encrypted-media"></iframe>
<audio id="lplay" preload="none"></audio>
<div id="npfull">
  <div class="nf-bg"><i id="nf-bg"></i></div>
  <div class="nf-bg b2"><i id="nf-bg2"></i></div>
  <div class="nf-bg b3"><i id="nf-bg3"></i></div>
  <div class="nf-scrim"></div>
  <button class="nf-x" id="nf-x" title="Close (Esc)">✕</button>
  <div class="nf-in">
    <div class="nf-left">
      <div class="nf-art"><img id="nf-art" alt=""></div>
      <div id="nf-host"></div>
    </div>
    <div class="nf-lyrwrap" id="nf-lyrwrap">
      <div class="nf-lyr" id="nf-lyr"></div>
      <button class="lyr-add" id="lyr-add">lyrics studio</button>
    </div>
  </div>
</div>

<!-- ── lyrics studio ──────────────────────────────────────────────────────────
     Everything that can be done to a lyric in one room: the words, their times,
     and where they came from. It sits over the full-screen player because the
     audio is the instrument you edit against — you cannot fix a timing you
     cannot hear. -->
<div class="lst-ovl" id="lst" hidden>
 <div class="lst">
  <div class="lst-h">
    <div class="lst-id">
      <div class="lst-t" id="lst-title">—</div>
      <div class="lst-a" id="lst-artist"></div>
    </div>
    <div class="lst-badge" id="lst-badge"></div>
    <button class="lst-btn prim" id="lst-save" disabled>Save</button>
    <button class="lst-x" id="lst-x" title="Close (Esc)">&times;</button>
  </div>
  <div class="lst-tabs">
    <button data-tab="sync" class="on">Sync</button>
    <button data-tab="words">Words</button>
    <button data-tab="src">Sources</button>
    <span class="lst-note" id="lst-note"></span>
  </div>

  <div class="lst-body">
    <!-- Sync: the line list is the editor and the preview at once — it lights up
         with the audio, so a bad time is visible before you go looking for it. -->
    <section class="lst-pane" id="lst-pane-sync">
      <div class="lst-tools">
        <span class="lst-lbl">Nudge every line</span>
        <button class="lst-btn" data-off="-0.5">−0.5</button>
        <button class="lst-btn" data-off="-0.1">−0.1</button>
        <span class="lst-off" id="lst-offv">0.00s</span>
        <button class="lst-btn" data-off="0.1">+0.1</button>
        <button class="lst-btn" data-off="0.5">+0.5</button>
        <span class="lst-sep"></span>
        <button class="lst-btn" id="lst-tap">Tap sync</button>
      </div>
      <div class="lst-lines" id="lst-lines"></div>
      <div class="lst-taphint" id="lst-taphint" hidden>
        <b>Space</b> stamps the lit line and moves on ·
        <b>&larr;</b> back a line · <b>&rarr;</b> skip · <b>Esc</b> stops
      </div>
    </section>

    <!-- Words: stamps ride along in the text, so a typo can be fixed without
         throwing away the timing that was already right. -->
    <section class="lst-pane" id="lst-pane-words" hidden>
      <textarea id="lst-ta" spellcheck="false" placeholder="One line per line.

Times in front are kept: [00:34.20] or 0:34
Lines without one stay untimed until you tap them in."></textarea>
      <div class="lst-row">
        <span class="lst-hint">Drop a .txt or .lrc file anywhere here.</span>
        <button class="lst-btn prim" id="lst-apply">Use these words</button>
      </div>
    </section>

    <!-- Sources: what the picker passed over. A wrong lyric is usually a right
         lyric for a different cut of the song. -->
    <section class="lst-pane" id="lst-pane-src" hidden>
      <div class="lst-row lst-srcbar">
        <span class="lst-hint">Other versions of this song.</span>
        <button class="lst-btn" id="lst-revert" hidden>Undo last save</button>
        <button class="lst-btn" id="lst-tx"></button>
      </div>
      <div class="lst-cands" id="lst-cands"></div>
    </section>
  </div>

  <div class="lst-foot">
    <button class="lst-btn lst-play" id="lst-play">&#9654;</button>
    <span class="lst-time" id="lst-cur">0:00</span>
    <div class="lst-bar" id="lst-bar"><div class="lst-fill" id="lst-fill"></div></div>
    <span class="lst-time" id="lst-dur">0:00</span>
  </div>
 </div>
</div>
<script>
let STATE=null, QUERY="", PLAYLIST=[], CURVID=null, YTLIKED=null, YTLOADING=false;
let STATSDATA=null;
let XP_REVEAL=false;
const OPENDAYS=new Set();
// video_ids liked directly on YouTube; folded into pending picks so hearts sync.
let YTLIKEDSET=new Set();
function applyYtLikes(picks){(picks||[]).forEach(p=>{
  if(p.video_id&&YTLIKEDSET.has(p.video_id)&&(!p.outcome||p.outcome==='pending'))
    p.outcome='liked';}); return picks;}
let DLS={active:false,total:0,items:[]}, DLWAS=false;
const $=s=>document.querySelector(s), el=(t,c,h)=>{const e=document.createElement(t);
if(c)e.className=c; if(h!=null)e.innerHTML=h; return e;};
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// Local files carry a 'local:<id>' video_id, so one player queue can hold both
// YouTube picks and tracks read off the disk.
const isLocal=v=>typeof v==='string'&&v.startsWith('local:');
const localId=v=>v.slice(6);
// Covers are cached by the browser for a week, so a replaced image would stay
// invisible behind the old one. ARTREV changes whenever the art cache is
// written to, which makes the swapped cover a different URL.
let ARTREV=0;
const thumb=v=>!v?"/assets/logo-256.png?v=3"
  :isLocal(v)?'/api/local/cover?id='+encodeURIComponent(localId(v))+'&v='+ARTREV
  :`https://i.ytimg.com/vi/${v}/hqdefault.jpg`;

// ── optimistic like/dislike ──
// A heart tap cycles pending → like → dislike → clear. Old code awaited the POST
// then did a full load()/render(), so every tap flickered the whole page. Now we
// flip the pick + its heart in place instantly and POST in the background; the
// server mirrors to YouTube async. A debounced quiet refresh keeps STATE (counts,
// Liked list) eventually consistent without re-rendering under the user.
const HEARTED=p=>p.outcome==='liked'||p.outcome==='disliked';
function paintHeart(p,hc,card){
  hc.classList.toggle('lk',p.outcome==='liked');
  hc.classList.toggle('dk',p.outcome==='disliked');
  hc.textContent=HEARTED(p)?'♥':'♡';
  if(card)card.classList.toggle('disliked',p.outcome==='disliked');
}
let _likeSyncT=null;
function quietSync(){clearTimeout(_likeSyncT); _likeSyncT=setTimeout(async()=>{
  try{STATE=await(await fetch('/api/state')).json();}catch(e){}
  YTLIKEDSET=new Set((YTLIKED||[]).map(t=>t.video_id).filter(Boolean));
},1200);}
function postOutcome(video_id,ep){
  fetch(ep,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({video_id})}).catch(()=>{});
  quietSync();
}
function repaintVid(vid){const p=PLAYLIST.find(x=>x.video_id===vid); if(!p)return;
  document.querySelectorAll('.pcard').forEach(c=>{if(c.dataset.vid!==vid)return;
    const hc=c.querySelector('.hchip'); if(hc)paintHeart(p,hc,c);});}
function cycleLike(p,hc,card){
  const ep=p.outcome==='liked'?'/api/dislike':p.outcome==='disliked'?'/api/clear':'/api/like';
  p.outcome=p.outcome==='liked'?'disliked':p.outcome==='disliked'?'pending':'liked';
  paintHeart(p,hc,card); syncPlayerBtns(); postOutcome(p.video_id,ep);
}

document.querySelectorAll('.side nav button[data-tab]').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.side nav button').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); $('#'+b.dataset.tab).classList.add('active');
  if(b.dataset.tab==='liked'&&YTLIKED===null)loadYtLiked();
  if(b.dataset.tab==='downloads')pollDownloads();
  if(b.dataset.tab==='library'){if(LIB===null)loadLibrary(); else renderLibrary();}
  if(b.dataset.tab==='stats')loadStats();
});
$('#q').oninput=e=>{QUERY=e.target.value.toLowerCase(); renderToday();};

async function load(){STATE=await (await fetch('/api/state')).json(); STATSDATA=null; render();}
function render(){renderNav();renderNowPlaying();renderToday();renderXP();renderIrish();renderTimeline();renderDigest();renderLiked();renderHistory();renderDownloads();renderGet();renderProfiles();renderControls();paintRuns();}

// ── derived stats ──
function stats(){
  const enabled=STATE.languages.filter(l=>l.enabled);
  const tracks=STATE.history.reduce((s,d)=>s+d.count,0);
  const liked=STATE.history.reduce((s,d)=>s+(d.liked||0),0);
  const markets=new Set(); enabled.forEach(l=>l.markets.forEach(m=>markets.add(m)));
  const langs=enabled.filter(l=>l.artist_count>0).length;
  const per=180, xp=liked*15+tracks*3;
  const level=Math.floor(xp/per)+1, pct=Math.round((xp%per)/per*100);
  return {tracks,countries:markets.size,langs,xp,level,pct,next:level*per};
}

function renderNav(){
  const d=STATE.history.find(x=>x.playlist_id), b=$('#nav-playlist');
  b.style.opacity=d?1:.4;
  b.onclick=()=>{if(d)window.open('https://music.youtube.com/playlist?list='+d.playlist_id,'_blank');};
}

// What the player shows when nothing is loaded: the first pick of the day, as
// an invitation. Only the artwork and title are painted — the transport stays
// inert until that suggestion is actually pressed.
function suggestion(){const picks=(STATE&&STATE.today.picks)||[];
  return picks.find(x=>x.video_id&&x.outcome!=='disliked')
    ||picks.find(x=>x.video_id)||null;}

function renderNowPlaying(){
  const np=$('#nowplay'), host=$('#np-host');
  if(CURVID){np.classList.remove('idle'); host.style.display=''; return;}
  const p=suggestion();
  host.style.display=p?'':'none';
  if(!p)return;
  np.classList.add('idle');
  paintArt(p.video_id);
  $('#p-t').textContent=p.title; $('#p-a').textContent=p.artist;
  $('#p-play').textContent='▶';
}

function badge(o){const m={liked:'b-liked',disliked:'b-disliked',skipped:'b-skipped',pending:'b-pending'};
  return `<span class="badge ${m[o]||'b-pending'}">${o}</span>`;}

// Artist-tile wall. HERO_ART is fetched once per day and cached; the day's
// artist photos become square facets tiling the card, the band rolling sideways
// at a constant rate (CSS). Square crops -> no stretch, no side gaps.
let HERO_ART=null, HERO_HTML='';
function ensureHeroArt(){
  const t=STATE.today||{};
  const bg=document.querySelector('#today .hero .hero-bg');
  // Re-entering the tab reuses the wall already built this session — rebuilding
  // it would restart the roll and flash the photos in again.
  if(HERO_HTML&&bg){bg.innerHTML=HERO_HTML;return;}
  if(HERO_ART&&HERO_ART.date===(t.date||'')){paintHeroStrip();return;}
  fetch('/api/hero-art').then(r=>r.json()).then(d=>{
    HERO_ART={date:(t.date||''),artists:(d.artists||[])}; paintHeroStrip();
  }).catch(()=>{});
}
function paintHeroStrip(){
  const bg=document.querySelector('#today .hero .hero-bg'); if(!bg)return;
  const imgs=((HERO_ART&&HERO_ART.artists)||[]).map(a=>a.image).filter(Boolean);
  if(!imgs.length){bg.innerHTML='';return;}
  const GAP=3, H=bg.clientHeight||380, W=bg.clientWidth||1000;
  // Rows divide the card height exactly, so no row is sliced by the top/bottom edge.
  const ROWS=Math.max(3,Math.round((H+GAP)/99));
  const TILE=(H-(ROWS-1)*GAP)/ROWS, STEP=TILE+GAP;
  // One copy must be wider than the card, else the loop seam becomes visible.
  const COLS=Math.max(14,Math.ceil(W/STEP)+4);
  // Grid flows column-first, drawing from a shuffled deck that is only refilled
  // once every artist has been placed — so a face can never recur until all the
  // others have had a turn. On top of that each pick avoids its already placed
  // neighbours (left, above, both diagonals) so lookalikes never sit together.
  // The last column also avoids the first, its neighbour where the loop wraps.
  const cells=[]; let deck=[];
  const at=(c,r)=>(c<0||r<0||r>=ROWS)?0:cells[c*ROWS+r];
  const draw=bad=>{
    for(let pass=0;pass<2;pass++){
      if(!deck.length) deck=imgs.slice().sort(()=>Math.random()-.5);
      const i=deck.findIndex(s=>!bad.includes(s));
      if(i>=0) return deck.splice(i,1)[0];
      deck=[];                       // whole deck conflicts — reshuffle and retry
    }
    return imgs[(Math.random()*imgs.length)|0];
  };
  for(let c=0;c<COLS;c++)for(let r=0;r<ROWS;r++){
    const bad=[at(c-1,r),at(c,r-1),at(c-1,r-1),at(c-1,r+1)];
    if(c===COLS-1) bad.push(at(0,r),at(0,r-1),at(0,r+1));
    cells.push(draw(bad));
  }
  const tiles=cells.concat(cells).map(src=>              // duplicated = seamless
    `<img class="hero-facet" src="${src}" alt="" referrerpolicy="no-referrer">`
  ).join('');
  const roll=Math.round(COLS*STEP/22);                   // constant ~22px/s
  HERO_HTML=`<div class="hero-mosaic" style="--rows:${ROWS};--roll:${roll}s;`+
    `--tile:${TILE.toFixed(2)}px">`+tiles+`</div>`;
  // Every tile is decoded before the wall is inserted, so it arrives complete
  // instead of popping in photo by photo. A stalled image can't hold it up.
  Promise.race([
    Promise.all(imgs.map(src=>{const i=new Image(); i.src=src;
      return (i.decode?i.decode():Promise.resolve()).catch(()=>{});})),
    new Promise(r=>setTimeout(r,4000)),
  ]).then(()=>{const b=document.querySelector('#today .hero .hero-bg');
    if(b)b.innerHTML=HERO_HTML;});
}

function renderToday(){const t=STATE.today, root=$('#today'), st=stats(); root.innerHTML='';
  const h=new Date().getHours(), greet=h<12?'morning':h<18?'afternoon':'evening';
  const hero=el('div','hero');
  // Cinematic backdrop: a scrolling wall of the day's artists' photos (one per
  // artist, so nothing repeats). Painted async once /api/hero-art resolves.
  hero.innerHTML=`<div class="hero-bg"></div><div class="hero-tint"></div><div class="hl">
    <div class="greet">Good ${greet}, zEi 🤘</div>
    <h2>New music.<br><em>Discovered.</em><br>Everyday.</h2>
    <p>Music XP scouts the planet for fresh sounds so you don't have to.${
      t.date?` Latest drop: ${esc(t.date)} · ${t.picks.length} tracks · ${st.langs} languages.`:''}</p>
    <div class="toolbar">
      <button class="cta xp" id="xpbtn" title="Out of your comfort zone — genres, artists &amp; languages you've never heard, from any era. Builds a fresh XP playlist on YouTube every press.">XP+ · Surprise me ✦</button>
      <button class="cta ghost" id="digestbtn" title="Roll your liked keepers from the last 7 days into one playlist">✦ Weekly digest</button>
    </div></div>`;
  const lg=el('pre'); lg.id='log';
  hero.querySelector('#xpbtn').onclick=runXP;
  hero.querySelector('#digestbtn').onclick=runDigest;

  const wrap=el('div','explore');
  const left=el('div');
  // Hero sits inside the left column so the rail starts level with it instead of
  // being pushed a full hero-height down the page.
  left.appendChild(hero); left.appendChild(lg);
  let picks=applyYtLikes(t.picks);
  if(QUERY) picks=picks.filter(p=>(p.title+' '+p.artist+' '+p.language).toLowerCase().includes(QUERY));
  const sh=el('div','sec-h');
  sh.innerHTML=`<h3>Today's Picks <span class="spark">✦</span></h3>
    <div class="sh-r"><span class="cnt">${picks.length} tracks</span>
    <button class="cta ghost sm" id="dlbtn" title="Download every track here as audio files">⬇ Download all</button></div>`;
  left.appendChild(sh);
  const db=sh.querySelector('#dlbtn'); if(db)db.onclick=downloadNow;
  if(!picks.length){left.appendChild(el('div','gcard muted',
    QUERY?'No picks match your search.':'No picks yet — the daily scout runs each morning, or hit XP+ to explore now.'));}
  else{const grid=el('div','cards');
    PLAYLIST=picks;
    picks.forEach(p=>{const c=el('div','pcard'+(p.outcome==='disliked'?' disliked':''));
      if(p.video_id)c.dataset.vid=p.video_id;
      c.innerHTML=`<div class="thumb" style="background-image:url('${thumb(p.video_id)}')">
        <div class="sc">${p.score.toFixed(2)}</div>
        ${p.source==='xp'?`<div class="xpb" title="Out-of-comfort-zone discovery">${esc(p.xp_set||'XP')}</div>`:''}
        ${p.video_id?`<button class="pchip" title="Play here">▶</button>
        <button class="dchip" title="Download this track">⬇</button>
        <button class="hchip${p.outcome==='liked'?' lk':p.outcome==='disliked'?' dk':''}"
          title="Tap: like ♥ · again: dislike · again: clear">${
          p.outcome==='liked'||p.outcome==='disliked'?'♥':'♡'}</button>`:''}</div>
        <div class="meta"><div class="pt">${esc(p.title)}</div>
        <div class="pa">${esc(p.artist)}</div><span class="pl">${esc(p.language)}</span></div>`;
      const chip=c.querySelector('.pchip');
      if(chip)chip.onclick=()=>{PLAYLIST=picks;
        p.video_id===CURVID?$('#p-play').onclick():playTrack(p.video_id);};
      const dc=c.querySelector('.dchip');
      if(dc)dc.onclick=()=>dlOne(p.video_id,dc);
      const hc=c.querySelector('.hchip');
      if(hc)hc.onclick=()=>cycleLike(p,hc,c);
      grid.appendChild(c);});
    left.appendChild(grid);}
  wrap.appendChild(left);
  if(CURVID){syncPlayerBtns();markPlaying();}

  const rail=el('div','rail');
  rail.innerHTML=`<div class="card">
    <div class="db-h"><span class="dbt">Disco Bowl</span><span class="livepill">LIVE</span></div>
    <div class="dbsub">Scouting the world…</div>
    <div class="dscene">
      ${[[7,26],[14,66],[22,40],[30,80],[38,16],[48,58],[56,30],[64,76],[72,46],[80,68],[88,28],[12,88],[52,90],[84,10],[93,54]]
        .map((s,i)=>`<span class="spk" style="left:${s[0]}%;top:${s[1]}%;animation-delay:${(i*0.4).toFixed(1)}s"></span>`).join('')}
      <div class="dwire"></div>
      <canvas class="dball" id="discoball" width="224" height="224"></canvas>
    </div>
    <div class="stats">
      <div class="stat"><div class="n">${st.tracks}</div><div class="l">Tracks Found</div></div>
      <div class="stat"><div class="n">${st.countries}</div><div class="l">Countries</div></div>
      <div class="stat"><div class="n">${st.langs}</div><div class="l">Languages</div></div>
    </div></div>
    <div class="card xp">
      <div class="row"><span class="t">XP Level</span><span class="lv">Lv. ${st.level}</span></div>
      <div class="bar"><i style="width:${st.pct}%"></i></div>
      <div class="a" style="margin-top:9px">${st.xp.toLocaleString()} / ${st.next.toLocaleString()} XP</div>
      <div class="a" style="margin-top:3px;color:var(--faint)">Keep exploring. Level up your taste.</div>
    </div>`;
  wrap.appendChild(rail);
  root.appendChild(wrap);
  ensureHeroArt();          // needs the hero laid out — tile size is measured
  startDisco();
}

// ── XP+ tab: every out-of-comfort-zone playlist you've made ──
// Every on-demand mode (XP+, Irish, Timeline) shows the same thing: a header,
// its published playlists newest first, and — for the modes whose button isn't
// on the Explore hero — a run button and log right in the tab.
function renderSets(o){
  const root=$('#'+o.id); if(!root)return; root.innerHTML='';
  const sets=(STATE[o.key]||[]);
  const sh=el('div','sec-h');
  sh.innerHTML=`<h3>${o.title} <span class="spark">${o.spark}</span></h3>
    <div class="sh-r"><span class="cnt">${sets.length} playlist${sets.length===1?'':'s'}</span></div>`;
  root.appendChild(sh);
  if(o.run){
    const b=el('button','cta '+(o.btnClass||''),o.runLabel);
    b.id=o.id+'-run'; if(o.hint)b.title=o.hint;
    b.onclick=()=>o.run();
    sh.querySelector('.sh-r').appendChild(b);
    const lg=el('pre'); lg.id=o.id+'-setlog'; root.appendChild(lg);
  }
  if(o.blurb)root.appendChild(el('div','gcard muted',o.blurb));
  if(!sets.length){root.appendChild(el('div','gcard muted',o.empty));
    if(o.id==='xp')XP_REVEAL=false; return;}
  sets.forEach((s,si)=>{
    let picks=applyYtLikes(s.picks||[]);
    if(QUERY) picks=picks.filter(p=>(p.title+' '+p.artist+' '+p.language).toLowerCase().includes(QUERY));
    const box=el('div','xpset');
    const pl=s.playlist_id?`<a class="cta ghost sm" href="https://music.youtube.com/playlist?list=${s.playlist_id}" target="_blank">Open on YouTube ↗</a>`:'';
    const h=el('div','xph');
    h.innerHTML=`<span class="xpname">${esc(s.name)}</span>
      <span class="xpmeta">${picks.length} tracks · ${esc(s.date||'')}</span><span class="grow"></span>`;
    box.appendChild(h);
    const acts=el('div'); acts.style.display='flex'; acts.style.gap='10px';
    const dlb=el('button','cta ghost sm','⬇ Download all');
    dlb.onclick=()=>openDownloadPicker('Download — '+s.name,s.picks||[],dlb);
    acts.appendChild(dlb);
    if(pl){const a=document.createElement('span'); a.innerHTML=pl; acts.appendChild(a.firstChild);}
    h.appendChild(acts);
    if(!picks.length){box.appendChild(el('div','gcard muted','No tracks match your search.'));}
    else{const grid=el('div','cards');
      picks.forEach((p,i)=>{const c=trackCard(p,picks);
        // Reveal only the newest set right after a fresh run, staggered.
        if(XP_REVEAL&&si===0){c.classList.add('reveal');
          c.style.animationDelay=(i*0.04).toFixed(2)+'s';}
        grid.appendChild(c);});
      box.appendChild(grid);}
    root.appendChild(box);
  });
  XP_REVEAL=false;
  if(CURVID)markPlaying();
}

function renderXP(){renderSets({
  id:'xp', key:'xp_sets', title:'XP+', spark:'✦',
  empty:"No XP+ playlists yet. Hit XP+ · Surprise me to explore genres, artists and languages you've never heard."});}

function renderIrish(){renderSets({
  id:'irish', key:'irish_sets', title:'Irish Mode', spark:'☘',
  btnClass:'irish', runLabel:'☘ Start a session', run:runIrish,
  hint:'Irish and Celtic traditional music — fiddle, uilleann pipes, bagpipes, tin whistle and bodhrán. Builds a fresh Irish playlist on YouTube every press.',
  blurb:'Irish Mode goes deep on one place instead of wide across the world: the tradition itself, plus anything carried by the instruments that define it. Artists only count if their own tags place them in the Irish/Celtic world, so the pipes stay Irish rather than drifting into bluegrass.',
  empty:'No sessions yet. Hit “☘ Start a session” to pull a set of fiddles, pipes and whistles.'});}

function renderTimeline(){renderSets({
  id:'timeline', key:'time_sets', title:'Timeline · 2008–2013', spark:'◷',
  btnClass:'time', runLabel:'◷ Take me back', run:runTimeline,
  hint:'The biggest English-language songs of 2008–2013, roughly five a year across the genres that defined the era.',
  blurb:'One press walks the whole window and comes back with a period mixtape — about five tracks a year. Popularity comes from Last.fm’s year charts, then every track is checked against iTunes and dropped unless its earliest release really lands inside 2008–2013.',
  empty:'No sets yet. Hit “◷ Take me back” to build a 2008–2013 mixtape.'});}

// ── Weekly Digest tab: each published digest playlist, newest first ──
function renderDigest(){const root=$('#digest'); if(!root)return; root.innerHTML='';
  const sets=(STATE.digests||[]);
  const sh=el('div','sec-h');
  sh.innerHTML=`<h3>Weekly Digest <span class="spark">◷</span></h3>
    <span class="cnt">${sets.length} playlist${sets.length===1?'':'s'}</span>`;
  root.appendChild(sh);
  root.appendChild(el('div','gcard muted',
    "A digest rolls your best liked keepers from the last few days into one lasting playlist — the daily lists are disposable, this saves the gems. Build one with “✦ Weekly digest” on the Explore page."));
  if(!sets.length){root.appendChild(el('div','gcard muted',
    'No digests yet. Hit “✦ Weekly digest” on the Explore page to build one from your recent liked tracks.'));return;}
  sets.forEach(s=>{
    let picks=applyYtLikes(s.picks||[]);
    if(QUERY) picks=picks.filter(p=>(p.title+' '+p.artist+' '+p.language).toLowerCase().includes(QUERY));
    const box=el('div','xpset');
    const pl=s.playlist_id?`<a class="cta ghost sm" href="https://music.youtube.com/playlist?list=${s.playlist_id}" target="_blank">Open on YouTube ↗</a>`:'';
    const h=el('div','xph');
    h.innerHTML=`<span class="xpname">${esc(s.name)}</span>
      <span class="xpmeta">${picks.length} tracks · last ${s.days||7} days · ${esc(s.date||'')}</span><span class="grow"></span>`;
    box.appendChild(h);
    const acts=el('div'); acts.style.display='flex'; acts.style.gap='10px';
    const dlb=el('button','cta ghost sm','⬇ Download all');
    dlb.onclick=()=>openDownloadPicker('Download — '+s.name,s.picks||[],dlb);
    acts.appendChild(dlb);
    if(pl){const a=document.createElement('span'); a.innerHTML=pl; acts.appendChild(a.firstChild);}
    h.appendChild(acts);
    if(!picks.length){box.appendChild(el('div','gcard muted','No tracks match your search.'));}
    else{const grid=el('div','cards');
      picks.forEach(p=>grid.appendChild(trackCard(p,picks)));
      box.appendChild(grid);}
    root.appendChild(box);
  });
  if(CURVID)markPlaying();
}

// ── Liked tab: every ♥ from the UI, merged with your YouTube Liked Music ──
function renderLiked(){const root=$('#liked'); if(!root)return; root.innerHTML='';
  const local=(STATE.liked||[]).map(p=>({...p,local:true,outcome:p.outcome||'liked'}));
  const have=new Set(local.map(p=>p.video_id));
  const yt=(YTLIKED||[]).filter(t=>t.video_id&&!have.has(t.video_id))
    .map(t=>({...t,outcome:'liked'}));
  const all=[...local,...yt];
  const sh=el('div','sec-h');
  sh.innerHTML=`<h3>Liked Songs <span class="spark">♥</span></h3>
    <span class="cnt">${all.length} tracks${YTLIKED?` · ${yt.length} extra from YouTube`:''}</span>`;
  root.appendChild(sh);
  const bar=el('div','toolbar'); bar.style.marginBottom='16px';
  bar.innerHTML=`<button class="cta ghost" id="ytlikebtn">${
      YTLOADING?'Loading YouTube likes…':YTLIKED?'↻ Refresh YouTube likes':'⟳ Load YouTube likes'}</button>
    <button class="cta ghost" id="dl-liked" title="Download every liked pick as audio">⬇ Download liked</button>`;
  root.appendChild(bar);
  const lg=el('pre'); lg.id='llog'; root.appendChild(lg);
  $('#ytlikebtn').onclick=loadYtLiked;
  $('#dl-liked').onclick=e=>{const ids=local.map(p=>p.video_id).filter(Boolean);
    if(ids.length)runDownload(ids,e.currentTarget);};
  if(!all.length){root.appendChild(el('div','gcard muted',
    'No liked songs yet — tap the ♡ on any track, or load your YouTube likes.'));return;}
  const grid=el('div','cards');
  all.forEach(p=>{const c=el('div','pcard');
    if(p.video_id)c.dataset.vid=p.video_id;
    c.innerHTML=`<div class="thumb" style="background-image:url('${thumb(p.video_id)}')">
      ${p.local?`<div class="sc">${esc(p.date||'')}</div>`:`<div class="sc">YouTube</div>`}
      ${p.video_id?`<button class="pchip" title="Play here">▶</button>`:''}
      ${p.video_id&&p.local?`<button class="dchip" title="Download this track">⬇</button>
      <button class="hchip lk" title="Tap: dislike · again: clear">♥</button>`:''}</div>
      <div class="meta"><div class="pt">${esc(p.title)}</div>
      <div class="pa">${esc(p.artist)}</div>${p.language?`<span class="pl">${esc(p.language)}</span>`:''}</div>`;
    const chip=c.querySelector('.pchip');
    if(chip)chip.onclick=()=>{PLAYLIST=all.filter(x=>x.video_id);
      p.video_id===CURVID?$('#p-play').onclick():playTrack(p.video_id);};
    const dc=c.querySelector('.dchip');
    if(dc)dc.onclick=()=>dlOne(p.video_id,dc);
    const hc=c.querySelector('.hchip');
    if(hc)hc.onclick=()=>cycleLike(p,hc,c);
    grid.appendChild(c);});
  root.appendChild(grid);
  if(CURVID){syncPlayerBtns();markPlaying();}
}

async function loadYtLiked(){if(YTLOADING)return; YTLOADING=true; renderLiked();
  try{const r=await(await fetch('/api/liked_youtube')).json();
    YTLIKED=r.tracks||[];
    YTLIKEDSET=new Set(YTLIKED.map(t=>t.video_id).filter(Boolean));
    if(r.error){const lg=$('#llog'); lg.style.display='block';
      lg.textContent='[YouTube] could not load likes: '+r.error;}
  }catch(e){YTLIKED=YTLIKED||[];}
  YTLOADING=false;
  // Full re-render so YouTube likes light up hearts in Today/History too, and
  // re-expand any open history day so its cards pick up the synced state.
  if(!STATE)return;
  render();
  OPENDAYS.forEach(date=>{const c=[...document.querySelectorAll('#history .gcard')]
    .find(x=>x.querySelector('.hhead')&&x.querySelector('.t')&&
      x.querySelector('.t').textContent.trim().startsWith(date));
    if(c){c.querySelector('.exp').textContent='▾';expandDay(date,c);}});
}

// Batch download picker: a checklist modal (liked pre-selected) that downloads
// the chosen tracks in one request. One batch avoids the single run-lock, so
// selections don't collide the way rapid per-track clicks did.
function openDownloadPicker(title,picks,srcBtn){
  picks=(picks||[]).filter(p=>p.video_id);
  if(!picks.length){alert('No downloadable tracks here.');return;}
  const ovl=el('div','ovl');
  const rows=picks.map((p,i)=>`<label class="mrow">
    <input type="checkbox" data-i="${i}" ${p.outcome==='liked'?'checked':''}>
    <div class="mth" style="background-image:url('${thumb(p.video_id)}')"></div>
    <div class="mmeta"><div class="mt">${esc(p.title)}</div>
      <div class="ma">${esc(p.artist)}${p.outcome&&p.outcome!=='pending'?' · '+p.outcome:''}</div></div>
    </label>`).join('');
  ovl.innerHTML=`<div class="modal">
    <div class="mh"><h3>${esc(title)}</h3><button class="mx" title="Close">✕</button></div>
    <div class="mtools">
      <span class="mchip" data-sel="all">Select all</span>
      <span class="mchip" data-sel="liked">Only liked ♥</span>
      <span class="mchip" data-sel="none">None</span></div>
    <div class="mlist">${rows}</div>
    <div class="mf"><span class="mcount"></span>
      <button class="cta mdl">⬇ Download</button></div></div>`;
  const boxes=[...ovl.querySelectorAll('.mrow input')];
  const upd=()=>{const n=boxes.filter(b=>b.checked).length;
    ovl.querySelector('.mcount').textContent=n+' of '+picks.length+' selected';
    const dl=ovl.querySelector('.mdl'); dl.disabled=!n;
    dl.textContent=n?`⬇ Download ${n} selected`:'⬇ Download';};
  boxes.forEach(b=>b.onchange=upd);
  ovl.querySelectorAll('.mchip').forEach(ch=>ch.onclick=()=>{const m=ch.dataset.sel;
    boxes.forEach(b=>{const p=picks[+b.dataset.i];
      b.checked=m==='all'?true:m==='none'?false:p.outcome==='liked';});upd();});
  const close=()=>ovl.remove();
  ovl.querySelector('.mx').onclick=close;
  ovl.onclick=e=>{if(e.target===ovl)close();};
  ovl.querySelector('.mdl').onclick=()=>{
    const ids=boxes.filter(b=>b.checked).map(b=>picks[+b.dataset.i].video_id);
    if(!ids.length)return; close();
    runDownload(ids,srcBtn||$('#dlbtn'));};
  document.body.appendChild(ovl); upd();
}

// Reusable track card (thumb + play/download/rate chips), shared by the
// history-day expansion. picks = the list to queue when this card is played.
function trackCard(p,picks){
  const c=el('div','pcard'+(p.outcome==='disliked'?' disliked':''));
  if(p.video_id)c.dataset.vid=p.video_id;
  c.innerHTML=`<div class="thumb" style="background-image:url('${thumb(p.video_id)}')">
    <div class="sc">${(p.score||0).toFixed(2)}</div>
    ${p.video_id?`<button class="pchip" title="Play here">▶</button>
    <button class="dchip" title="Download this track">⬇</button>
    <button class="hchip${p.outcome==='liked'?' lk':p.outcome==='disliked'?' dk':''}"
      title="Tap: like ♥ · again: dislike · again: clear">${
      p.outcome==='liked'||p.outcome==='disliked'?'♥':'♡'}</button>`:''}</div>
    <div class="meta"><div class="pt">${esc(p.title)}</div>
    <div class="pa">${esc(p.artist)}</div>${p.language?`<span class="pl">${esc(p.language)}</span>`:''}</div>`;
  const chip=c.querySelector('.pchip');
  if(chip)chip.onclick=()=>{PLAYLIST=picks.filter(x=>x.video_id);
    p.video_id===CURVID?$('#p-play').onclick():playTrack(p.video_id);};
  const dc=c.querySelector('.dchip');
  if(dc)dc.onclick=()=>dlOne(p.video_id,dc);
  const hc=c.querySelector('.hchip');
  if(hc)hc.onclick=()=>cycleLike(p,hc,c);
  return c;
}

async function expandDay(date,c){
  const wrap=c.querySelector('.daywrap'), caret=c.querySelector('.exp');
  caret.textContent='▾'; wrap.style.display='block';
  wrap.innerHTML='<div class="muted" style="padding:8px 2px">Loading tracks…</div>';
  let picks=[];
  try{picks=applyYtLikes(((await(await fetch('/api/day?date='+encodeURIComponent(date))).json()).picks)||[]);}
  catch(e){wrap.innerHTML='<div class="muted" style="padding:8px 2px">Could not load tracks.</div>';return;}
  wrap.innerHTML='';
  if(!picks.length){wrap.innerHTML='<div class="muted" style="padding:8px 2px">No tracks.</div>';return;}
  const grid=el('div','cards'); grid.style.marginTop='4px';
  picks.forEach(p=>grid.appendChild(trackCard(p,picks)));
  wrap.appendChild(grid);
  if(CURVID)markPlaying();
}

function collapseDay(c){c.querySelector('.daywrap').style.display='none';
  c.querySelector('.exp').textContent='▸';}

function toggleDay(date,c){
  if(OPENDAYS.has(date)){OPENDAYS.delete(date);collapseDay(c);}
  else{OPENDAYS.add(date);expandDay(date,c);}
}

function renderHistory(){const root=$('#history'); root.innerHTML='';
  const sh=el('div','sec-h'); sh.innerHTML=`<h3>History</h3><span class="cnt">${STATE.history.length} days</span>`;
  root.appendChild(sh);
  if(!STATE.history.length){root.appendChild(el('div','gcard muted','No history yet.'));return;}
  STATE.history.forEach(d=>{const c=el('div','gcard');
    const pl=d.playlist_id?`<a href="https://music.youtube.com/playlist?list=${d.playlist_id}" target="_blank">open playlist ↗</a>`:'';
    c.innerHTML=`<div class="row"><div class="hhead"><div class="t">${d.date} <span class="exp">▸</span></div>
    <div class="a">${d.count} picks · <span style="color:var(--good)">${d.liked}♥</span>
    · <span style="color:var(--bad)">${d.disliked}✕</span> · ${d.skipped} skipped · <span class="tapx">tap to view tracks</span></div></div>
    <div class="hact"><button class="cta ghost dl-day" title="Download this day's picks as audio files">⬇ Download</button>${pl}</div></div>
    <div class="daywrap"></div>`;
    c.querySelector('.hhead').onclick=()=>toggleDay(d.date,c);
    const b=c.querySelector('.dl-day');
    b.onclick=async(e)=>{e.stopPropagation(); b.disabled=true; b.textContent='…';
      let picks=[];
      try{picks=((await(await fetch('/api/day?date='+encodeURIComponent(d.date))).json()).picks)||[];}catch(_){}
      b.disabled=false; b.textContent='⬇ Download';
      openDownloadPicker('Download — '+d.date,picks,b);};
    root.appendChild(c);
    if(OPENDAYS.has(d.date))expandDay(d.date,c);});
}

const DLLABEL={queued:'Queued',downloading:'Downloading',tagging:'Finishing',
  done:'Done',have:'Already saved',failed:'Failed'};
function renderDownloads(){const root=$('#downloads'); if(!root)return; root.innerHTML='';
  const sh=el('div','sec-h');
  const items=DLS.items||[];
  const fin=items.filter(x=>['done','have','failed'].includes(x.status)).length;
  const failedIds=items.filter(x=>x.status==='failed'&&x.video_id).map(x=>x.video_id);
  sh.innerHTML=`<h3>Downloads <span class="spark">⬇</span></h3>
    <span class="cnt">${items.length?`${fin}/${items.length} · ${DLS.active?'running':'idle'}`:'idle'}</span>`;
  if(failedIds.length&&!DLS.active){const rb=el('button','dlhead-retry',
    `↻ Retry ${failedIds.length} failed`);
    rb.onclick=()=>runDownload(failedIds,rb); sh.appendChild(rb);}
  root.appendChild(sh);
  if(!items.length){root.appendChild(el('div','gcard muted',
    'No downloads yet. Use ⬇ Download on Today\'s Picks or any History day to start one.'));return;}
  const list=el('div','dllist');
  items.forEach(it=>{const row=el('div','dlrow');
    const pct=Math.max(0,Math.min(100,it.pct||0));
    const st=it.status||'queued';
    row.innerHTML=`<div class="dlth" style="background-image:url('${thumb(it.video_id)}')"></div>
      <div class="dlmeta"><div class="dlt">${esc(it.title||'')}</div>
        <div class="dla">${esc(it.artist||'')}</div>
        <div class="dlbar dl-${st}"><i style="width:${st==='downloading'?pct:(['done','have','tagging'].includes(st)?100:0)}%"></i></div></div>
      <div class="dlstat dl-${st}">${st==='downloading'?pct.toFixed(0)+'%':(DLLABEL[st]||st)}</div>`;
    if(st==='failed'&&it.video_id){const rb=el('button','dlretry','↻ Retry');
      rb.onclick=()=>runDownload([it.video_id],rb); row.appendChild(rb);}
    list.appendChild(row);});
  root.appendChild(list);
}

function renderDlPill(){const p=$('#dlpill'); if(!p)return;
  const running=(DLS.items||[]).filter(x=>x.status==='downloading'||x.status==='queued').length;
  if(DLS.active&&running){p.textContent=running; p.style.display='inline-block';}
  else p.style.display='none';}

async function pollDownloads(){
  try{DLS=await(await fetch('/api/downloads')).json();}catch(e){return;}
  renderDlPill();
  if($('#downloads').classList.contains('active'))renderDownloads();
  // When a run finishes, refresh state once so outcomes/history counts catch up.
  if(DLWAS&&!DLS.active){DLWAS=false; load();}
  if(DLS.active)DLWAS=true;
}

function renderProfiles(){const root=$('#profiles'); root.innerHTML='';
  const langs=[...STATE.languages].sort((a,b)=>b.artist_count-a.artist_count);
  const sh=el('div','sec-h');
  sh.innerHTML=`<h3>Disco Bowl — Your Taste</h3><span class="cnt">${langs.length} languages</span>`;
  root.appendChild(sh);
  langs.forEach(l=>{const c=el('div','gcard');
    const chips=l.top_artists.map(t=>`<span class="chip">${esc(t.artist)} · ${t.weight}</span>`).join('');
    c.innerHTML=`<div class="row"><div class="t">${esc(l.name)} ${l.enabled?'':'<span class="muted">(off)</span>'}</div>
    <div class="a">${l.artist_count} artists · ${l.markets.join(', ')}</div></div>
    <div class="pctl">${chips||'<span class="muted">no artists yet</span>'}</div>`;
    root.appendChild(c);});
}

async function loadStats(){
  if(STATSDATA===null){try{STATSDATA=await (await fetch('/api/stats')).json();}
    catch(e){STATSDATA={_error:String(e)};}}
  renderStats();
}
function renderStats(){const root=$('#stats'); if(!root)return;
  const s=STATSDATA; root.innerHTML='';
  const sh=el('div','sec-h');
  sh.innerHTML='<h3>Stats &amp; Insights</h3>'+(s&&!s._error
    ?`<span class="cnt">${s.active_days} active day${s.active_days===1?'':'s'}</span>`:'');
  root.appendChild(sh);
  if(!s){root.appendChild(el('p','muted')).textContent='Loading…';return;}
  if(s._error){root.appendChild(el('p','muted')).textContent='Could not load stats: '+s._error;return;}
  const pct=x=>Math.round((x||0)*100)+'%';
  const metric=(label,val,sub)=>`<div class="gcard" style="flex:1;min-width:130px">
    <div style="font-size:26px;font-weight:700">${val}</div>
    <div class="a">${label}</div>${sub?`<div class="muted" style="font-size:12px">${sub}</div>`:''}</div>`;
  const grid=el('div'); grid.style.cssText='display:flex;flex-wrap:wrap;gap:12px;margin-bottom:14px';
  grid.innerHTML=[
    metric('Total picks',s.total_picks,`${s.unique_artists} unique artists`),
    metric('Liked',s.liked,`${pct(s.like_rate)} of graded`),
    metric('Skipped',s.skipped,`${pct(s.skip_rate)} of graded`),
    metric('Discovery rate',pct(s.discovery_rate),'new artists you liked'),
  ].join('');
  root.appendChild(grid);
  const list=(title,items,fmt)=>{const c=el('div','gcard');
    const chips=(items||[]).map(fmt).join('')||'<span class="muted">nothing yet</span>';
    c.innerHTML=`<div class="row"><div class="t">${title}</div></div>
      <div class="pctl">${chips}</div>`; return c;};
  root.appendChild(list('Top genres you like',s.top_genres,
    t=>`<span class="chip">${esc(t.name)} · ${t.count}</span>`));
  root.appendChild(list('Top artists you like',s.top_artists,
    t=>`<span class="chip">${esc(t.name)} · ${t.count}</span>`));
  root.appendChild(list('Top languages you like',s.top_languages,
    t=>`<span class="chip">${esc(t.name)} · ${t.count}</span>`));
}

function slider(key,label,min,max,step,val){return `<label class="ctl">
  <div class="row"><span>${label}</span><span id="v-${key}">${val}</span></div>
  <input type="range" id="c-${key}" min="${min}" max="${max}" step="${step}" value="${val}"></label>`;}

function renderControls(){const root=$('#controls'), cf=STATE.config; root.innerHTML='';
  const sh=el('div','sec-h'); sh.innerHTML=`<h3>Settings</h3>`; root.appendChild(sh);

  // ── Daily run: the launchd agent that builds Today's Picks each morning ──
  const sc=el('div','gcard');
  sc.innerHTML=`<div class="t" style="margin-bottom:4px">Daily run</div>
    <div class="muted" style="font-size:12.5px;margin-bottom:14px">
      Builds Today's Picks in the background. It runs through launchd, so a run
      missed while the laptop was asleep fires on the next wake instead of being
      skipped.</div>
    <label class="langtoggle" style="margin-bottom:14px">
      <input type="checkbox" id="sch-on">
      <div><div>Run every day</div><small id="sch-state">checking…</small></div></label>
    <label class="ctl"><div class="row"><span>Time</span></div>
      <input type="time" id="sch-time" class="getin" style="width:150px;margin:0"></label>
    <div class="row" style="gap:10px;margin-top:14px;justify-content:flex-start">
      <button class="cta sm" id="sch-save">Apply schedule</button>
      <button class="cta ghost sm" id="runnow">▶ Run daily scout now</button>
      <span id="sch-msg" class="muted"></span></div>`;
  root.appendChild(sc);
  const rl=el('pre'); rl.id='runlog'; root.appendChild(rl);
  fetch('/api/schedule').then(r=>r.json()).then(s=>{
    const on=$('#sch-on'), t=$('#sch-time'); if(!on||!t)return;
    on.checked=!!s.enabled;
    t.value=String(s.hour).padStart(2,'0')+':'+String(s.minute).padStart(2,'0');
    $('#sch-state').textContent=s.enabled
      ?`on — next run ${t.value}`:'off — nothing is scheduled';
  }).catch(()=>{const e=$('#sch-state'); if(e)e.textContent='could not read schedule';});
  $('#sch-save').onclick=applySchedule;
  $('#runnow').onclick=runDailyNow;

  const c=el('div','gcard');
  c.innerHTML=`<div class="t" style="margin-bottom:6px">Scoring & selection</div>
  ${slider('min_score','Min score (pickiness)',0,0.9,0.01,cf.min_score)}
  ${slider('adventurousness','Adventurousness',0,1,0.05,cf.adventurousness)}
  ${slider('picks_per_day','Picks per day',5,60,1,cf.picks_per_day)}
  ${slider('release_window_days','Release window (days)',1,21,1,cf.release_window_days)}
  ${slider('max_per_artist','Max per artist',1,5,1,cf.max_per_artist)}
  ${slider('feedback_window_days','Feedback window (days)',1,14,1,cf.feedback_window_days)}
  ${slider('discovery_ratio','Discovery ratio (unknown artists)',0,1,0.05,cf.discovery_ratio)}
  ${slider('taste_decay','Taste decay (how fast old likes fade)',0,0.1,0.005,cf.taste_decay)}
  <label class="ctl"><div class="row"><span>Playlist privacy</span></div>
  <select id="c-playlist_privacy">
  ${['PRIVATE','UNLISTED','PUBLIC'].map(o=>`<option ${o===cf.playlist_privacy?'selected':''}>${o}</option>`).join('')}
  </select></label>
  <label class="ctl"><div class="row"><span>Playlist name prefix</span></div>
  <input class="getin" style="margin:0" id="c-playlist_name_prefix"
    value="${esc(cf.playlist_name_prefix||'Fresh')}"></label>`;
  root.appendChild(c);
  ['min_score','adventurousness','picks_per_day','release_window_days','max_per_artist',
   'feedback_window_days','discovery_ratio','taste_decay']
   .forEach(k=>{const i=$('#c-'+k); i.oninput=()=>$('#v-'+k).textContent=i.value;});

  const lc=el('div','gcard');
  lc.innerHTML=`<div class="t" style="margin-bottom:8px">Languages</div><div class="langgrid" id="langgrid"></div>`;
  root.appendChild(lc);
  const grid=lc.querySelector('#langgrid');
  STATE.languages.forEach(l=>{const d=el('label','langtoggle');
    d.innerHTML=`<input type="checkbox" data-lang="${esc(l.name)}" ${l.enabled?'checked':''}>
    <div><div>${esc(l.name)}</div><small>${l.artist_count} artists</small></div>`;
    grid.appendChild(d);});

  const save=el('div','gcard');
  save.innerHTML=`<div class="row"><button class="cta" id="savebtn">Save settings</button>
  <span id="savemsg" class="muted"></span></div>`;
  root.appendChild(save);
  $('#savebtn').onclick=saveConfig;
}

async function applySchedule(){
  const btn=$('#sch-save'), msg=$('#sch-msg');
  const on=$('#sch-on').checked, [h,m]=($('#sch-time').value||'07:00').split(':');
  btn.disabled=true; msg.textContent='applying…';
  try{const r=await fetch('/api/schedule',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:on,hour:parseInt(h),minute:parseInt(m)})});
    const s=await r.json();
    msg.textContent=s.message||'';
    $('#sch-state').textContent=s.enabled
      ?`on — next run ${String(s.hour).padStart(2,'0')}:${String(s.minute).padStart(2,'0')}`
      :'off — nothing is scheduled';
  }catch(e){msg.textContent='could not apply: '+e;}
  btn.disabled=false;
  setTimeout(()=>{if(msg)msg.textContent='';},4000);
}

async function runDailyNow(){
  const btn=$('#runnow'), log=$('#runlog');
  btn.disabled=true; btn.textContent='scouting…';
  log.style.display='block'; log.textContent='';
  try{const res=await fetch('/api/run',{method:'POST'});
    const reader=res.body.getReader(), dec=new TextDecoder();
    for(;;){const{done,value}=await reader.read(); if(done)break;
      log.textContent+=dec.decode(value,{stream:true}); log.scrollTop=log.scrollHeight;}
  }catch(e){log.textContent+='\n[error] '+e;}
  btn.disabled=false; btn.textContent='▶ Run daily scout now'; await load();
}

async function saveConfig(){
  const num=id=>parseFloat($('#c-'+id).value);
  const payload={min_score:num('min_score'),adventurousness:num('adventurousness'),
    picks_per_day:parseInt($('#c-picks_per_day').value),
    release_window_days:parseInt($('#c-release_window_days').value),
    max_per_artist:parseInt($('#c-max_per_artist').value),
    feedback_window_days:parseInt($('#c-feedback_window_days').value),
    discovery_ratio:num('discovery_ratio'),taste_decay:num('taste_decay'),
    playlist_privacy:$('#c-playlist_privacy').value,
    playlist_name_prefix:$('#c-playlist_name_prefix').value.trim()||'Fresh',
    disabled_languages:[...document.querySelectorAll('[data-lang]')]
      .filter(x=>!x.checked).map(x=>x.dataset.lang)};
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)});
  $('#savemsg').textContent='saved ✓'; await load();
  document.querySelector('.side nav button[data-tab=controls]').click();
  setTimeout(()=>{const m=$('#savemsg'); if(m)m.textContent='';},2500);
}


// ── disco ball: canvas mirror-ball render, photoreal-style ──
// Each tile is a mirror reflecting a "room": its brightness is a pseudo-random
// function of where it points, re-sampled in discrete steps as the ball turns,
// so facets flip bright/dark like the real thing. Hot tiles get star flares.
let _DRAF=null;
function startDisco(){
  const cv=document.getElementById('discoball'); if(!cv)return;
  if(_DRAF)cancelAnimationFrame(_DRAF);
  const ctx=cv.getContext('2d'), W=cv.width, H=cv.height;
  const cx=W/2, cy=H/2, R=W*0.465;
  const rows=22, tiles=[];
  for(let i=0;i<rows;i++){
    const th=(i+0.5)/rows*Math.PI, n=Math.max(8,Math.round(Math.sin(th)*40));
    for(let j=0;j<n;j++)
      tiles.push({th,ph:j/n*2*Math.PI+(i%2)*0.09,tw:Math.PI/rows,
                  r0:Math.abs(Math.sin(i*12.9898+j*78.233))});
  }
  const hue=v=>{ // red mirror palette: near-black maroon → crimson → pink-white
    const r=Math.min(255,14+v*365),
          g=Math.min(255,2+Math.pow(v,2.4)*300),
          b=Math.min(255,5+Math.pow(v,2.0)*320);
    return `rgb(${r|0},${g|0},${b|0})`;};
  let last=0;
  function frame(now){
    _DRAF=requestAnimationFrame(frame);
    // 30fps cap + skip entirely when the tab/section isn't visible — keeps
    // the 2012 GPU free for scrolling and the audio player.
    if(now-last<33||document.hidden||!cv.offsetParent)return;
    last=now;
    const rot=now*0.00028;
    const rq=rot; // smooth continuous turn
    ctx.clearRect(0,0,W,H);
    ctx.beginPath();ctx.arc(cx,cy,R,0,7);ctx.fillStyle='#120309';ctx.fill();
    const hot=[];
    for(const t of tiles){
      const ph=t.ph+rot;
      const sx=Math.sin(t.th)*Math.sin(ph), sy=Math.cos(t.th), sz=Math.sin(t.th)*Math.cos(ph);
      if(sz<=0.03)continue;
      const px=cx+sx*R, py=cy-sy*R;
      // "environment" the mirror reflects, sampled at the stepped angle
      const phq=t.ph+rq;
      let v=0.18+0.30*t.r0
        +0.26*Math.sin(phq*2.1+t.th*3.4+1.3)
        +0.22*Math.sin(phq*5.3+t.th*1.2)
        +0.14*Math.sin(phq*9.7+t.r0*6.28);
      // broad key light from upper left
      v+=0.30*Math.max(0,sx*-0.45+sy*0.5+sz*0.74);
      v=Math.max(0.02,Math.min(1,v));
      const s=t.tw*R*0.96, w=s*Math.max(0.2,Math.pow(sz,0.75));
      const ang=Math.atan2(px-cx,cy-py);
      ctx.save();ctx.translate(px,py);ctx.rotate(ang);
      ctx.fillStyle=hue(v);
      ctx.fillRect(-w/2*0.88,-s/2*0.88,w*0.88,s*0.88);
      ctx.restore();
      if(v>0.90&&hot.length<9)hot.push([px,py,v]);
    }
    // star flares on the hottest facets
    ctx.save();ctx.globalCompositeOperation='lighter';
    for(const [px,py,v] of hot){
      const L=(10+v*26)*(0.75+0.25*Math.sin(now*0.006+px));
      const g=ctx.createRadialGradient(px,py,0,px,py,L*0.5);
      g.addColorStop(0,'rgba(255,225,232,.9)');g.addColorStop(1,'rgba(255,60,90,0)');
      ctx.fillStyle=g;ctx.beginPath();ctx.arc(px,py,L*0.5,0,7);ctx.fill();
      ctx.strokeStyle='rgba(255,210,220,.75)';ctx.lineWidth=1;
      ctx.beginPath();
      ctx.moveTo(px-L,py);ctx.lineTo(px+L,py);
      ctx.moveTo(px,py-L);ctx.lineTo(px,py+L);
      ctx.moveTo(px-L*0.4,py-L*0.4);ctx.lineTo(px+L*0.4,py+L*0.4);
      ctx.moveTo(px-L*0.4,py+L*0.4);ctx.lineTo(px+L*0.4,py-L*0.4);
      ctx.stroke();
    }
    ctx.restore();
    // rim + sheen
    ctx.beginPath();ctx.arc(cx,cy,R,0,7);
    ctx.strokeStyle='rgba(255,110,132,.30)';ctx.lineWidth=1.4;ctx.stroke();
    const sheen=ctx.createRadialGradient(cx-R*0.42,cy-R*0.5,R*0.05,cx-R*0.42,cy-R*0.5,R*0.95);
    sheen.addColorStop(0,'rgba(255,222,230,.14)');sheen.addColorStop(1,'rgba(255,222,230,0)');
    ctx.beginPath();ctx.arc(cx,cy,R,0,7);ctx.fillStyle=sheen;ctx.fill();
  }
  _DRAF=requestAnimationFrame(frame);
}

// ── in-page player (audio only: the iframe is hidden off-screen) ──
let PAUSED=false, DUR=0, CUR=0, SHUFFLE=false, REPEAT='off',
    SEEKING=false;
// Both players report the clock about four times a second, which is coarse
// enough that a lyric line lands visibly late. CURAT records when the last
// report arrived so the gaps in between can be filled in by the wall clock.
let CURAT=0;
function setCur(t){CUR=t; CURAT=performance.now();}
function lyrNow(){
  return PAUSED||!CURAT ? CUR : CUR+(performance.now()-CURAT)/1000;
}
// ORDER is the play order, decided once: the playlist as-is, or one shuffle of
// the whole thing. ⏭/⏮ walk it, so nothing repeats before everything has played
// and going back returns to the track that actually played before.
let ORDER=[];
function buildOrder(){
  const ids=PLAYLIST.map(x=>x.video_id).filter(Boolean);
  if(SHUFFLE){
    for(let k=ids.length-1;k>0;k--){
      const r=Math.floor(Math.random()*(k+1)); const t=ids[k]; ids[k]=ids[r]; ids[r]=t;
    }
    const i=ids.indexOf(CURVID);
    if(i>0){ids.splice(i,1); ids.unshift(CURVID);}
  }
  ORDER=ids;
}
// The playlist swaps under the player (a tab, a library list, a new day), so the
// order is rebuilt only when the set of tracks actually changed.
function syncOrder(){
  const ids=PLAYLIST.map(x=>x.video_id).filter(Boolean);
  if(ORDER.length!==ids.length||!ORDER.every(v=>ids.includes(v)))buildOrder();
}
const fmt=s=>{s=Math.max(0,Math.floor(s||0));
  return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');};

function ytCmd(func,args){
  const f=$('#pframe'); if(!f.src)return;
  f.contentWindow.postMessage(JSON.stringify({event:'command',func,args:args||[]}),'*');
}
// Artwork goes to the sidebar card, the full-screen cover and the blurred
// backdrop behind it, all from the one thumbnail.
function paintArt(vid){
  const src=vid?thumb(vid):'';
  // Only YouTube thumbs are letterboxed; square art must not be zoom-cropped.
  const sq=!vid||isLocal(vid);
  ['#p-art','#nf-art'].forEach(k=>{$(k).src=src; $(k).classList.toggle('sq',sq);});
  const bg=src?`url("${src}")`:'none';
  ['#nf-bg','#nf-bg2','#nf-bg3'].forEach(k=>$(k).style.backgroundImage=bg);
}
function playTrack(vid){
  const p=PLAYLIST.find(x=>x.video_id===vid); if(!p)return;
  const loc=isLocal(vid);
  CURVID=vid; PAUSED=false; DUR=loc?(p.dur||0):0; setCur(0);
  const a=$('#lplay');
  if(loc){
    $('#pframe').src='';
    a.src='/api/local/audio?id='+encodeURIComponent(localId(vid));
    a.play().catch(()=>{});
  }else{
    a.pause(); a.removeAttribute('src'); a.load();
    $('#pframe').src=`https://www.youtube.com/embed/${vid}?autoplay=1&enablejsapi=1`;
  }
  paintArt(vid);
  $('#p-t').textContent=p.title; $('#p-a').textContent=p.artist;
  $('#p-yt').href=p.url||'#'; $('#p-play').textContent='⏸';
  // Ratings and the YT link only mean anything for picks that came from YouTube;
  // playlists are the other way round — they only hold files on this Mac.
  ['#p-yt','#p-like','#p-dis'].forEach(s=>$(s).style.display=loc?'none':'');
  $('#p-pl').style.display=loc?'':'none';
  $('#p-fill').style.width='0%'; $('#p-cur').textContent='0:00';
  $('#p-dur').textContent=fmt(DUR);
  syncOrder();
  $('#p-q').textContent=(ORDER.indexOf(vid)+1)+' / '+ORDER.length;
  $('#np-host').style.display=''; $('#nowplay').classList.remove('idle');
  LYR=null; LYRI=-1; LYRVID=null; paintLyrics();
  if(loc){if(DUR)requestLyrics();}
  // Subscribe to the embed's status stream so we get its time and state.
  else [600,1500,3000].forEach(t=>setTimeout(()=>{
    const f=$('#pframe'); if(!f.src)return;
    f.contentWindow.postMessage(JSON.stringify({event:'listening',id:'mxp',channel:'widget'}),'*');
  },t));
  mediaMeta(p);
  syncPlayerBtns(); renderNowPlaying(); markPlaying(); markLibPlaying(); renderQueue();
}
function syncPlayerBtns(){
  const p=PLAYLIST.find(x=>x.video_id===CURVID)||{};
  $('#p-like').classList.toggle('on',p.outcome==='liked');
  $('#p-dis').classList.toggle('on',p.outcome==='disliked');
}
function markPlaying(){
  document.querySelectorAll('.pcard').forEach(c=>{
    const on=c.dataset.vid&&c.dataset.vid===CURVID;
    c.classList.toggle('playing',on);
    const ch=c.querySelector('.pchip');
    if(ch)ch.textContent=on&&!PAUSED?'❚❚':'▶';
  });
}
function step(d){
  syncOrder();
  if(!ORDER.length)return;
  let j=ORDER.indexOf(CURVID)+d;
  if(REPEAT==='all')j=(j+ORDER.length)%ORDER.length;
  const n=ORDER[j]; if(n)playTrack(n);
}
function setPaused(v){
  // Restart the interpolation from now, or resuming would credit the song with
  // every second it spent paused.
  if(PAUSED&&!v)setCur(CUR);
  PAUSED=v; $('#p-play').textContent=v?'▶':'⏸';
  if(navigator.mediaSession)navigator.mediaSession.playbackState=v?'paused':'playing';
  renderNowPlaying(); markPlaying(); markLibPlaying(); renderQueue();
}
function paintTime(){
  $('#p-cur').textContent=fmt(CUR); $('#p-dur').textContent=fmt(DUR);
  $('#p-fill').style.width=(DUR?Math.min(100,CUR/DUR*100):0)+'%';
  syncLyrics();
}
// Local files play through a plain <audio>; the same handlers drive the same UI.
const AUD=$('#lplay');
AUD.addEventListener('timeupdate',()=>{if(!isLocal(CURVID)||SEEKING)return;
  setCur(AUD.currentTime); paintTime();});
AUD.addEventListener('durationchange',()=>{if(!isLocal(CURVID))return;
  if(isFinite(AUD.duration)&&AUD.duration>0)DUR=AUD.duration;
  paintTime(); if(LYRVID!==CURVID)requestLyrics();});
AUD.addEventListener('play',()=>{if(isLocal(CURVID))setPaused(false);});
AUD.addEventListener('pause',()=>{if(isLocal(CURVID)&&!AUD.ended)setPaused(true);});
AUD.addEventListener('ended',()=>{if(!isLocal(CURVID))return;
  if(REPEAT==='one'){AUD.currentTime=0;AUD.play().catch(()=>{});}else step(1);});
AUD.addEventListener('error',()=>{if(!isLocal(CURVID))return;
  const p=PLAYLIST.find(x=>x.video_id===CURVID)||{};
  $('#p-a').textContent=(p.ext&&['.ogg','.opus','.wma'].includes(p.ext))
    ?'Safari can’t play '+p.ext+' — try Chrome' : 'Could not play this file';
  setPaused(true);});
// Status stream from the hidden YouTube iframe: time, duration, state.
window.addEventListener('message',e=>{
  if(typeof e.data!=='string'||!/^https:\/\/www\.youtube/.test(e.origin))return;
  if(isLocal(CURVID))return;
  let d; try{d=JSON.parse(e.data);}catch(_){return;}
  const i=d&&d.info; if(!i)return;
  // Wait for the duration before asking LRCLIB: it separates the single from
  // the extended mix when several entries share a title.
  if(i.duration){DUR=i.duration; if(LYRVID!==CURVID)requestLyrics();}
  if(i.currentTime!=null&&!SEEKING){setCur(i.currentTime); paintTime();}
  if(i.playerState!=null){
    if(i.playerState===0){ // ended
      if(REPEAT==='one'){ytCmd('seekTo',[0,true]);ytCmd('playVideo');}
      else step(1);
    }else if(i.playerState===1)setPaused(false);
    else if(i.playerState===2)setPaused(true);
  }
});
function seekTo(t){
  setCur(Math.max(0,Math.min(t,DUR||t)));
  if(isLocal(CURVID)){AUD.currentTime=CUR;}else ytCmd('seekTo',[CUR,true]);
  paintTime();
}
// seek bar (click + drag)
const _bar=$('#p-bar');
function _ratio(e){const b=_bar.getBoundingClientRect();
  return Math.min(1,Math.max(0,(e.clientX-b.left)/b.width));}
_bar.addEventListener('pointerdown',e=>{if(!CURVID||!DUR)return;
  SEEKING=true; _bar.setPointerCapture(e.pointerId);
  $('#p-fill').style.width=_ratio(e)*100+'%'; $('#p-cur').textContent=fmt(_ratio(e)*DUR);});
_bar.addEventListener('pointermove',e=>{if(!SEEKING)return;
  $('#p-fill').style.width=_ratio(e)*100+'%'; $('#p-cur').textContent=fmt(_ratio(e)*DUR);});
_bar.addEventListener('pointerup',e=>{if(!SEEKING)return; SEEKING=false;
  setCur(_ratio(e)*DUR);
  if(isLocal(CURVID)){AUD.currentTime=CUR; AUD.play().catch(()=>{});}
  else{ytCmd('seekTo',[CUR,true]); ytCmd('playVideo');}
  setPaused(false);});
// Mac media keys: F8 already reaches the <audio> element on its own, but F7/F9
// and the Touch Bar only fire if we claim the handlers ourselves. Registering
// metadata too puts the track in Control Centre and the lock screen.
function mediaKeys(){
  const ms=navigator.mediaSession; if(!ms)return;
  const bind=(k,fn)=>{try{ms.setActionHandler(k,fn);}catch(e){}};
  bind('play',()=>{if(PAUSED)$('#p-play').onclick();});
  bind('pause',()=>{if(!PAUSED)$('#p-play').onclick();});
  bind('nexttrack',()=>step(1));
  bind('previoustrack',()=>step(-1));
  bind('seekforward',()=>seekTo(CUR+10));
  bind('seekbackward',()=>seekTo(Math.max(0,CUR-10)));
  bind('seekto',d=>{if(d&&d.seekTime!=null)seekTo(d.seekTime);});
}
mediaKeys();
function mediaMeta(p){
  const ms=navigator.mediaSession; if(!ms||!window.MediaMetadata)return;
  try{
    ms.metadata=new MediaMetadata({title:p.title||'',artist:p.artist||'',
      album:p.album||'',artwork:[{src:thumb(CURVID),sizes:'512x512'}]});
  }catch(e){}
}
// transport
$('#p-prev').onclick=()=>step(-1);
$('#p-next').onclick=()=>step(1);
$('#p-play').onclick=()=>{
  if(!CURVID){const p=suggestion(); if(!p)return;
    PLAYLIST=((STATE&&STATE.today.picks)||[]).filter(x=>x.video_id);
    playTrack(p.video_id); return;}
  if(isLocal(CURVID)){PAUSED?AUD.play().catch(()=>{}):AUD.pause();}
  else ytCmd(PAUSED?'playVideo':'pauseVideo');
  setPaused(!PAUSED);};
$('#p-shuf').onclick=()=>{SHUFFLE=!SHUFFLE;
  $('#p-shuf').classList.toggle('on',SHUFFLE);
  buildOrder(); $('#p-q').textContent=(ORDER.indexOf(CURVID)+1)+' / '+ORDER.length;
  renderQueue();};
$('#p-que').onclick=()=>{$('#queue').classList.toggle('on');
  $('#p-que').classList.toggle('on',$('#queue').classList.contains('on'));
  renderQueue();};
$('#q-x').onclick=()=>$('#p-que').onclick();
// The queue lists the order itself, so the shuffle is visible rather than felt.
function renderQueue(){
  if(!$('#queue').classList.contains('on'))return;
  syncOrder();
  const i=ORDER.indexOf(CURVID), list=$('#q-list');
  $('#q-sub').textContent=(SHUFFLE?'shuffled':'in order')+' · '
    +(i>=0?(i+1)+' of ':'')+ORDER.length;
  list.innerHTML='';
  ORDER.forEach((vid,k)=>{
    const t=PLAYLIST.find(x=>x.video_id===vid); if(!t)return;
    const r=el('div','qrow'+(k<i?' done':'')+(k===i?' now':''),
      `<div class="qn">${k===i?(PAUSED?'❚❚':'▶'):k+1}</div>
       <div class="qth" style="background-image:url('${thumb(vid)}')"></div>
       <div class="qm"><div class="qt">${esc(t.title)}</div>
         <div class="qa">${esc(t.artist)}</div></div>`);
    r.onclick=()=>{vid===CURVID?$('#p-play').onclick():playTrack(vid);};
    list.appendChild(r);
  });
  const now=list.querySelector('.qrow.now');
  if(now)list.scrollTop=now.offsetTop-list.clientHeight/2+now.offsetHeight/2;
}
$('#p-rep').onclick=()=>{REPEAT=REPEAT==='off'?'all':REPEAT==='all'?'one':'off';
  $('#p-rep').classList.toggle('on',REPEAT!=='off');
  $('#p-rep').innerHTML=REPEAT==='one'?'↻<span class="rep1">1</span>':'↻';};
$('#p-pl').onclick=async()=>{
  if(!isLocal(CURVID))return;
  // The Library tab may never have been opened, so the index might not be here yet.
  if(!LIB){LIB=await(await fetch('/api/library')).json(); ARTREV=LIB.artrev||0; libShape();}
  addToPlaylist([localId(CURVID)],$('#p-t').textContent);};
$('#p-close').onclick=()=>{$('#pframe').src='';
  AUD.pause(); AUD.removeAttribute('src'); AUD.load(); CURVID=null;
  setFull(false); renderNowPlaying(); markPlaying(); markLibPlaying();};
// Full screen: the sidebar card is *moved* into the overlay rather than mirrored,
// so there is one player and no second copy of its state to keep in step.
let FULL=false;
// Browser fullscreen on top of the overlay, so the tab strip and address bar go
// too. Must run inside the click/keypress that opened it or the browser refuses.
function browserFull(on){
  const d=document, e=d.documentElement;
  try{
    if(on){if(!d.fullscreenElement&&!d.webkitFullscreenElement)
      (e.requestFullscreen||e.webkitRequestFullscreen).call(e);}
    else if(d.fullscreenElement||d.webkitFullscreenElement)
      (d.exitFullscreen||d.webkitExitFullscreen).call(d);
  }catch(err){}
}
// Ask the server to hold caffeinate while full-screen lyrics are open, so the
// display doesn't switch off mid-song (macOS only keeps the display awake for
// real fullscreen *video*, not for a fullscreen web page like this).
function awake(on){
  try{fetch('/api/awake?on='+(on?1:0),{keepalive:true}).catch(()=>{});}catch(e){}
}
window.addEventListener('beforeunload',()=>awake(false));
function setFull(on){
  if(on&&!CURVID)return;
  FULL=on;
  awake(on);
  const np=$('#nowplay');
  (on?$('#nf-host'):$('#np-host')).appendChild(np);
  np.classList.toggle('full',on);
  $('#npfull').classList.toggle('on',on);
  browserFull(on);
  // Opening mid-song: the panel only exists now, so jump straight to the live
  // line rather than sliding the whole song past.
  if(on)requestAnimationFrame(()=>{const inn=$('#ll-in');
    if(inn)inn.style.transition='none';
    syncLyrics(true);
    if(inn)requestAnimationFrame(()=>inn.style.transition='');});
  lyrTicker(on);
}
// The players only report the clock four times a second, so waiting for them
// lands a line up to a quarter-second late — enough to read as a stutter. This
// checks the interpolated clock instead, capped at 30fps and only while the
// lyrics are on screen. Frames where the line hasn't changed cost an integer
// compare and nothing else: syncLyrics returns before it touches the DOM.
let _LRAF=0;
function lyrTicker(on){
  if(_LRAF){cancelAnimationFrame(_LRAF); _LRAF=0;}
  if(!on)return;
  let last=0;
  (function frame(now){
    _LRAF=requestAnimationFrame(frame);
    if(now-last<33||document.hidden)return;
    last=now;
    syncLyrics();
  })(0);
}
// Leaving fullscreen by another route (Esc, the green button, ⌃⌘F) should drop
// the overlay too, rather than leaving a window-sized player behind.
['fullscreenchange','webkitfullscreenchange'].forEach(ev=>
  document.addEventListener(ev,()=>{
    if(FULL&&!document.fullscreenElement&&!document.webkitFullscreenElement)setFull(false);
  }));
$('#p-exp').onclick=e=>{e.stopPropagation(); setFull(true);};
$('#np-art').ondblclick=()=>setFull(true);
$('#nf-x').onclick=()=>setFull(false);
wireStudio();
// The pointer gets out of the way while the lyrics run.
let _cursT=null;
document.addEventListener('mousemove',()=>{
  if(!FULL)return;
  $('#npfull').classList.remove('hidecur');
  clearTimeout(_cursT);
  // Not while the studio is open: every control in there is aimed at.
  _cursT=setTimeout(()=>{if(FULL&&!LST.open)$('#npfull').classList.add('hidecur');},2600);
});

// ── synced lyrics (LRCLIB) ──
// LYRI is the line currently lit. Holding it means each status tick is a couple
// of comparisons, and the DOM is only touched when the line actually changes.
let LYR=null, LYRI=-1, LYRVID=null;
function paintLyrics(){
  const box=$('#nf-lyr');
  if(!LYR){box.innerHTML='<div class="nf-msg">Looking for lyrics…</div>';return;}
  if(!LYR.synced.length){
    // Nobody timed this one, so there is no line to follow — show the whole
    // song as one scrollable block instead, readable start to finish.
    // Reading the audio costs twenty minutes of CPU, so it is never started on
    // our own initiative — the offer is a button and nothing happens until it's
    // pressed. `busy` means one is already under way for this track.
    const busy=LYR.state==='queued'||LYR.state==='running';
    const act='<div class="lp-act">'
      +'<button class="lyr-go" id="lyr-std">Open the lyrics studio</button>'
      +((LYR.can_transcribe&&!busy)
        ?`<button class="lyr-go" id="lyr-tx">${LYR.plain
            ?'Time these against the audio':'Transcribe from the audio'}</button>`
          +'<span class="lyr-hint">Takes ~20 min. Only happens once.</span>':'')
      +'</div>';
    if(LYR.plain){
      // Untimed words are still worth reading, and they may be on their way to
      // being timed — say so rather than leaving the block looking finished.
      box.innerHTML='<div class="ll-plain">'
        +(busy?'<div class="lp lp-note">Listening to the track to time these…</div>':'')
        +LYR.plain.split('\n').map(l=>l.trim()
          ?`<div class="lp">${esc(l)}</div>`:'<div class="lp lp-gap"></div>').join('')
        +act+'</div>';
    }else{
      box.innerHTML='<div class="nf-msg"><div>'+(busy
        ?'No lyrics anywhere for this one — transcribing it from the audio.<br>'
         +'<small>This takes a while. It only happens once.</small>'
        :LYR.state==='failed'?'Could not read any words off this track.'
        :'No lyrics found for this one.')+act+'</div></div>';
    }
    const go=$('#lyr-tx'); if(go)go.onclick=startTranscribe;
    const st=$('#lyr-std'); if(st)st.onclick=()=>lstOpen();
    return;}
  box.innerHTML='<div class="ll-in" id="ll-in">'
    +LYR.synced.map(l=>`<div class="ll">${esc(l.line)||'♪'}</div>`).join('')+'</div>';
  LYRI=-1; syncLyrics(true);
}
async function requestLyrics(){
  const p=PLAYLIST.find(x=>x.video_id===CURVID); if(!p)return;
  const vid=p.video_id; LYRVID=vid; LYR=null; LYRI=-1; lstTrackChanged(); paintLyrics();
  try{
    // Local files cache under artist+title, not the id: the id is a hash of the
    // path, so renaming or moving a file would throw its lyrics away.
    const r=await fetch('/api/lyrics?vid='+encodeURIComponent(isLocal(vid)?'':vid)
      +'&artist='+encodeURIComponent(p.artist)+'&title='+encodeURIComponent(p.title)
      +'&dur='+Math.round(DUR||0));
    const d=await r.json();
    if(LYRVID!==vid)return;      // the track changed while we were waiting
    LYR=d;
  }catch(e){LYR={synced:[],plain:'',source:''};}
  paintLyrics();
  if(LYR&&LYR.key&&(LYR.state==='queued'||LYR.state==='running'))pollLyrics(vid,LYR.key);
}
// A transcription outlives the track being on screen, so the poll checks it is
// still the one playing before painting, and gives up once it is not.
async function pollLyrics(vid,key){
  while(LYRVID===vid){
    await new Promise(r=>setTimeout(r,20000));
    if(LYRVID!==vid)return;
    try{
      const r=await fetch('/api/transcribe?key='+encodeURIComponent(key));
      const d=await r.json();
      if(LYRVID!==vid)return;
      if(d.synced&&d.synced.length){LYR=d; paintLyrics(); lstAdopt(); return;}
      if(d.state==='failed'||d.state==='unavailable'){
        LYR.state=d.state; paintLyrics(); lstTxBtn(); return;}
      LYR.state=d.state; paintLyrics(); lstTxBtn();
    }catch(e){return;}
  }
}
// ── lyrics studio ────────────────────────────────────────────────────────────
// Words and a clock are separate problems (see _timed_lyrics), and the old paste
// box could only ever hand over words. Everything that can go wrong with a lyric
// gets its tool here instead: text with no stamps, stamps that run half a second
// early, a sync that was made for a different cut of the song. All of it is done
// against the audio, because a timing you cannot hear is a timing you cannot
// judge — which is why the studio only ever opens on the track that is playing.
const LST={open:false,vid:null,track:null,key:'',lines:[],src:'',dirty:false,
           tab:'sync',sel:0,lit:-1,tap:false,off:0,cands:null,
           raf:null,sec:-1};

// mm:ss.hh both ways. The input accepts a bare number of seconds too, since
// that is what a stopwatch or a waveform editor gives you.
const lstFmt=t=>{if(t==null||!isFinite(t))return '--:--.--';
  t=Math.max(0,t); const m=Math.floor(t/60), s=t-m*60;
  return m+':'+(s<10?'0':'')+s.toFixed(2);};
const lstLrc=t=>{const m=Math.floor(t/60), s=t-m*60;
  return String(m).padStart(2,'0')+':'+(s<10?'0':'')+s.toFixed(2);};
function lstParseT(s){
  const m=(s||'').trim().match(/^(?:(\d{1,3}):)?(\d{1,3}(?:[.,]\d{1,3})?)$/);
  return m?(m[1]?parseInt(m[1],10)*60:0)+parseFloat(m[2].replace(',','.')):null;
}
// A stamp in front of the words, in any of the shapes people write them.
const LSTAMP=/^\s*[\[(]?\s*(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\s*[\])]?\s*/;
function lstFromText(text){
  return (text||'').replace(/\r/g,'').split('\n').map(raw=>{
    const m=raw.match(LSTAMP);
    if(!m)return {t:null,line:raw.trim()};
    return {t:parseInt(m[1],10)*60+parseInt(m[2],10)+(m[3]?parseFloat('0.'+m[3]):0),
            line:raw.slice(m[0].length).trim()};
  });
}
const lstToText=()=>LST.lines.map(l=>
  l.t==null?l.line:'['+lstLrc(l.t)+'] '+l.line).join('\n');
function lstLines(d){
  if(!d)return [];
  if(d.synced&&d.synced.length)return d.synced.map(l=>({t:l.t,line:l.line||''}));
  return (d.plain||'').split('\n').map(s=>({t:null,line:s.trim()}));
}

function lstOpen(seed){
  const p=PLAYLIST.find(x=>x.video_id===CURVID); if(!p)return;
  LST.open=true; LST.vid=p.video_id; LST.track=p; LST.cands=null; LST.sec=-1;
  document.body.classList.add('lst-on');
  $('#lst').hidden=false;
  $('#lst-title').textContent=p.title||'';
  $('#lst-artist').textContent=p.artist||'';
  $('#npfull').classList.remove('hidecur');
  lstTapMode(false);
  if(seed!==undefined)lstLoad(lstFromText(seed),'your lyrics',true);
  else lstLoad(lstLines(LYR),(LYR&&LYR.source)||'',false);
  LST.key=(LYR&&LYR.key)||'';
  $('#lst-revert').hidden=!(LYR&&LYR.has_prev);
  lstTab(seed!==undefined?'words':'sync');
  if(!LST.raf)LST.raf=requestAnimationFrame(lstTick);
}
function lstClose(force){
  if(!force&&LST.dirty
     &&!confirm('Close the studio? The unsaved changes go with it.'))return;
  LST.open=false; LST.tap=false;
  $('#lst').hidden=true;
  document.body.classList.remove('lst-on');
  if(LST.raf)cancelAnimationFrame(LST.raf);
  LST.raf=null;
}
// The player walking on to the next song leaves the studio editing something
// nobody is listening to. Unedited, that is just clutter; edited, throwing it
// away would be worse than saying so.
function lstTrackChanged(){
  if(!LST.open)return;
  if(!LST.dirty)return lstClose(true);
  lstNote('the player moved on — these edits are still for '
          +((LST.track&&LST.track.title)||'that track'),true);
}
// A poll or a transcription landing should reach the studio too, but never over
// the top of work in progress.
function lstAdopt(){
  if(!LST.open||LST.vid!==LYRVID||LST.dirty)return;
  LST.key=(LYR&&LYR.key)||LST.key;
  lstLoad(lstLines(LYR),(LYR&&LYR.source)||'',false);
  lstTxBtn();
}
function lstLoad(lines,src,dirty){
  LST.lines=lines; LST.src=src||''; LST.sel=0; LST.lit=-1; LST.off=0;
  $('#lst-offv').textContent='0.00s';
  lstPaintLines(); lstDirty(!!dirty);
}
function lstTab(name){
  LST.tab=name;
  document.querySelectorAll('.lst-tabs button').forEach(b=>
    b.classList.toggle('on',b.dataset.tab===name));
  $('#lst-pane-sync').hidden=name!=='sync';
  $('#lst-pane-words').hidden=name!=='words';
  $('#lst-pane-src').hidden=name!=='src';
  if(name==='words')$('#lst-ta').value=lstToText();
  if(name==='sync')lstFollow(true);
  if(name==='src'){lstTxBtn(); if(!LST.cands)lstSources();}
}
function lstDirty(v){
  LST.dirty=!!v;
  $('#lst-save').disabled=!LST.dirty;
  lstBadge(); lstNote();
}
function lstBadge(){
  const timed=LST.lines.filter(l=>l.t!=null&&l.line).length;
  $('#lst-badge').textContent=(LST.src||'nothing found yet')
    +' · '+(timed?timed+' timed':'untimed');
}
function lstNote(msg,warn){
  const n=$('#lst-note');
  if(msg!==undefined){n.textContent=msg; n.classList.toggle('warn',!!warn); return;}
  const words=LST.lines.filter(l=>l.line).length;
  const timed=LST.lines.filter(l=>l.t!=null&&l.line).length;
  n.classList.toggle('warn',timed>0&&timed<words);
  n.textContent=(!words?'nothing here yet'
    :timed===0?words+' lines, none timed'
    :timed<words?(words-timed)+' of '+words+' lines still need a time'
    :words+' lines timed')+(LST.dirty?' · unsaved':'');
}

// ── the line list: editor and preview in one ─────────────────────────────────
function lstPaintLines(){
  $('#lst-lines').innerHTML=LST.lines.map((l,i)=>
    '<div class="lrow'+(i===LST.sel?' sel':'')+'" data-i="'+i+'">'
    +'<input class="lt'+(l.t==null?' none':'')+'" value="'+lstFmt(l.t)+'">'
    +'<span class="lx'+(l.line?'':' blank')+'">'+(esc(l.line)||'♪')+'</span>'
    +'<button class="lb" data-act="now" title="Stamp it with the moment playing">now</button>'
    +'</div>').join('');
  LST.lit=-1; lstFollow(true);
}
// Only the stamps change under a shift or a nudge, so only the stamps are
// rewritten — rebuilding the list would drop the caret and the scroll position.
function lstPaintStamps(){
  const kids=$('#lst-lines').children;
  for(let i=0;i<kids.length&&i<LST.lines.length;i++){
    const inp=kids[i].firstChild, t=LST.lines[i].t;
    if(inp!==document.activeElement)inp.value=lstFmt(t);
    inp.classList.toggle('none',t==null);
  }
  lstBadge();
}
function lstSelect(i,seek){
  if(!LST.lines.length)return;
  i=Math.max(0,Math.min(i,LST.lines.length-1));
  const kids=$('#lst-lines').children;
  if(kids[LST.sel])kids[LST.sel].classList.remove('sel');
  LST.sel=i;
  if(kids[i]){kids[i].classList.add('sel'); if(LST.tap)lstCentre(i);}
  if(seek&&LST.lines[i].t!=null)seekTo(LST.lines[i].t);
}
function lstCentre(i){
  const box=$('#lst-lines'), r=box.children[i]; if(!r)return;
  box.scrollTop=Math.max(0,r.offsetTop-box.clientHeight/2+r.offsetHeight/2);
}
// Lit by the audio. One integer compare a frame unless the line actually
// changed, so following a song costs nothing between lines.
function lstFollow(force){
  if(!LST.open||LST.tab!=='sync')return;
  const t=lyrNow(), a=LST.lines;
  let i=-1;
  for(let k=0;k<a.length;k++){
    if(a[k].t==null)continue;
    if(a[k].t<=t+0.05)i=k; else break;
  }
  if(i===LST.lit&&!force)return;
  const kids=$('#lst-lines').children;
  if(kids[LST.lit])kids[LST.lit].classList.remove('on');
  LST.lit=i;
  if(i>=0&&kids[i]){
    kids[i].classList.add('on');
    // Scrolling under a pointer that is aiming at a button is infuriating, so
    // the list holds still while it is being used.
    if(!LST.hover&&!(document.activeElement
                     &&$('#lst-lines').contains(document.activeElement)))lstCentre(i);
  }
}
// 30fps is plenty for a lit line and a progress bar, and it is the ceiling the
// rest of this page works to on 2012 hardware.
function lstTick(){
  if(!LST.open){LST.raf=null;return;}
  const now=performance.now();
  if(now-(LST.last||0)>=33){
    LST.last=now; lstFollow();
    const t=lyrNow(), s=Math.floor(t);
    if(s!==LST.sec){
      LST.sec=s;
      $('#lst-cur').textContent=fmt(t); $('#lst-dur').textContent=fmt(DUR);
      $('#lst-fill').style.width=(DUR?Math.min(100,t/DUR*100):0)+'%';
      $('#lst-play').innerHTML=PAUSED?'&#9654;':'&#9208;';
    }
  }
  LST.raf=requestAnimationFrame(lstTick);
}

// ── timing tools ─────────────────────────────────────────────────────────────
function lstShift(d){
  let n=0;
  for(const l of LST.lines){
    if(l.t==null)continue;
    l.t=Math.max(0,Math.round((l.t+d)*100)/100); n++;
  }
  if(!n)return lstNote('no times to shift yet',true);
  LST.off=Math.round((LST.off+d)*100)/100;
  $('#lst-offv').textContent=(LST.off>0?'+':'')+LST.off.toFixed(2)+'s';
  lstPaintStamps(); lstDirty(true);
}
function lstSetNow(i){
  LST.lines[i].t=Math.max(0,Math.round(lyrNow()*100)/100);
  lstPaintStamps(); lstDirty(true);
}
function lstTapMode(on){
  LST.tap=on;
  $('#lst-tap').classList.toggle('on',on);
  $('#lst-tap').textContent=on?'Stop tapping':'Tap sync';
  $('#lst-taphint').hidden=!on;
  if(on){lstTab('sync'); lstCentre(LST.sel);}
}
function lstTap(){
  // Blank lines are spacing, not something anybody sings, so a tap never lands
  // on one and never leaves one holding a time.
  let i=LST.sel;
  while(i<LST.lines.length&&!LST.lines[i].line)i++;
  if(i>=LST.lines.length)return lstTapMode(false);
  lstSelect(i); lstSetNow(i);
  let j=i+1;
  while(j<LST.lines.length&&!LST.lines[j].line)j++;
  if(j>=LST.lines.length){lstTapMode(false); return lstNote('that was the last line');}
  lstSelect(j);
}

// ── words ────────────────────────────────────────────────────────────────────
function lstApply(){
  const lines=lstFromText($('#lst-ta').value);
  if(!lines.some(l=>l.line))return lstNote('nothing to use',true);
  LST.lines=lines; LST.sel=0;
  lstPaintLines(); lstDirty(true); lstTab('sync');
}

// ── sources ──────────────────────────────────────────────────────────────────
async function lstSources(){
  $('#lst-cands').innerHTML='<div class="lst-hint">Asking LRCLIB and NetEase…</div>';
  try{
    const p=LST.track;
    const r=await fetch('/api/lyrics/sources?artist='+encodeURIComponent(p.artist||'')
      +'&title='+encodeURIComponent(p.title||'')+'&dur='+Math.round(DUR||0)
      +'&key='+encodeURIComponent(LST.key));
    LST.cands=(await r.json()).items||[];
  }catch(e){LST.cands=[];}
  lstPaintCands();
}
function lstPaintCands(){
  const a=LST.cands||[];
  if(!a.length){
    $('#lst-cands').innerHTML='<div class="lst-hint">Nobody has synced this one. '
      +'Paste the words under Words, then tap them in — or read them off the audio.</div>';
    return;
  }
  $('#lst-cands').innerHTML=a.map((c,i)=>{
    const n=c.synced.length;
    const bits=[n?n+' timed lines':'untimed words'];
    if(c.dur)bits.push(fmt(c.dur));
    // A sync that stops at the second chorus is the commonest way a "found"
    // lyric is still wrong, so how far it reaches is on the label.
    if(n&&c.coverage)bits.push('reaches '+Math.round(c.coverage*100)+'%');
    const first=(c.synced[0]&&c.synced[0].line)||(c.plain||'').split('\n')[0]||'';
    return '<div class="cand"><span class="cand-w">'+esc(c.where)+'</span>'
      +'<div class="cand-m"><div class="cand-t">'+esc(c.title||'')+'</div>'
      +'<div class="cand-s">'+esc(c.artist||'')+' · '+esc(bits.join(' · '))+'</div>'
      +'<div class="cand-p">'+esc(first)+'</div></div>'
      +'<button class="lst-btn" data-cand="'+i+'">Load</button></div>';
  }).join('');
}
function lstUseCand(i){
  const c=(LST.cands||[])[i]; if(!c)return;
  lstLoad(c.synced.length
    ?c.synced.map(l=>({t:l.t,line:l.line||''}))
    :(c.plain||'').split('\n').map(s=>({t:null,line:s.trim()})),
    c.source+' ('+c.where+')',true);
  lstTab('sync');
  lstNote('loaded — play it through, then Save if it fits');
}
// Two different jobs behind one button. With whisper segments already on disk
// the words only need lining up again, which is instant; without them the clock
// has to be earned, and that costs twenty minutes of CPU nobody spends unasked.
function lstTxBtn(){
  if(!LST.open)return;
  const b=$('#lst-tx'), st=(LYR&&LYRVID===LST.vid)?LYR.state:'';
  const busy=st==='queued'||st==='running';
  b.hidden=!(LYR&&(LYR.can_transcribe||LYR.has_whisper))&&!busy;
  b.disabled=busy;
  b.textContent=busy?'Reading the audio…'
    :(LYR&&LYR.has_whisper)?'Re-time these against the audio'
    :'Read the times off the audio (~20 min)';
}
async function lstAutoTime(){
  const p=LST.track, b=$('#lst-tx');
  const text=LST.lines.map(l=>l.line).join('\n').trim();
  if(LYR&&LYR.has_whisper&&text){
    b.disabled=true; b.textContent='Lining them up…';
    try{
      const r=await fetch('/api/lyrics/paste',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({vid:isLocal(LST.vid)?'':LST.vid,artist:p.artist,
          title:p.title,dur:Math.round(DUR||0),key:LST.key,text:text})});
      const d=await r.json();
      if(d.error)lstNote(d.error,true);
      else{
        if(LYRVID===LST.vid){LYR=d; paintLyrics();}
        LST.key=d.key||LST.key;
        lstLoad(lstLines(d),d.source,false);
        $('#lst-revert').hidden=!d.has_prev;
        lstTab('sync'); lstNote('timed against the transcription');
      }
    }catch(e){lstNote('could not line those up',true);}
    lstTxBtn(); return;
  }
  await startTranscribe();
  lstTxBtn();
}
async function lstRevert(){
  if(!confirm('Put back the version from before the last save?'))return;
  try{
    const p=LST.track;
    const r=await fetch('/api/lyrics/revert',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key:LST.key,vid:isLocal(LST.vid)?'':LST.vid,
                           artist:p.artist,title:p.title})});
    const d=await r.json();
    if(d.error)return lstNote(d.error,true);
    if(LYRVID===LST.vid){LYR=d; paintLyrics();}
    lstLoad(lstLines(d),d.source,false);
    $('#lst-revert').hidden=!d.has_prev;
    lstTab('sync'); lstNote('put back');
  }catch(e){lstNote('could not undo that',true);}
}
async function lstSave(){
  if(!LST.open||!LST.track)return;
  const p=LST.track;
  const words=LST.lines.filter(l=>l.line);
  const timed=words.filter(l=>l.t!=null);
  // Half a timed song plays worse than none of it: the player would follow to
  // the last stamp and then sit still for the rest. So it is all or nothing.
  const whole=timed.length&&timed.length===words.length;
  const base=(LST.src||'').replace(/ · your edit$/,'');
  $('#lst-save').disabled=true;
  try{
    const r=await fetch('/api/lyrics/save',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({vid:isLocal(LST.vid)?'':LST.vid,artist:p.artist,
        title:p.title,dur:Math.round(DUR||0),key:LST.key,
        synced:whole?timed:[],
        plain:LST.lines.map(l=>l.line).join('\n'),
        source:(base?base+' · your edit':'your lyrics')})});
    const d=await r.json();
    if(d.error){lstNote(d.error,true); $('#lst-save').disabled=false; return;}
    LST.key=d.key||LST.key;
    if(LYRVID===LST.vid){LYR=d; paintLyrics();}
    lstLoad(lstLines(d),d.source,false);
    $('#lst-revert').hidden=!d.has_prev;
    const short=words.length-timed.length;
    lstNote(whole?('saved · '+timed.length+' lines timed')
      :('saved as plain text — '+short
        +(short===1?' line still needs':' lines still need')+' a time'),!whole);
  }catch(e){lstNote('could not save that',true); $('#lst-save').disabled=false;}
}
async function startTranscribe(){
  const p=PLAYLIST.find(x=>x.video_id===CURVID); if(!p)return;
  const vid=p.video_id, go=$('#lyr-tx');
  if(go){go.disabled=true; go.textContent='Starting…';}
  try{
    const r=await fetch('/api/transcribe/start',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({vid:isLocal(vid)?'':vid,artist:p.artist,title:p.title,
                           dur:Math.round(DUR||0)})});
    const d=await r.json();
    if(LYRVID!==vid||d.error)return;
    LYR.key=d.key; LYR.state=d.state; paintLyrics();
    if(d.state==='queued'||d.state==='running')pollLyrics(vid,d.key);
  }catch(e){if(go){go.disabled=false; go.textContent='Transcribe from the audio';}}
}
function wireStudio(){
  const wrap=$('#nf-lyrwrap'), ovl=$('#lst'), lines=$('#lst-lines');
  $('#lyr-add').onclick=()=>lstOpen();
  $('#lst-x').onclick=()=>lstClose();
  ovl.onclick=e=>{if(e.target===ovl)lstClose();};
  $('#lst-save').onclick=lstSave;
  $('#lst-apply').onclick=lstApply;
  $('#lst-revert').onclick=lstRevert;
  $('#lst-tx').onclick=lstAutoTime;
  $('#lst-tap').onclick=()=>lstTapMode(!LST.tap);
  document.querySelectorAll('.lst-tabs button').forEach(b=>
    b.onclick=()=>lstTab(b.dataset.tab));
  document.querySelectorAll('.lst-tools [data-off]').forEach(b=>
    b.onclick=()=>lstShift(parseFloat(b.dataset.off)));
  // One listener for a list that can be two hundred rows long.
  lines.addEventListener('click',e=>{
    const row=e.target.closest('.lrow'); if(!row)return;
    const i=+row.dataset.i, act=e.target.dataset.act;
    if(act==='now'){lstSelect(i); lstSetNow(i);}
    else lstSelect(i,e.target.classList.contains('lx'));
  });
  lines.addEventListener('change',e=>{
    if(!e.target.classList.contains('lt'))return;
    const i=+e.target.closest('.lrow').dataset.i;
    LST.lines[i].t=lstParseT(e.target.value);
    lstPaintStamps(); lstDirty(true);
  });
  lines.addEventListener('pointerenter',()=>{LST.hover=true;});
  lines.addEventListener('pointerleave',()=>{LST.hover=false;});
  $('#lst-cands').addEventListener('click',e=>{
    if(e.target.dataset.cand!==undefined)lstUseCand(+e.target.dataset.cand);
  });
  $('#lst-play').onclick=()=>$('#p-play').onclick();
  $('#lst-bar').addEventListener('pointerdown',e=>{
    if(!DUR)return;
    const b=e.currentTarget.getBoundingClientRect();
    seekTo(Math.min(1,Math.max(0,(e.clientX-b.left)/b.width))*DUR);
  });
  // A dropped file is words, wherever it lands: on the lyrics behind, it opens
  // the studio; on the studio, it fills the sheet.
  const readDrop=async e=>{
    const f=e.dataTransfer.files&&e.dataTransfer.files[0];
    return f?await f.text():e.dataTransfer.getData('text');
  };
  wrap.addEventListener('dragover',e=>{e.preventDefault(); wrap.classList.add('drop');});
  wrap.addEventListener('dragleave',e=>{
    if(!wrap.contains(e.relatedTarget))wrap.classList.remove('drop');});
  wrap.addEventListener('drop',async e=>{
    e.preventDefault(); wrap.classList.remove('drop');
    const text=await readDrop(e);
    if(text&&text.trim())lstOpen(text);
  });
  ovl.addEventListener('dragover',e=>{e.preventDefault(); ovl.classList.add('drop');});
  ovl.addEventListener('dragleave',e=>{
    if(!ovl.contains(e.relatedTarget))ovl.classList.remove('drop');});
  ovl.addEventListener('drop',async e=>{
    e.preventDefault(); ovl.classList.remove('drop');
    const text=await readDrop(e);
    if(!text||!text.trim())return;
    lstTab('words'); $('#lst-ta').value=text; lstApply();
  });
  // Captured, so the page's own shortcuts never fire underneath the studio —
  // Space would otherwise both stamp a line and toggle the player.
  document.addEventListener('keydown',e=>{
    if(!LST.open)return;
    const inField=/INPUT|TEXTAREA/.test(e.target.tagName);
    if(e.key==='Escape'){
      e.preventDefault(); e.stopPropagation();
      if(inField)e.target.blur();
      else if(LST.tap)lstTapMode(false);
      else lstClose();
      return;
    }
    if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='s'){
      e.preventDefault(); e.stopPropagation(); lstSave(); return;}
    if(inField||e.metaKey||e.ctrlKey||e.altKey)return;
    const tapping=LST.tap&&LST.tab==='sync';
    if(e.key===' '){
      e.preventDefault(); e.stopPropagation();
      if(tapping)lstTap(); else $('#p-play').onclick();
    }else if(e.key==='ArrowLeft'){
      e.preventDefault(); e.stopPropagation();
      if(tapping)lstSelect(LST.sel-1,true); else seekTo(Math.max(0,CUR-5));
    }else if(e.key==='ArrowRight'){
      e.preventDefault(); e.stopPropagation();
      if(tapping)lstSelect(LST.sel+1); else seekTo(CUR+5);
    }
  },true);
}
function syncLyrics(force){
  if(!LYR||!LYR.synced.length||!FULL)return;
  const arr=LYR.synced, t=lyrNow();
  let i=LYRI;
  while(i+1<arr.length&&arr[i+1].t<=t+0.15)i++;
  while(i>=0&&arr[i].t>t+0.15)i--;
  if(i===LYRI&&!force)return;
  const prev=LYRI; LYRI=i;
  const inn=$('#ll-in'), box=$('#nf-lyr'); if(!inn)return;
  const kids=inn.children;
  // Half a screen of padding top and bottom, so even the first and last lines
  // can sit in the middle instead of being pinned to an edge.
  const half=Math.round(box.clientHeight/2);
  if(inn._pad!==half){inn._pad=half; inn.style.padding=half+'px 0';}
  // Singing forward moves one line at a time, and restyling only the two lines
  // that changed keeps a long song from restyling every line each verse. A
  // seek can land anywhere, so that still repaints the lot.
  if(!force&&Math.abs(i-prev)===1&&prev>=0&&i>=0){
    kids[prev].classList.remove('on');
    kids[prev].classList.toggle('done',prev<i);
    kids[i].classList.add('on'); kids[i].classList.remove('done');
  }else{
    for(let k=0;k<kids.length;k++){
      kids[k].classList.toggle('on',k===i); kids[k].classList.toggle('done',k<i);}
  }
  const cur=i>=0?kids[i]:kids[0];
  const y=cur?Math.max(0,cur.offsetTop+cur.offsetHeight/2-box.clientHeight/2):0;
  inn.style.transform='translateY(-'+y+'px)';
}
$('#p-like').onclick=()=>{if(!CURVID)return;
  const p=PLAYLIST.find(x=>x.video_id===CURVID); if(!p)return;
  const ep=p.outcome==='liked'?'/api/clear':'/api/like';
  p.outcome=p.outcome==='liked'?'pending':'liked';
  syncPlayerBtns(); repaintVid(CURVID); postOutcome(CURVID,ep);};
$('#p-dis').onclick=()=>{if(!CURVID)return;
  const vid=CURVID, p=PLAYLIST.find(x=>x.video_id===vid);
  if(p&&p.outcome!=='disliked'){p.outcome='disliked'; syncPlayerBtns(); repaintVid(vid);}
  postOutcome(vid,'/api/dislike'); step(1);};
// keyboard shortcuts
document.addEventListener('keydown',e=>{
  if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)||e.metaKey||e.ctrlKey||e.altKey)return;
  if(e.key==='Escape'&&FULL){setFull(false);return;}
  if(!CURVID)return;
  const k=e.key.toLowerCase();
  if(k==='f'){setFull(!FULL);return;}
  if(e.code==='Space'){e.preventDefault();$('#p-play').onclick();}
  else if(e.key==='ArrowRight'){seekTo(CUR+5);}
  else if(e.key==='ArrowLeft'){seekTo(Math.max(0,CUR-5));}
  else if(k==='n')step(1); else if(k==='p')step(-1);
  else if(k==='s')$('#p-shuf').onclick();
  else if(k==='q')$('#p-que').onclick();
  else if(k==='r')$('#p-rep').onclick();
  else if(k==='l')$('#p-like').onclick();
  else if(k==='x')$('#p-dis').onclick();
});

// XP+ world tour. The run streams `::stop|genre`, `::found|genre|artist — title`
// and `::phase|name` markers; a globe turns to face where each genre comes from
// and leaves a pin and a great-circle route behind, so a run reads as a journey
// rather than a spinner. Orthographic projection, redrawn per frame from the
// same Natural Earth outlines the flat map used.
const SVGNS='http://www.w3.org/2000/svg';
const D2R=Math.PI/180;
const XPG={rings:null,grat:null,home:null,R:0,lon0:0,lat0:12,ctx:null,cx:0,cy:0,
  from:null,to:null,t0:0,dur:1700,stops:0,finds:0,lastLL:null,
  pins:[],arcs:[],pings:[],raf:0,prev:0,scanT:null,scanD:null,near:null};

// Undo the equirectangular projection world.svg was written in, so every outline
// becomes lat/lon and can be re-projected onto a sphere each frame. Trig for the
// points is precomputed once — per-frame it is only multiplies and adds.
function xpParseRings(d){
  const out=[], KEEP=1.2, MINEXT=2.6;  // degrees; ~9.6k points down to ~4k
  for(const chunk of d.split('M')){
    if(!chunk.trim())continue;
    const nums=chunk.replace(/Z/g,'').trim().split(/[\s,]+/).map(Number);
    const raw=nums.length>>1;
    if(raw<4)continue;
    const pts=[];
    let x0=180,x1=-180,y0=90,y1=-90;
    for(let i=0;i<raw;i++){
      const lon=(nums[i*2]/2000)*360-180, lat=84-(nums[i*2+1]/788.9)*142;
      if(lon<x0)x0=lon; if(lon>x1)x1=lon; if(lat<y0)y0=lat; if(lat>y1)y1=lat;
      // Thin the outline: at globe scale a degree is ~1.5px, so sub-degree
      // detail costs frames and shows nothing.
      const p=pts[pts.length-1];
      if(!p||Math.hypot(lon-p[0],lat-p[1])>KEEP)pts.push([lon,lat]);
    }
    if(pts.length<4)continue;
    if(x1-x0<MINEXT&&y1-y0<MINEXT)continue;  // specks: pure cost, no pixels
    const n=pts.length;
    const sla=new Float32Array(n),cla=new Float32Array(n),
          slo=new Float32Array(n),clo=new Float32Array(n);
    for(let i=0;i<n;i++){
      sla[i]=Math.sin(pts[i][1]*D2R); cla[i]=Math.cos(pts[i][1]*D2R);
      slo[i]=Math.sin(pts[i][0]*D2R); clo[i]=Math.cos(pts[i][0]*D2R);
    }
    out.push({n,sla,cla,slo,clo});
  }
  return out;
}
function xpGraticule(){
  const out=[];
  for(let lon=-180;lon<180;lon+=45){
    const pts=[]; for(let lat=-80;lat<=80;lat+=8)pts.push([lat,lon]);
    out.push(pts);
  }
  for(let lat=-60;lat<=60;lat+=30){
    const pts=[]; for(let lon=-180;lon<=180;lon+=8)pts.push([lat,lon]);
    out.push(pts);
  }
  return out.map(pts=>{
    const n=pts.length,sla=new Float32Array(n),cla=new Float32Array(n),
          slo=new Float32Array(n),clo=new Float32Array(n);
    pts.forEach(([lat,lon],i)=>{
      sla[i]=Math.sin(lat*D2R); cla[i]=Math.cos(lat*D2R);
      slo[i]=Math.sin(lon*D2R); clo[i]=Math.cos(lon*D2R);});
    return {n,sla,cla,slo,clo};
  });
}
async function xpMapLoad(){
  if(XPG.rings)return;
  const [txt,home]=await Promise.all([
    fetch('/assets/world.svg').then(r=>r.text()).catch(()=>''),
    fetch('/api/genre-home').then(r=>r.json()).catch(()=>({})),
  ]);
  XPG.rings=xpParseRings((txt.match(/\sd="([^"]+)"/)||['',''])[1]);
  XPG.grat=xpGraticule();
  XPG.home=home||{};
}

function xpGlobeBuild(){
  const stage=$('#xpfx-stage'); if(!stage)return;
  const R=XPG.R=Math.max(150,Math.min(window.innerWidth,window.innerHeight)*.33);
  const pad=Math.round(R*1.5), size=pad*2;
  const dpr=Math.min(1.5,window.devicePixelRatio||1);  // cap: fill rate is the limit
  stage.innerHTML='<canvas id="xpfx-cv" width="'+Math.round(size*dpr)
    +'" height="'+Math.round(size*dpr)+'" style="width:'+size+'px;height:'+size+'px"></canvas>';
  const cv=$('#xpfx-cv'); XPG.ctx=cv.getContext('2d');
  XPG.ctx.scale(dpr,dpr); XPG.cx=pad; XPG.cy=pad;
  const sky=$('#xpfx-stars');
  if(sky&&!sky.childElementCount){
    let s='';
    for(let i=0;i<110;i++)s+='<i style="left:'+(Math.random()*100).toFixed(2)
      +'%;top:'+(Math.random()*100).toFixed(2)+'%;opacity:'+(.2+Math.random()*.7).toFixed(2)
      +';animation-delay:'+(Math.random()*4).toFixed(2)+'s"></i>';
    sky.innerHTML=s;
  }
  XPG.pins=[]; XPG.arcs=[]; XPG.pings=[];
  XPG.lon0=-20; XPG.lat0=12; XPG.from=null; XPG.to=null;
  if(!XPG.raf){XPG.prev=0; XPG.raf=requestAnimationFrame(xpFrame);}
}

const xpEase=t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2;

function xpFrame(now){
  XPG.raf=requestAnimationFrame(xpFrame);
  if(now-XPG.prev<33||document.hidden)return;
  const dt=Math.min(.06,(now-XPG.prev)/1000); XPG.prev=now;
  if(XPG.to){
    const k=Math.min(1,(now-XPG.t0)/XPG.dur), e=xpEase(k);
    XPG.lon0=XPG.from[1]+(XPG.to[1]-XPG.from[1])*e;
    XPG.lat0=XPG.from[0]+(XPG.to[0]-XPG.from[0])*e;
    if(k>=1)XPG.to=null;
  }else{
    XPG.lon0+=4.5*dt;   // idle drift keeps the planet alive between stops
  }
  if(XPG.lon0>180)XPG.lon0-=360; if(XPG.lon0<-180)XPG.lon0+=360;
  xpDraw(now);
}

function xpTrace(ctx,rings,R,s0,c0,sl0,cl0,close){
  for(let k=0;k<rings.length;k++){
    const r=rings[k]; let open=false;
    for(let i=0;i<r.n;i++){
      const cla=r.cla[i],sla=r.sla[i],slo=r.slo[i],clo=r.clo[i];
      const dc=clo*cl0+slo*sl0;
      if(s0*sla+c0*cla*dc<=0){          // behind the horizon
        if(open&&close)ctx.closePath();
        open=false; continue;
      }
      const ds=slo*cl0-clo*sl0;
      const x=R*cla*ds, y=-R*(c0*sla-s0*cla*dc);
      if(open)ctx.lineTo(x,y); else ctx.moveTo(x,y);
      open=true;
    }
    if(open&&close)ctx.closePath();
  }
}

function xpDraw(now){
  const ctx=XPG.ctx; if(!ctx)return;
  const R=XPG.R, cx=XPG.cx, cy=XPG.cy;
  const s0=Math.sin(XPG.lat0*D2R),c0=Math.cos(XPG.lat0*D2R),
        sl0=Math.sin(XPG.lon0*D2R),cl0=Math.cos(XPG.lon0*D2R);
  ctx.clearRect(0,0,cx*2,cy*2);
  ctx.save(); ctx.translate(cx,cy);

  // ocean
  const sea=ctx.createRadialGradient(-R*.35,-R*.4,R*.05,0,0,R*1.05);
  sea.addColorStop(0,'#5a0f1f'); sea.addColorStop(.55,'#2b0710');
  sea.addColorStop(1,'#0c0206');
  ctx.beginPath(); ctx.arc(0,0,R,0,7); ctx.fillStyle=sea; ctx.fill();

  ctx.save(); ctx.beginPath(); ctx.arc(0,0,R,0,7); ctx.clip();

  ctx.beginPath(); xpTrace(ctx,XPG.grat,R,s0,c0,sl0,cl0,false);
  ctx.strokeStyle='rgba(255,150,168,.13)'; ctx.lineWidth=1; ctx.stroke();

  ctx.beginPath(); xpTrace(ctx,XPG.rings,R,s0,c0,sl0,cl0,true);
  ctx.fillStyle='#c8203f'; ctx.fill();
  ctx.strokeStyle='rgba(255,196,206,.45)'; ctx.lineWidth=.8; ctx.stroke();

  // routes: great circles, clipped to the near face so none cut through the ball
  for(const a of XPG.arcs){
    const k=Math.min(1,(now-a.t0)/1400);
    ctx.beginPath(); xpArcTrace(ctx,a.a,a.b,k,R,s0,c0,sl0,cl0);
    ctx.strokeStyle='rgba(255,214,150,.9)'; ctx.lineWidth=2;
    ctx.lineCap='round'; ctx.stroke();
  }
  // idle scan pings
  for(const p of XPG.pings){
    const q=xpPt3(p.lat,p.lon,R,s0,c0,sl0,cl0); if(q[2]<=0)continue;
    const t=Math.min(1,(now-p.t0)/1900);
    ctx.beginPath(); ctx.arc(q[0],q[1],3+t*20,0,7);
    ctx.strokeStyle='rgba(255,226,232,'+(.85*(1-t)).toFixed(3)+')';
    ctx.lineWidth=1.3; ctx.stroke();
  }
  // pins, newest one pulsing like a hot mirror facet
  ctx.save(); ctx.globalCompositeOperation='lighter';
  for(let i=0;i<XPG.pins.length;i++){
    const p=XPG.pins[i], last=i===XPG.pins.length-1;
    const q=xpPt3(p.lat,p.lon,R,s0,c0,sl0,cl0); if(q[2]<=0)continue;
    if(last){
      const t=((now-p.t0)%2400)/2400;
      ctx.beginPath(); ctx.arc(q[0],q[1],4+t*26,0,7);
      ctx.strokeStyle='rgba(255,224,180,'+(.85*(1-t)).toFixed(3)+')';
      ctx.lineWidth=1.6; ctx.stroke();
      const L=16+4*Math.sin(now*.006);
      const g=ctx.createRadialGradient(q[0],q[1],0,q[0],q[1],L);
      g.addColorStop(0,'rgba(255,240,220,.85)'); g.addColorStop(1,'rgba(255,120,90,0)');
      ctx.beginPath(); ctx.arc(q[0],q[1],L,0,7); ctx.fillStyle=g; ctx.fill();
      ctx.strokeStyle='rgba(255,235,215,.7)'; ctx.lineWidth=1;
      ctx.beginPath();
      ctx.moveTo(q[0]-L,q[1]); ctx.lineTo(q[0]+L,q[1]);
      ctx.moveTo(q[0],q[1]-L); ctx.lineTo(q[0],q[1]+L); ctx.stroke();
    }
    ctx.beginPath(); ctx.arc(q[0],q[1],last?4.2:3,0,7);
    ctx.fillStyle=last?'#fff3e2':'rgba(255,220,200,.5)'; ctx.fill();
  }
  ctx.restore();

  // terminator + specular sheen, same trick the mirror ball uses
  const term=ctx.createRadialGradient(-R*.32,-R*.36,R*.1,0,0,R*1.06);
  term.addColorStop(.5,'rgba(0,0,0,0)'); term.addColorStop(1,'rgba(6,0,3,.78)');
  ctx.beginPath(); ctx.arc(0,0,R,0,7); ctx.fillStyle=term; ctx.fill();
  const sheen=ctx.createRadialGradient(-R*.42,-R*.5,R*.05,-R*.42,-R*.5,R*.95);
  sheen.addColorStop(0,'rgba(255,222,230,.13)'); sheen.addColorStop(1,'rgba(255,222,230,0)');
  ctx.beginPath(); ctx.arc(0,0,R,0,7); ctx.fillStyle=sheen; ctx.fill();
  ctx.restore();   // un-clip

  // rim light + atmosphere bloom
  ctx.beginPath(); ctx.arc(0,0,R,0,7);
  ctx.strokeStyle='rgba(255,120,140,.55)'; ctx.lineWidth=1.4; ctx.stroke();
  const atm=ctx.createRadialGradient(0,0,R,0,0,R*1.22);
  atm.addColorStop(0,'rgba(255,70,100,.30)'); atm.addColorStop(1,'rgba(255,70,100,0)');
  ctx.beginPath(); ctx.arc(0,0,R*1.22,0,7); ctx.fillStyle=atm; ctx.fill();
  ctx.restore();
}

function xpPt3(lat,lon,R,s0,c0,sl0,cl0){
  const sla=Math.sin(lat*D2R),cla=Math.cos(lat*D2R),
        slo=Math.sin(lon*D2R),clo=Math.cos(lon*D2R);
  const dc=clo*cl0+slo*sl0, ds=slo*cl0-clo*sl0;
  return [R*cla*ds,-R*(c0*sla-s0*cla*dc),s0*sla+c0*cla*dc];
}
// Great circle by slerp, cut at the horizon so a route never crosses the ball.
function xpArcTrace(ctx,a,b,k,R,s0,c0,sl0,cl0){
  const A=[Math.cos(a[0]*D2R)*Math.cos(a[1]*D2R),Math.cos(a[0]*D2R)*Math.sin(a[1]*D2R),Math.sin(a[0]*D2R)],
        B=[Math.cos(b[0]*D2R)*Math.cos(b[1]*D2R),Math.cos(b[0]*D2R)*Math.sin(b[1]*D2R),Math.sin(b[0]*D2R)];
  const dot=Math.max(-1,Math.min(1,A[0]*B[0]+A[1]*B[1]+A[2]*B[2])), om=Math.acos(dot);
  const sn=Math.sin(om), N=48;
  let open=false;
  for(let i=0;i<=N*k;i++){
    const t=i/N;
    const w1=om<1e-6?1-t:Math.sin((1-t)*om)/sn, w2=om<1e-6?t:Math.sin(t*om)/sn;
    let x=A[0]*w1+B[0]*w2, y=A[1]*w1+B[1]*w2, z=A[2]*w1+B[2]*w2;
    const m=Math.hypot(x,y,z); x/=m; y/=m; z/=m;
    const lat=Math.asin(z)/D2R, lon=Math.atan2(y,x)/D2R;
    const q=xpPt3(lat,lon,R,s0,c0,sl0,cl0);
    if(q[2]<=0){open=false;continue;}
    if(open)ctx.lineTo(q[0],q[1]); else ctx.moveTo(q[0],q[1]);
    open=true;
  }
}
function xpSpinTo(lat,lon){
  XPG.from=[XPG.lat0,XPG.lon0];
  let d=lon-XPG.lon0; while(d>180)d-=360; while(d<-180)d+=360;
  XPG.to=[Math.max(-55,Math.min(55,lat)),XPG.lon0+d];
  XPG.t0=performance.now();
}
function xpPin(lat,lon){XPG.pins.push({lat,lon,t0:performance.now()});}
function xpArc(a,b){XPG.arcs.push({a,b,t0:performance.now()});}
function xpScanStart(){
  xpScanStop();
  const places=Object.values(XPG.home||{});
  let near=places.slice();
  // Pings are drawn from places near whatever face is turned toward us, else
  // most of them fire on the far side and the planet still looks idle.
  const focus=h=>{near=places.slice().sort((a,b)=>
    Math.hypot(a[2]-h[2],a[3]-h[3])-Math.hypot(b[2]-h[2],b[3]-h[3])).slice(0,14);};
  XPG.scanT=setInterval(()=>{
    const h=near[Math.floor(Math.random()*near.length)]; if(!h)return;
    const rec={lat:h[2],lon:h[3],t0:performance.now()};
    XPG.pings.push(rec);
    setTimeout(()=>{XPG.pings=XPG.pings.filter(x=>x!==rec);},2000);
  },520);
  const drift=()=>{
    const h=places[Math.floor(Math.random()*places.length)]; if(!h)return;
    focus(h);
    $('#xpfx-place').textContent='scanning '+h[0]+', '+h[1]+'…';
    xpSpinTo(h[2],h[3]);
    XPG.scanD=setTimeout(drift,2900);
  };
  drift();
}
function xpScanStop(){
  clearInterval(XPG.scanT); clearTimeout(XPG.scanD);
  XPG.scanT=null; XPG.scanD=null; XPG.pings=[];
}
function xpStop(genre){
  xpScanStop();
  XPG.stops++;
  $('#xpfx-title').textContent='World tour · stop '+XPG.stops;
  $('#xpfx-genre').textContent=genre;
  $('#xpfx-feed').innerHTML='';
  const h=(XPG.home||{})[genre];
  if(!h){$('#xpfx-place').textContent='somewhere off the map';return;}
  $('#xpfx-place').textContent=h[0]+', '+h[1];
  const ll=[h[2],h[3]];
  xpPin(ll[0],ll[1]);
  if(XPG.lastLL)xpArc(XPG.lastLL,ll);
  XPG.lastLL=ll;
  xpSpinTo(ll[0],ll[1]);
  const stage=$('#xpfx-stage'); if(stage)stage.style.transform='scale(1.06)';
}
function xpFound(text){
  XPG.finds++;
  const ul=$('#xpfx-feed'); if(!ul||!text)return;
  const li=document.createElement('li'); li.textContent=text;
  ul.insertBefore(li,ul.firstChild);
  while(ul.children.length>5)ul.removeChild(ul.lastChild);
}
function xpPhase(name){
  if(name!=='resolving')return;
  $('#xpfx-title').textContent=XPG.finds+' finds · '+XPG.stops+' places';
  $('#xpfx-genre').textContent='Bringing them home';
  $('#xpfx-place').textContent='Matching every find on YouTube…';
  $('#xpfx-feed').innerHTML='';
  const stage=$('#xpfx-stage'); if(stage)stage.style.transform='';
}
function xpfxMark(line){
  const parts=line.slice(2).split('|');
  if(parts[0]==='stop')xpStop(parts[1]||'');
  else if(parts[0]==='found')xpFound(parts[2]||'');
  else if(parts[0]==='phase')xpPhase(parts[1]||'');
}
// ?xpdemo=1 replays a fake route so the overlay can be watched without burning
// an actual XP+ run (each real run publishes a playlist).
async function xpDemo(){
  await showXPFX();
  const names=Object.keys(XPG.home||{});
  const pick=n=>names.sort(()=>Math.random()-.5).slice(0,n);
  let d=3200;
  for(const g of pick(5)){
    setTimeout(()=>{xpStop(g);
      [1,2,3].forEach(i=>setTimeout(()=>xpFound('Artist '+i+' — a track'),i*600));
    },d);
    d+=4200;
  }
  setTimeout(()=>xpPhase('resolving'),d);
  setTimeout(()=>hideXPFX(true),d+3200);
}
async function showXPFX(){
  const fx=$('#xpfx'); if(!fx)return;
  XPG.stops=0; XPG.finds=0; XPG.lastLL=null;
  $('#xpfx-title').textContent='World tour';
  $('#xpfx-genre').textContent='Plotting a route';
  $('#xpfx-place').textContent='Picking corners you have not heard yet…';
  $('#xpfx-feed').innerHTML='';
  fx.classList.remove('out'); fx.classList.add('on');
  await xpMapLoad(); xpGlobeBuild(); xpScanStart();
}
function hideXPFX(ok,why){return new Promise(res=>{
  xpScanStop();
  const fx=$('#xpfx'); if(!fx){res();return;}
  $('#xpfx-title').textContent=ok?'Tour complete':'';
  $('#xpfx-genre').textContent=ok?'Home again'
    :(why?'Could not publish':'Nothing new this time');
  $('#xpfx-place').textContent=ok
    ?XPG.finds+' finds from '+XPG.stops+' places'
    :(why||'Try again in a moment.');
  const stage=$('#xpfx-stage'); if(stage)stage.style.transform='';
  setTimeout(()=>{fx.classList.add('out');
    setTimeout(()=>{fx.classList.remove('on','out');
      cancelAnimationFrame(XPG.raf); XPG.raf=0; res();},600);},1400);
});}

// A run outlives the nodes that show it: finishing one calls load() -> render(),
// which rebuilds every tab and orphans the button and log the run was writing
// to. So runs write their state here and paintRuns() copies it onto whatever
// nodes exist now — otherwise starting Irish, then tapping Timeline, wipes the
// Irish tab back to blank while its run is still going.
const RUNS={};
const RUN_EL={xp:['#xpbtn','#log'],irish:['#irish-run','#irish-setlog'],
              timeline:['#timeline-run','#timeline-setlog']};

function runBusy(){for(const k in RUNS)if(RUNS[k].busy)return true; return false;}

function paintRuns(){
  const busy=runBusy();
  for(const id in RUN_EL){
    const r=RUNS[id], btn=$(RUN_EL[id][0]), log=$(RUN_EL[id][1]);
    // Every run button is disabled while any run is going: they share one lock
    // server-side, so a second tap only ever means waiting.
    if(btn){btn.disabled=busy; if(r&&r.busy&&r.label)btn.textContent=r.label;}
    if(log&&r){log.style.display=r.open?'block':'none';
      if(log.textContent!==r.text){log.textContent=r.text;
        log.scrollTop=log.scrollHeight;}}
  }
}

async function runXP(){
  if(runBusy())return;
  const r=RUNS.xp={busy:true,label:'',text:'',open:false};
  paintRuns();
  await showXPFX().catch(()=>{});  // a map that won't load must not block the run
  let ok=false, buf='';
  try{const res=await fetch('/api/xp',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({})});
    const reader=res.body.getReader(), dec=new TextDecoder();
    for(;;){const{done,value}=await reader.read(); if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n'); buf=lines.pop();
      // `::`-prefixed lines drive the map; everything else is the plain log.
      for(const line of lines){
        if(line.startsWith('::'))xpfxMark(line); else r.text+=line+'\n';}
      paintRuns();}
    if(buf){if(buf.startsWith('::'))xpfxMark(buf); else r.text+=buf;}
    ok=/Created 'XP/.test(r.text);
  }catch(e){r.text+='\n[error] '+e;}
  const why=/invalid_grant|Google auth expired/.test(r.text)
    ?'Google sign-in expired — run reauth_google.py':'';
  await hideXPFX(ok,why);
  r.busy=false;
  r.open=!ok;  // surface the log so a dud run is explainable
  XP_REVEAL=ok;
  paintRuns();
  await load();
  // Reveal the new set: jump to the XP+ tab.
  const xpTab=document.querySelector('.side nav button[data-tab=xp]');
  if(ok&&xpTab)xpTab.click();
}

// Irish Mode / Timeline: same streamed run as XP+, but the button and log live
// in the tab itself rather than on the Explore hero, and there's no world map.
async function runSet(o){
  if(runBusy())return;
  const r=RUNS[o.id]={busy:true,label:o.busy,text:'',open:true};
  paintRuns();
  let ok=false, buf='';
  try{const res=await fetch(o.api,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({})});
    const reader=res.body.getReader(), dec=new TextDecoder();
    for(;;){const{done,value}=await reader.read(); if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n'); buf=lines.pop();
      // `::` lines drive the XP+ map; here they're just progress, so show the
      // artist as it's found and drop the rest.
      for(const line of lines){
        if(line.startsWith('::found|')){r.label=line.split('|').pop().slice(0,34)+'…';}
        else if(!line.startsWith('::'))r.text+=line+'\n';}
      paintRuns();}
    if(buf&&!buf.startsWith('::'))r.text+=buf;
    ok=new RegExp("Created '"+o.prefix).test(r.text);
  }catch(e){r.text+='\n[error] '+e;}
  if(/invalid_grant|Google auth expired/.test(r.text))
    r.text+='\nGoogle sign-in expired — run reauth_google.py\n';
  r.busy=false;
  r.open=!ok;   // clean on success, kept open to explain a dud
  XP_REVEAL=ok;
  paintRuns();
  await load();
}
const runIrish=()=>runSet({id:'irish',api:'/api/irish',prefix:'Irish',
  busy:'Gathering the session…'});
const runTimeline=()=>runSet({id:'timeline',api:'/api/timeline',prefix:'Time',
  busy:'Winding back…'});

async function runDigest(){const btn=$('#digestbtn'), log=$('#log');
  btn.disabled=true; btn.textContent='rolling up…'; log.style.display='block'; log.textContent='';
  try{const res=await fetch('/api/digest',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({days:7})});
    const reader=res.body.getReader(), dec=new TextDecoder();
    for(;;){const{done,value}=await reader.read(); if(done)break;
      log.textContent+=dec.decode(value,{stream:true}); log.scrollTop=log.scrollHeight;}
  }catch(e){log.textContent+='\n[error] '+e;}
  btn.disabled=false; await load();
}

// Download a batch, turning `btn` itself into a live progress bar (fed by the
// global DLS poll) instead of dumping text into a log box. Reverts when done;
// per-track detail still lives in the Downloads tab.
async function runDownload(ids,btn){
  ids=(ids||[]).filter(Boolean); if(!ids.length)return false;
  const compact=btn&&btn.classList.contains('dchip');
  const orig=btn?(btn.dataset.orig??btn.innerHTML):'';
  let started=false;  // flips once this job (not one ahead of it) is running
  if(btn){btn.dataset.orig=orig; btn.disabled=true; btn.classList.add('dl-run');
    btn.style.setProperty('--dlp','0%'); btn.textContent=compact?'…':'Starting…';}
  // Progress reflects the shared DLS snapshot, but only once OUR job is running —
  // while queued behind another download, don't paint that job's progress here.
  const tick=setInterval(()=>{if(!btn||!started)return; const its=DLS.items||[]; if(!its.length)return;
    const fin=its.filter(x=>['done','have','failed'].includes(x.status)).length;
    const prog=its.reduce((s,x)=>s+(['done','have','tagging'].includes(x.status)?100
      :(x.status==='downloading'?(x.pct||0):0)),0);
    const pct=Math.round(prog/(its.length*100)*100);
    btn.style.setProperty('--dlp',pct+'%');
    btn.textContent=compact?pct+'%':`⬇ ${fin}/${its.length} · ${pct}%`;},350);
  let ok=false;
  try{const res=await fetch('/api/download',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({video_ids:ids})});
    const rd=res.body.getReader(); const dec=new TextDecoder(); let buf='';
    for(;;){const{done,value}=await rd.read(); if(done)break;
      buf+=dec.decode(value,{stream:true});
      if(!started){
        if(/Downloading /.test(buf)){started=true;}      // our job just began
        else if(/Queued/.test(buf)&&btn){btn.textContent=compact?'⏳':'Queued…';}
      }
    }
    ok=true;
  }catch(e){}
  clearInterval(tick);
  if(btn){btn.classList.remove('dl-run'); btn.style.removeProperty('--dlp'); btn.disabled=false;
    const failed=(DLS.items||[]).filter(x=>x.status==='failed').length;
    if(failed){btn.classList.add('dl-fail','dl-run');
      btn.textContent=compact?'⚠':`⚠ ${failed} failed — see Downloads`;}
    else{btn.textContent=compact?'✓':'✓ Done';}
    setTimeout(()=>{btn.classList.remove('dl-fail','dl-run');
      btn.innerHTML=orig; delete btn.dataset.orig;},3200);}
  await load();
  return ok;
}

// Built once (idempotent) so streaming/typing survives the periodic render().
function renderGet(){const root=$('#get'); if(!root||root.firstChild)return;
  const sh=el('div','sec-h');
  sh.innerHTML=`<h3>Get — Downloader <span class="spark">⬇</span></h3>
    <span class="cnt">paste any YouTube link</span>`;
  root.appendChild(sh);
  const c=el('div','gcard');
  c.innerHTML=`
    <input id="geturl" class="getin" placeholder="https://youtube.com/watch?v=…  or a playlist link">
    <div class="gethint">Playlists download every track. Files land in Downloads/MusicXP/Get.</div>
    <div class="getopts">
      <label class="getseg">Format
        <select id="getmode">
          <option value="audio">Audio · highest quality (.m4a)</option>
          <option value="video">Video (.mp4)</option>
        </select></label>
      <label class="getseg" id="getreswrap" style="display:none">Max resolution
        <select id="getres">
          <option value="2160">2160p · 4K</option>
          <option value="1440">1440p · 2K</option>
          <option value="1080" selected>1080p · Full HD</option>
          <option value="720">720p · HD</option>
          <option value="480">480p</option>
          <option value="360">360p</option>
        </select></label>
      <button id="getbtn" class="cta">⬇ Download</button>
    </div>
    <pre id="getlog"></pre>`;
  root.appendChild(c);
  $('#getmode').onchange=e=>{$('#getreswrap').style.display=e.target.value==='video'?'':'none';};
  $('#getbtn').onclick=runGet;
  $('#geturl').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();runGet();}});
}

async function runGet(){const btn=$('#getbtn'), log=$('#getlog');
  const url=($('#geturl').value||'').trim();
  log.style.display='block';
  if(!/^https?:\/\//i.test(url)){log.textContent='Enter a valid http(s) link.'; return;}
  const mode=$('#getmode').value, height=$('#getres').value;
  btn.disabled=true; const orig=btn.textContent; btn.textContent='downloading…'; log.textContent='';
  try{const res=await fetch('/api/get',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url,mode,height})});
    if(!res.ok){let m='HTTP '+res.status; try{m=(await res.json()).error||m;}catch(e){} log.textContent=m;}
    else{const reader=res.body.getReader(), dec=new TextDecoder();
      for(;;){const{done,value}=await reader.read(); if(done)break;
        log.textContent+=dec.decode(value,{stream:true}); log.scrollTop=log.scrollHeight;}
      log.textContent+='\n✓ Done — saved to Downloads/MusicXP/Get\n'; log.scrollTop=log.scrollHeight;}
  }catch(e){log.textContent+='\n[error] '+e;}
  btn.disabled=false; btn.textContent=orig;
}

// ── Library tab: folders of files on this Mac, played offline ──
// LIB.list holds player-shaped tracks (video_id 'local:<id>'), so the same
// queue, transport and full-screen view work for disk and YouTube alike.
let LIB=null, LIBVIEW='albums', LIBQ='', LIBOPEN=null, LIBSHOWN=200, LIBPOLL=null;

function libGroup(keyOf,nameOf,artOf){
  const m=new Map();
  LIB.list.forEach(t=>{const k=keyOf(t); let g=m.get(k);
    if(!g){g={key:k,name:nameOf(t),sub:artOf(t),tracks:[]}; m.set(k,g);}
    g.tracks.push(t);});
  return [...m.values()];
}
// Folders on disk are a library view of their own: the tags may be empty or
// wrong, but the way the files were filed by hand never is.
function libDirs(t){
  const p=t.path||'', dir=p.slice(0,p.lastIndexOf('/'));
  const root=(t.root||'').replace(/\/+$/,'');
  const rel=dir.startsWith(root)?dir.slice(root.length).replace(/^\/+/,''):dir;
  const segs=rel?rel.split('/'):[];
  const rootname=root.split('/').filter(Boolean).pop()||root||'/';
  return {fdir:dir||root,fname:segs.length?segs[segs.length-1]:rootname,
          fpath:[rootname,...segs.slice(0,-1)].join(' / ')};
}
function libShape(){
  LIB.list=(LIB.tracks||[]).map(t=>Object.assign({video_id:'local:'+t.id,title:t.title,
    artist:t.artist,album:t.album,albumartist:t.albumartist||t.artist,
    dur:t.dur,track:t.track,disc:t.disc,year:t.year,ext:t.ext,url:''},libDirs(t)));
  LIB.albums=libGroup(t=>(t.albumartist||t.artist).toLowerCase()+'\u0000'+(t.album||'').toLowerCase(),
    t=>t.album||'Loose tracks', t=>t.albumartist||t.artist);
  LIB.artists=libGroup(t=>(t.artist||'').toLowerCase(), t=>t.artist,
    t=>'').map(g=>Object.assign(g,{sub:g.tracks.length+' track'+(g.tracks.length===1?'':'s')}));
  LIB.folders=libGroup(t=>t.fdir,t=>t.fname,t=>t.fpath);
  LIB.folders.forEach(g=>g.tracks.sort((a,b)=>(a.disc||0)-(b.disc||0)
    ||(a.track||0)-(b.track||0)||a.title.localeCompare(b.title)));
  LIB.albums.sort((a,b)=>a.sub.localeCompare(b.sub)||a.name.localeCompare(b.name));
  LIB.artists.sort((a,b)=>b.tracks.length-a.tracks.length||a.name.localeCompare(b.name));
  LIB.folders.sort((a,b)=>a.sub.localeCompare(b.sub)||a.name.localeCompare(b.name));
}
async function loadLibrary(){
  try{LIB=await(await fetch('/api/library')).json();}catch(e){LIB={roots:[],tracks:[],scan:{}};}
  ARTREV=LIB.artrev||0;
  libShape(); renderLibPill(); renderLibrary();
  if(LIB.scan&&LIB.scan.active)startLibPoll();
}
// Playlists arrive as track ids; resolve them against LIB.list so a playlist
// behaves exactly like an album — same rows, same queue, same transport.
function libPlaylists(){
  const by=new Map(LIB.list.map(t=>[t.video_id.slice(6),t]));
  return (LIB.playlists||[]).map(p=>({id:p.id,name:p.name,missing:p.missing||0,
    tracks:p.tracks.map(i=>by.get(i)).filter(Boolean)}));
}
async function plPost(body){
  const r=await fetch('/api/playlists',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let j={}; try{j=await r.json();}catch(e){}
  if(!r.ok)return {error:j.error||'that did not work'};
  const s=await(await fetch('/api/library')).json();
  LIB=s; ARTREV=s.artrev||ARTREV; libShape();
  return j;
}
function startLibPoll(){
  if(LIBPOLL)return;
  LIBPOLL=setInterval(async()=>{
    let s; try{s=await(await fetch('/api/library')).json();}catch(e){return;}
    const wasActive=LIB&&LIB.scan&&LIB.scan.active;
    LIB=s; ARTREV=s.artrev||ARTREV; libShape(); renderLibPill();
    if(!s.scan.active){clearInterval(LIBPOLL); LIBPOLL=null;}
    if(!s.scan.active&&wasActive)LIBOPEN=null;
    if($('#library').classList.contains('active'))renderLibrary();
  },1200);
}
function renderLibPill(){const p=$('#libpill'); if(!p)return;
  const n=(LIB&&LIB.tracks||[]).length;
  p.textContent=(LIB&&LIB.scan&&LIB.scan.active)?'⟳':(n?n:'');}

function libGroups(view){
  return view==='artists'?LIB.artists:view==='folders'?LIB.folders:LIB.albums;
}
function libMatch(t){
  if(!LIBQ)return true;
  return (t.title+' '+t.artist+' '+t.album+' '+(t.fpath||'')+' '+(t.fname||''))
    .toLowerCase().includes(LIBQ);
}
function libFiltered(){return LIB.list.filter(libMatch);}
function markLibPlaying(){
  document.querySelectorAll('.ltrow').forEach(r=>
    r.classList.toggle('playing',r.dataset.vid===CURVID));
}
function libPlay(list,vid){
  if(!list.length)return;
  PLAYLIST=list.slice(); ORDER=[];
  playTrack(vid||list[0].video_id);
}
function libShuffle(list){
  if(!list.length)return;
  if(!SHUFFLE){SHUFFLE=true; $('#p-shuf').classList.add('on');}
  libPlay(list,list[Math.floor(Math.random()*list.length)].video_id);
}
function libRow(t,i,list,showNum,inPl){
  const r=el('div','ltrow'+(t.video_id===CURVID?' playing':''));
  r.dataset.vid=t.video_id;
  const bad=['.ogg','.opus','.wma'].includes(t.ext)?
    `<span class="ltx">${esc(t.ext.slice(1))}</span>`:'';
  r.innerHTML=(showNum?`<div class="ltn">${t.track||i+1}</div>`
      :`<div class="ltth" style="background-image:url('${thumb(t.video_id)}')"></div>`)
    +`<div class="ltm"><div class="ltt">${esc(t.title)}</div>
      <div class="lta">${esc(t.artist)}${t.album?' · '+esc(t.album):''}</div></div>`
    +bad+`<div class="ltd">${t.dur?fmt(t.dur):''}</div>`;
  const act=el('button','ltadd',inPl?'✕':'+');
  act.title=inPl?'Remove from this playlist':'Add to a playlist';
  act.onclick=async e=>{e.stopPropagation();
    if(inPl){await plPost({action:'remove',id:inPl,track:t.video_id.slice(6)});
      renderLibrary();}
    else addToPlaylist([t.video_id.slice(6)],t.title);};
  r.appendChild(act);
  r.onclick=()=>{if(t.video_id===CURVID)$('#p-play').onclick();
    else libPlay(list,t.video_id);};
  return r;
}
// One sheet handles both "add these tracks" and "make a playlist out of them",
// so adding a whole album is the same gesture as adding one song.
function addToPlaylist(tids,label){
  const lists=libPlaylists();
  const ovl=el('div','ovl');
  ovl.innerHTML=`<div class="modal pl-modal">
    <div class="mh"><h3>Add to playlist</h3><button class="mx" title="Close">✕</button></div>
    <div class="plsub">${esc(label||(tids.length+' tracks'))}</div>
    <div class="mlist plpick">${lists.map(p=>`<button class="plopt" data-id="${p.id}">
        <span class="pln">${esc(p.name)}</span>
        <span class="plc">${p.tracks.length}</span></button>`).join('')
      ||'<div class="muted" style="padding:12px 4px">No playlists yet.</div>'}</div>
    <div class="plnew"><input type="text" class="plname" placeholder="New playlist name…">
      <button class="cta sm plmk">Create &amp; add</button></div>
    <div class="mf"><span class="mcount plmsg"></span></div></div>`;
  const close=()=>ovl.remove();
  const msg=t=>ovl.querySelector('.plmsg').textContent=t;
  ovl.querySelector('.mx').onclick=close;
  ovl.onclick=e=>{if(e.target===ovl)close();};
  ovl.querySelectorAll('.plopt').forEach(b=>b.onclick=async()=>{
    b.disabled=true;
    const r=await plPost({action:'add',id:b.dataset.id,tracks:tids});
    if(r.error){msg(r.error); b.disabled=false; return;}
    close(); renderLibrary();});
  const mk=async()=>{
    const inp=ovl.querySelector('.plname'), name=(inp.value||'').trim();
    if(!name){inp.focus();return;}
    const c=await plPost({action:'create',name});
    if(c.error){msg(c.error);return;}
    await plPost({action:'add',id:c.id,tracks:tids});
    close(); LIBVIEW='playlists'; renderLibrary();};
  ovl.querySelector('.plmk').onclick=mk;
  ovl.querySelector('.plname').addEventListener('keydown',
    e=>{if(e.key==='Enter'){e.preventDefault();mk();}});
  document.body.appendChild(ovl);
  ovl.querySelector('.plname').focus();
}
function renderLibrary(){
  const root=$('#library'); if(!root)return; root.innerHTML='';
  if(!LIB){root.appendChild(el('div','gcard muted','Reading your library…')); return;}
  const scan=LIB.scan||{};
  const sh=el('div','sec-h');
  sh.innerHTML=`<h3>Library <span class="spark">♫</span></h3>
    <div class="sh-r"><span class="cnt">${LIB.tracks.length} track${
      LIB.tracks.length===1?'':'s'} · ${LIB.albums.length} album${
      LIB.albums.length===1?'':'s'}</span></div>`;
  const rb=el('button','cta ghost sm',scan.active?'⟳ Scanning…':'⟳ Rescan');
  rb.disabled=!!scan.active||!LIB.roots.length;
  rb.onclick=async()=>{await fetch('/api/library/scan',{method:'POST'});
    LIB.scan={active:true,done:0,total:0}; renderLibrary(); startLibPoll();};
  sh.querySelector('.sh-r').appendChild(rb);
  root.appendChild(sh);

  // folders
  const fc=el('div','gcard');
  fc.innerHTML=`<div class="librow">
      <input type="text" id="librootin" placeholder="Paste a folder path — e.g. /Users/zei/Music/Albums">
      <button class="cta sm" id="librootadd">+ Add folder</button></div>
    <div class="libroots" id="librootlist"></div>
    <div class="gethint" style="margin-top:10px">Drag a folder onto the box to paste its
      path, or copy it in Finder with ⌥⌘C. Nothing is moved or copied — files play
      where they sit.</div>`;
  root.appendChild(fc);
  const rl=$('#librootlist');
  if(!LIB.roots.length)rl.innerHTML='<span class="muted">No folders yet.</span>';
  LIB.roots.forEach(p=>{const chip=el('div','libroot');
    chip.innerHTML=`<span title="${esc(p)}">${esc(p)}</span>`;
    const x=el('button',null,'✕'); x.title='Remove from library';
    x.onclick=async()=>{await fetch('/api/library/roots',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'remove',path:p})}); LIBOPEN=null; loadLibrary();};
    chip.appendChild(x); rl.appendChild(chip);});
  const addRoot=async()=>{
    const inp=$('#librootin'), v=(inp.value||'').trim(); if(!v)return;
    const btn=$('#librootadd'); btn.disabled=true;
    const res=await fetch('/api/library/roots',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({path:v})});
    btn.disabled=false;
    if(!res.ok){let m='could not add that folder';
      try{m=(await res.json()).error||m;}catch(e){}
      inp.value=''; inp.placeholder=m; return;}
    inp.value=''; loadLibrary();
  };
  $('#librootadd').onclick=addRoot;
  const inp=$('#librootin');
  inp.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();addRoot();}});
  inp.addEventListener('dragover',e=>e.preventDefault());
  inp.addEventListener('drop',e=>{e.preventDefault();
    const f=e.dataTransfer.files&&e.dataTransfer.files[0];
    // Browsers hide real paths, so fall back to the text/uri-list the Finder sends.
    const uri=e.dataTransfer.getData('text/uri-list')||e.dataTransfer.getData('text');
    inp.value=uri||(f?f.name:''); if(inp.value)addRoot();});

  if(scan.active){
    const pct=scan.total?Math.round(scan.done/scan.total*100):0;
    root.appendChild(el('div','gcard muted',
      `Indexing ${scan.done}/${scan.total||'…'} files (${pct}%) — reading tags and cover art.`));
  }
  if(scan.error)root.appendChild(el('div','gcard muted','Scan failed: '+esc(scan.error)));
  if(!LIB.tracks.length){
    if(!scan.active)root.appendChild(el('div','gcard muted',
      LIB.roots.length?'No audio files found in those folders.'
      :'Add a folder above and everything inside it becomes playable here — no internet needed.'));
    return;
  }

  // toolbar
  const bar=el('div','libbar');
  const seg=el('div','libseg');
  [['albums','Albums'],['artists','Artists'],['folders','Folders'],
   ['songs','Songs'],['playlists','Playlists']].forEach(([k,label])=>{
    const b=el('button',LIBVIEW===k?'on':null,label);
    b.onclick=()=>{LIBVIEW=k; LIBOPEN=null; LIBSHOWN=200; renderLibrary();};
    seg.appendChild(b);});
  bar.appendChild(seg);
  const find=el('input','libfind');
  find.type='text'; find.placeholder='Find in library…'; find.value=LIBQ;
  find.oninput=e=>{LIBQ=e.target.value.toLowerCase().trim(); LIBOPEN=null; LIBSHOWN=200;
    renderLibrary(); const f=$('.libfind'); if(f){f.focus();
      f.setSelectionRange(f.value.length,f.value.length);}};
  bar.appendChild(find);
  const sb=el('button','cta ghost sm','⤨ Shuffle all');
  sb.onclick=()=>libShuffle(libFiltered());
  bar.appendChild(sb);
  if(LIB.scanned)bar.appendChild(el('span','libscan',
    'indexed '+new Date(LIB.scanned*1000).toLocaleString()));
  root.appendChild(bar);

  if(LIBOPEN){root.appendChild(libPanel()); markLibPlaying(); return;}

  if(LIBVIEW==='playlists'){
    const lists=libPlaylists();
    const nb=el('button','cta sm libnewpl','+ New playlist');
    nb.onclick=()=>newPlaylist();
    const wrap=el('div','plhead'); wrap.appendChild(nb);
    root.appendChild(wrap);
    if(!lists.length){root.appendChild(el('div','gcard muted',
      'No playlists yet. Make one here, or hit + on any song.')); return;}
    const grid=el('div','libgrid');
    lists.forEach(g=>{
      const art=g.tracks.length?thumb(g.tracks[0].video_id):'/assets/logo-256.png?v=3';
      const c=el('div','libcard');
      c.innerHTML=`<div class="libart" style="background-image:url('${art}')">
          <div class="n">${g.tracks.length}</div><button class="go" title="Play">▶</button></div>
        <div class="meta"><div class="lt">${esc(g.name)}</div>
          <div class="la">${g.tracks.length} track${g.tracks.length===1?'':'s'}${
            g.missing?' · '+g.missing+' missing':''}</div></div>`;
      c.onclick=()=>{LIBOPEN={view:'playlists',key:g.id}; renderLibrary(); window.scrollTo(0,0);};
      c.querySelector('.go').onclick=e=>{e.stopPropagation(); libPlay(g.tracks);};
      grid.appendChild(c);});
    root.appendChild(grid); markLibPlaying(); return;
  }

  if(LIBVIEW==='songs'){
    const list=libFiltered();
    if(!list.length){root.appendChild(el('div','gcard muted','Nothing matches that.')); return;}
    const box=el('div','libtracks');
    // Long libraries render in slices: 2000 rows at once stalls the 2012 GPU.
    list.slice(0,LIBSHOWN).forEach((t,i)=>box.appendChild(libRow(t,i,list,false)));
    root.appendChild(box);
    if(list.length>LIBSHOWN){
      const more=el('button','cta ghost libmore',
        `Show more — ${list.length-LIBSHOWN} left`);
      more.onclick=()=>{LIBSHOWN+=400; renderLibrary();};
      root.appendChild(more);}
    markLibPlaying(); return;
  }

  const groups=libGroups(LIBVIEW)
    .map(g=>({...g,tracks:g.tracks.filter(libMatch)})).filter(g=>g.tracks.length);
  if(!groups.length){root.appendChild(el('div','gcard muted','Nothing matches that.')); return;}
  const grid=el('div','libgrid');
  groups.forEach(g=>{
    const c=el('div','libcard');
    c.innerHTML=`<div class="libart" style="background-image:url('${thumb(g.tracks[0].video_id)}')">
        <div class="n">${g.tracks.length}</div><button class="go" title="Play">▶</button></div>
      <div class="meta"><div class="lt">${esc(g.name)}</div>
        <div class="la">${esc(g.sub||'')}</div></div>`;
    c.onclick=()=>{LIBOPEN={view:LIBVIEW,key:g.key}; renderLibrary(); window.scrollTo(0,0);};
    c.querySelector('.go').onclick=e=>{e.stopPropagation(); libPlay(g.tracks);};
    grid.appendChild(c);});
  root.appendChild(grid);
  markLibPlaying();
}
function newPlaylist(){
  const ovl=el('div','ovl');
  ovl.innerHTML=`<div class="modal pl-modal">
    <div class="mh"><h3>New playlist</h3><button class="mx" title="Close">✕</button></div>
    <div class="plnew"><input type="text" class="plname" placeholder="Playlist name…">
      <button class="cta sm plmk">Create</button></div>
    <div class="mf"><span class="mcount plmsg"></span></div></div>`;
  const close=()=>ovl.remove();
  ovl.querySelector('.mx').onclick=close;
  ovl.onclick=e=>{if(e.target===ovl)close();};
  const mk=async()=>{
    const inp=ovl.querySelector('.plname'), name=(inp.value||'').trim();
    if(!name){inp.focus();return;}
    const c=await plPost({action:'create',name});
    if(c.error){ovl.querySelector('.plmsg').textContent=c.error;return;}
    close(); LIBVIEW='playlists'; renderLibrary();};
  ovl.querySelector('.plmk').onclick=mk;
  ovl.querySelector('.plname').addEventListener('keydown',
    e=>{if(e.key==='Enter'){e.preventDefault();mk();}});
  document.body.appendChild(ovl);
  ovl.querySelector('.plname').focus();
}
function libPanel(){
  const pl=LIBOPEN.view==='playlists';
  const g0=pl?libPlaylists().find(x=>x.id===LIBOPEN.key)
    :libGroups(LIBOPEN.view).find(x=>x.key===LIBOPEN.key);
  const box=el('div','libpanel');
  if(!g0){box.appendChild(el('div','muted',pl?'That playlist is gone.'
    :'That group is gone — rescan?')); return box;}
  const tracks=g0.tracks.filter(libMatch);
  const art=tracks.length?thumb(tracks[0].video_id):'/assets/logo-256.png?v=3';
  const sub=pl?(tracks.length+' track'+(tracks.length===1?'':'s')
      +(g0.missing?' · '+g0.missing+' missing':''))
    :esc(g0.sub||'')+' · '+tracks.length+' track'+(tracks.length===1?'':'s');
  const h=el('div','ph');
  h.innerHTML=`<div class="pa2" style="background-image:url('${art}')"></div>
    <div><div class="pn">${esc(g0.name)}</div>
      <div class="ps">${sub}</div></div><div class="grow"></div>`;
  const play=el('button','cta sm','▶ Play');
  play.onclick=()=>libPlay(tracks);
  const shuf=el('button','cta ghost sm','⤨ Shuffle');
  shuf.onclick=()=>libShuffle(tracks);
  const back=el('button','cta ghost sm','← Back');
  back.onclick=()=>{LIBOPEN=null; renderLibrary();};
  const btns=[play,shuf];
  if(pl){
    const ren=el('button','cta ghost sm','Rename');
    ren.onclick=()=>renamePlaylist(g0);
    const del=el('button','cta ghost sm','Delete');
    del.onclick=async()=>{if(!confirm('Delete "'+g0.name+'"? The files stay put.'))return;
      await plPost({action:'delete',id:g0.id}); LIBOPEN=null; renderLibrary();};
    btns.push(ren,del);
  }else{
    const add=el('button','cta ghost sm','+ Add to playlist');
    add.onclick=()=>addToPlaylist(tracks.map(t=>t.video_id.slice(6)),g0.name);
    btns.push(add);
  }
  btns.push(back);
  btns.forEach(b=>h.appendChild(b));
  box.appendChild(h);
  const list=el('div','libtracks');
  if(pl&&!tracks.length)list.appendChild(el('div','muted',
    'Empty — hit + on any song to drop it in here.'));
  tracks.forEach((t,i)=>list.appendChild(
    libRow(t,i,tracks,LIBOPEN.view==='albums',pl?g0.id:null)));
  box.appendChild(list);
  return box;
}
function renamePlaylist(g){
  const ovl=el('div','ovl');
  ovl.innerHTML=`<div class="modal pl-modal">
    <div class="mh"><h3>Rename playlist</h3><button class="mx" title="Close">✕</button></div>
    <div class="plnew"><input type="text" class="plname"><button class="cta sm plmk">Save</button></div>
    <div class="mf"><span class="mcount plmsg"></span></div></div>`;
  const close=()=>ovl.remove();
  ovl.querySelector('.mx').onclick=close;
  ovl.onclick=e=>{if(e.target===ovl)close();};
  const inp=ovl.querySelector('.plname'); inp.value=g.name;
  const save=async()=>{
    const name=(inp.value||'').trim(); if(!name){inp.focus();return;}
    const r=await plPost({action:'rename',id:g.id,name});
    if(r.error){ovl.querySelector('.plmsg').textContent=r.error;return;}
    close(); renderLibrary();};
  ovl.querySelector('.plmk').onclick=save;
  inp.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();save();}});
  document.body.appendChild(ovl); inp.focus(); inp.select();
}

function downloadNow(){
  openDownloadPicker("Download — Today's Picks",(STATE.today&&STATE.today.picks)||[],$('#dlbtn'));
}

async function dlOne(vid,btn){return runDownload([vid],btn);}

load();
pollDownloads();
setInterval(pollDownloads,1000);
// Sync YouTube likes on start and every 5 min so hearts stay in step with
// anything liked directly in YouTube Music (server caches for 10 min).
loadYtLiked();
setInterval(loadYtLiked,300000);
if(location.search.includes('xpdemo'))setTimeout(xpDemo,400);
</script></body></html>
"""


if __name__ == "__main__":
    main()
