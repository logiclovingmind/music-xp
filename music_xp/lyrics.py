"""Time-synced lyrics from LRCLIB — free, keyless, no account.

LRCLIB is the only lyrics source that survives this environment: Spotify's
is behind the Premium paywall and Musixmatch wants a key, while lrclib.net
answers plain GETs and hands back LRC with `[mm:ss.xx]` stamps.

Matching is the hard part, because YouTube titles are not track titles — they
carry "(Official Video)", featured artists and remix suffixes that LRCLIB has
never heard of. So we try the exact endpoint with a cleaned title first and fall
back to a fuzzy search, scoring candidates on title/artist agreement and on how
close their duration is to the track we are actually playing.

Answers are cached in data/lyrics.json so that replaying a song costs nothing.
Hits are kept for good; misses expire after a month, since LRCLIB's catalogue
grows one contributor at a time and today's gap is often filled by next month.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from urllib.parse import quote

import requests

from .config import DATA_DIR

CACHE = DATA_DIR / "lyrics.json"
MISS_TTL = 30 * 86400
API = "https://lrclib.net/api"
UA = {"User-Agent": "MusicXP/0.1 (personal use; https://github.com/)"}

# The furniture YouTube titles carry and real track titles don't.
_NOISE = re.compile(
    r"\s*[(\[][^)\]]*?(official|video|audio|lyric|visualizer|hd|4k|mv|m/v|"
    r"live|performance|version|explicit|remaster|colou?r\s*coded)[^)\]]*[)\]]",
    re.I)
_FEAT = re.compile(r"\s*[(\[]?\s*(feat\.?|ft\.?|featuring|with)\s+[^)\]]*[)\]]?\s*$", re.I)
_PAREN_TAIL = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*$")
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\bx\b|\bvs\.?\b|\bfeat\.?\b|\bft\.?\b|·)\s*", re.I)
# Some uploads separate the hundredths with a colon — [02:10:76] rather than
# [02:10.76]. Rejecting those reads a fully timed transcription as flat text.
_STAMP = re.compile(r"\[(\d+):(\d+)(?:[.:](\d+))?\]")

# Skipping quickly through a playlist fires one lookup per track; back-to-back
# requests get a 429 from LRCLIB, so space them out. Served from threads, hence
# the lock.
_gate = threading.Lock()
_last = 0.0


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _clean_title(title: str) -> str:
    t = _NOISE.sub("", title or "")
    t = _FEAT.sub("", t)
    return t.strip(" -–—") or (title or "")


def _lead_artist(artist: str) -> str:
    return _ARTIST_SPLIT.split(artist or "", 1)[0].strip() or (artist or "")


def _secs(mm: str, ss: str, frac: str) -> float:
    return int(mm) * 60 + int(ss) + (float("0." + frac) if frac else 0.0)


def parse_lrc(lrc: str) -> list[dict]:
    """LRC text -> [{t: seconds, line: str}], in time order.

    One stamp per line is the common case, but the format allows several on a
    shared line (a repeated chorus), so each stamp becomes its own entry.
    """
    out: list[dict] = []
    for raw in (lrc or "").splitlines():
        stamps = _STAMP.findall(raw)
        if not stamps:
            continue
        text = _STAMP.sub("", raw).strip()
        for mm, ss, frac in stamps:
            out.append({"t": _secs(mm, ss, frac), "line": text})
    out.sort(key=lambda x: x["t"])
    return out


def _score(hit: dict, want_t: str, want_a: str, dur: float) -> float:
    """How much a search hit looks like the track we're playing."""
    ht, ha = _fold(hit.get("trackName", "")), _fold(hit.get("artistName", ""))
    if not ht:
        return -1
    if ht == want_t:
        s = 2.0
    elif want_t and (want_t in ht or ht in want_t):
        s = 1.2
    else:
        return -1                     # a different song, whatever else matches
    if ha and want_a:
        if ha == want_a or ha in want_a or want_a in ha:
            s += 1.0
    # Duration is the tiebreaker that separates the single from the 8-minute
    # extended mix; ±3s is the same recording for our purposes.
    hd = hit.get("duration") or 0
    if dur and hd:
        gap = abs(hd - dur)
        s += 1.0 if gap <= 3 else 0.5 if gap <= 10 else -0.6
    return s


def _get(path: str, params: dict) -> tuple[bool, object]:
    """(reached_the_service, payload). 404 counts as reached — it means "no such
    track", which is an answer. A timeout or a 429 does not, and must not be
    mistaken for one, or a rate-limited moment gets cached as a permanent miss.
    """
    with _gate:
        global _last
        wait = 0.6 - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
    try:
        r = requests.get(f"{API}/{path}", params=params, headers=UA, timeout=12)
        if r.status_code == 404:
            return True, None
        if r.status_code != 200:
            return False, None
        return True, r.json()
    except Exception:
        return False, None


def _hit(h: dict) -> dict:
    return {"synced": parse_lrc(h.get("syncedLyrics") or ""),
            "plain": h.get("plainLyrics") or "",
            "source": f'{h.get("artistName","")} — {h.get("trackName","")}',
            "ok": True}


# What completeness is worth when two uploads both match the track. Small
# enough never to outvote title/artist agreement, big enough to separate a
# truncated transcription from a full one.
_COVER_W = 1.0


def _coverage(lines: list[dict], dur: float) -> float:
    """How far into the track the timed lines actually reach."""
    if not lines or not dur:
        return 0.0
    return min(1.0, lines[-1]["t"] / dur)


def _plain_of(h: dict) -> tuple[str, str]:
    return ((h.get("plainLyrics") or "").strip(),
            f'{h.get("artistName", "")} — {h.get("trackName", "")}')


def _fetch(artist: str, title: str, dur: float) -> dict:
    """Best synced lyrics for a track, falling back to untimed text.

    Timed lyrics are always preferred, but a song nobody has synced is still
    worth reading, so an untimed transcription is returned rather than nothing.
    """
    clean, lead = _clean_title(title), _lead_artist(artist)
    want_t, want_a = _fold(clean), _fold(lead)

    reached = False
    plain, plain_src = "", ""

    # The exact endpoint is cheapest and most accurate when the metadata is
    # good, but it answers with whichever upload it happens to hold — sometimes
    # one that stops early or skips a verse, which then drifts out of sync
    # halfway through. So treat it as a candidate and let search offer rivals.
    exact: list[dict] = []
    for t in (clean, _PAREN_TAIL.sub("", clean)):
        if not t:
            continue
        ok, hit = _get("get", {"track_name": t, "artist_name": lead,
                               **({"duration": int(dur)} if dur else {})})
        reached = reached or ok
        if ok and isinstance(hit, dict):
            exact.append(hit)

    ok, hits = _get("search", {"q": f"{lead} {clean}".strip()})
    reached = reached or ok
    found = exact + [h for h in (hits if isinstance(hits, list) else [])
                     if isinstance(h, dict)]

    best, best_s = None, 0.5
    for h in found:
        s = _score(h, want_t, want_a, dur)
        if s < 0:
            continue
        if h.get("syncedLyrics"):
            s += _COVER_W * _coverage(parse_lrc(h["syncedLyrics"]), dur)
            if s > best_s:
                best, best_s = h, s
        elif not plain:
            plain, plain_src = _plain_of(h)
    if best:
        return _hit(best)
    for h in exact:            # the scorer couldn't confirm it, but we asked for it
        if h.get("syncedLyrics"):
            return _hit(h)

    ne_ok, ne = _netease(lead, clean, dur, want_t, want_a)
    if ne and ne["synced"]:
        return ne
    if ne and ne["plain"] and not plain:
        plain, plain_src = ne["plain"], ne["source"]
    if not plain:
        for h in exact:
            if h.get("plainLyrics"):
                plain, plain_src = _plain_of(h)
                break
    if not plain:
        o_ok, plain, plain_src = _ovh(lead, clean)
        reached = reached or o_ok
    if plain:
        return {"synced": [], "plain": plain, "source": plain_src, "ok": True}
    return {"synced": [], "plain": "", "source": "", "ok": reached or ne_ok}


# ── untimed fallback ──────────────────────────────────────────────────────────
# When nobody has synced the song, lyrics.ovh still serves Deezer's plain text
# without a key, which is enough to read the whole song through.
def _ovh(lead: str, clean: str) -> tuple[bool, str, str]:
    try:
        r = requests.get(
            f"https://api.lyrics.ovh/v1/{quote(lead)}/{quote(clean)}",
            headers=UA, timeout=15)
        if r.status_code == 404:
            return True, "", ""
        if r.status_code != 200:
            return False, "", ""
        text = (r.json().get("lyrics") or "").strip()
        return True, text, (f"{lead} — {clean}" if text else "")
    except Exception:
        return False, "", ""


# ── NetEase fallback ──────────────────────────────────────────────────────────
# LRCLIB only has what a volunteer happened to upload, so recent releases fall
# through it. NetEase carries LRC for most of the same catalogue and needs no
# key either; it just answers slower and labels its credits in Chinese.
NE_API = "https://music.163.com/api"
NE_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
         "Referer": "https://music.163.com"}
# A title alone is not enough to accept a stranger's upload: 2.0 (title) plus at
# least one of a matching artist or a matching runtime.
NE_MIN = 3.0
_NE_CREDIT = re.compile(r"^\s*(作词|作曲|编曲|制作人|出品人?|监制|录音|混音|母带|"
                        r"吉他|贝斯|鼓|键盘|和声|发行|词|曲)\s*[:：]")
_NE_SECTION = re.compile(r"^\[[^\]]*\]\s*")

_ne_gate = threading.Lock()
_ne_last = 0.0


def _ne_get(path: str, params: dict) -> tuple[bool, object]:
    with _ne_gate:
        global _ne_last
        wait = 0.5 - (time.time() - _ne_last)
        if wait > 0:
            time.sleep(wait)
        _ne_last = time.time()
    try:
        r = requests.get(f"{NE_API}/{path}", params=params, headers=NE_UA, timeout=12)
        if r.status_code != 200:
            return False, None
        return True, r.json()
    except Exception:
        return False, None


def _ne_parse(lrc: str) -> list[dict]:
    """NetEase LRC -> our line list, minus the credit block and [Intro] markers."""
    out: list[dict] = []
    for raw in (lrc or "").splitlines():
        stamps = _STAMP.findall(raw)
        if not stamps:
            continue
        text = _NE_SECTION.sub("", _STAMP.sub("", raw).strip()).strip()
        if _NE_CREDIT.match(text):
            continue
        for mm, ss, frac in stamps:
            out.append({"t": _secs(mm, ss, frac), "line": text})
    out.sort(key=lambda x: x["t"])
    return out


def _netease(lead: str, clean: str, dur: float,
             want_t: str, want_a: str) -> tuple[bool, dict | None]:
    ok, res = _ne_get("search/get", {"s": f"{lead} {clean}".strip(),
                                     "type": 1, "limit": 10})
    if not ok:
        return False, None
    songs = ((res or {}).get("result") or {}).get("songs") or []
    best, best_s = None, NE_MIN - 0.01
    for s in songs:
        if not isinstance(s, dict):
            continue
        who = ", ".join(a.get("name", "") for a in (s.get("artists") or []))
        sc = _score({"trackName": s.get("name", ""), "artistName": who,
                     "duration": (s.get("duration") or 0) / 1000.0},
                    want_t, want_a, dur)
        if sc > best_s:
            best, best_s = s, sc
    if not best:
        return True, None
    ok, doc = _ne_get("song/lyric", {"id": best.get("id"), "lv": 1, "kv": 1, "tv": -1})
    if not ok:
        return False, None
    lrc = ((doc or {}).get("lrc") or {}).get("lyric") or ""
    lines = _ne_parse(lrc)
    if not lines and not lrc.strip():
        return True, None
    who = ", ".join(a.get("name", "") for a in (best.get("artists") or []))
    return True, {"synced": lines,
                  "plain": "" if lines else lrc.strip(),
                  "source": f'{who} — {best.get("name", "")}', "ok": True}


# ── every match, not just the winner ──────────────────────────────────────────
# _fetch scores the field and returns one entry, which is right for playback and
# useless when it picks wrong: a live take, an edit that skips a verse, a version
# whose stamps drift after the bridge. The studio needs to see the rivals it beat,
# so this runs the same searches and hands back all of them, already parsed.
def candidates(artist: str, title: str, dur: float = 0,
               limit: int = 10, ne_top: int = 3) -> list[dict]:
    clean, lead = _clean_title(title), _lead_artist(artist)
    want_t, want_a = _fold(clean), _fold(lead)

    hits: list[dict] = []
    for t in (clean, _PAREN_TAIL.sub("", clean)):
        if not t:
            continue
        ok, hit = _get("get", {"track_name": t, "artist_name": lead,
                               **({"duration": int(dur)} if dur else {})})
        if ok and isinstance(hit, dict):
            hits.append(hit)
    ok, found = _get("search", {"q": f"{lead} {clean}".strip()})
    if ok and isinstance(found, list):
        hits += [h for h in found if isinstance(h, dict)]

    out: list[dict] = []
    for h in hits:
        synced = parse_lrc(h.get("syncedLyrics") or "")
        plain = (h.get("plainLyrics") or "").strip()
        if not synced and not plain:
            continue
        out.append(_cand(synced, plain, "LRCLIB", h.get("artistName", ""),
                         h.get("trackName", ""), h.get("duration") or 0, dur,
                         _score(h, want_t, want_a, dur)))

    # NetEase charges a request per song for the words, so only the few that
    # already look like the right track are worth opening.
    ok, res = _ne_get("search/get", {"s": f"{lead} {clean}".strip(),
                                     "type": 1, "limit": 10})
    songs = ((res or {}).get("result") or {}).get("songs") or [] if ok else []
    ranked = []
    for s in songs:
        if not isinstance(s, dict):
            continue
        who = ", ".join(a.get("name", "") for a in (s.get("artists") or []))
        sc = _score({"trackName": s.get("name", ""), "artistName": who,
                     "duration": (s.get("duration") or 0) / 1000.0},
                    want_t, want_a, dur)
        if sc > 0:
            ranked.append((sc, who, s))
    ranked.sort(key=lambda r: -r[0])
    for sc, who, s in ranked[:ne_top]:
        ok, doc = _ne_get("song/lyric", {"id": s.get("id"), "lv": 1, "kv": 1,
                                         "tv": -1})
        if not ok:
            break
        lrc = ((doc or {}).get("lrc") or {}).get("lyric") or ""
        lines = _ne_parse(lrc)
        if not lines and not lrc.strip():
            continue
        out.append(_cand(lines, "" if lines else lrc.strip(), "NetEase", who,
                         s.get("name", ""), (s.get("duration") or 0) / 1000.0,
                         dur, sc))

    # Timed beats untimed, then the closer match, then the one that reaches
    # furthest into the song — a sync that stops at the second chorus is the
    # commonest way a "found" lyric is still wrong.
    out.sort(key=lambda c: (bool(c["synced"]), c["score"], c["coverage"]),
             reverse=True)
    # Both services carry the same upload several times over. What makes two
    # entries the same is the words, not the id — the exact and search endpoints
    # disagree about which id they quote for one set of lines. Deduped after the
    # sort, so the copy that survives is the best-scoring one.
    uniq: list[dict] = []
    seen: set = set()
    for c in out:
        sig = (len(c["synced"]), len(c["plain"]),
               c["synced"][0]["line"] if c["synced"] else c["plain"][:40])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(c)
    return uniq[:limit]


def _cand(synced: list[dict], plain: str, where: str, who: str, what: str,
          hd: float, dur: float, score: float) -> dict:
    return {"synced": synced, "plain": plain, "where": where,
            "artist": who, "title": what, "dur": round(hd or 0, 1),
            "source": f"{who} — {what}".strip(" —") or where,
            "coverage": round(_coverage(synced, dur), 3),
            "score": round(score, 2)}


# The cache is served from a threaded HTTP server, so load-modify-save runs
# concurrently. Without this lock two savers interleave and the loser's entries
# vanish; worse, a half-written file reads back as no cache at all.
_cache_lock = threading.RLock()
_mem: dict | None = None
_mem_stamp: tuple | None = None


def _stamp() -> tuple | None:
    try:
        st = CACHE.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _load() -> dict:
    """The cache, re-read only when the file changed underneath us.

    Re-parsing several megabytes of JSON on every lookup is what made playback
    feel heavy, and another process (the daily run) may still write the file, so
    the snapshot is trusted only as long as mtime and size agree.
    """
    global _mem, _mem_stamp
    with _cache_lock:
        now = _stamp()
        if _mem is not None and now == _mem_stamp:
            return _mem
        if now is None:
            _mem, _mem_stamp = {}, None
            return _mem
        try:
            loaded = json.loads(CACHE.read_text())
            if not isinstance(loaded, dict):
                raise ValueError("cache is not an object")
        except (OSError, json.JSONDecodeError, ValueError):
            # A damaged file must never read as an empty one: the next save
            # would then persist the emptiness and take every cached song with
            # it. Keep it for inspection and carry on with what we still hold.
            try:
                CACHE.replace(CACHE.with_suffix(".json.corrupt"))
            except OSError:
                pass
            _mem_stamp = _stamp()
            _mem = _mem if _mem is not None else {}
            return _mem
        _mem, _mem_stamp = loaded, now
        return _mem


def _save(cache: dict) -> None:
    global _mem, _mem_stamp
    with _cache_lock:
        DATA_DIR.mkdir(exist_ok=True)
        # A temp name of our own: a shared one is truncated under a second
        # writer, and the interleaved bytes get renamed over the real cache.
        tmp = CACHE.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(cache, ensure_ascii=False))
            tmp.replace(CACHE)
        finally:
            tmp.unlink(missing_ok=True)
        _mem, _mem_stamp = cache, _stamp()


def _put(key: str, entry: dict) -> dict:
    """Write one entry without letting a concurrent writer drop the rest.

    The store lock is shared with the rest of data/, which is heavier than this
    needs but keeps a single ordering: no chance of two locks taken in opposite
    orders somewhere down the line.
    """
    from .store import transaction
    with transaction():
        global _mem, _mem_stamp
        _mem = _mem_stamp = None      # another process may have written meanwhile
        cache = _load()
        cache[key] = entry
        _save(cache)
    return entry


def for_track(artist: str, title: str, dur: float = 0, key: str = "") -> dict:
    """Synced lyrics for one track, cached.

    A hit is kept forever — the words don't change. A miss is only kept for
    MISS_TTL, because LRCLIB is crowd-uploaded track by track: a song nobody had
    synced in March may well be there in April, and a permanent miss would mean
    we never look again.
    """
    ck = key or f"{_fold(artist)}|{_fold(title)}"
    cache = _load()
    old = cache.get(ck)
    if old is not None:
        if old.get("synced") or old.get("plain"):
            return old
        if time.time() - old.get("at", 0) < MISS_TTL:
            return old
    got = _fetch(artist, title, dur)
    if got["ok"]:                 # never cache a request that didn't land
        got["at"] = time.time()
        _put(ck, got)
    return got


if __name__ == "__main__":
    import sys
    a, t = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("Adele", "Rolling in the Deep")
    res = for_track(a, t)
    print(res["source"] or "no match", "—", len(res["synced"]), "synced lines")
    for line in res["synced"][:8]:
        print(f'  {line["t"]:7.2f}  {line["line"]}')
