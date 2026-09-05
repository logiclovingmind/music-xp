"""Offline library: index local music folders and stream them to the dashboard.

Folders pasted into the Library tab are walked for audio files. Tags are read
with mutagen (falling back to 'Artist - Title' filenames) and cached in
data/library.json, so a rescan only re-reads files whose size or mtime changed.

The browser only ever asks for a track id, never a path: playback and cover art
are resolved through the index, so nothing outside your chosen folders can be
read through the web server.

    python -m music_xp.library            # scan the configured roots
    python -m music_xp.library ~/Music    # add a folder, then scan
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Callable

from .config import DATA_DIR

LIB_PATH = DATA_DIR / "library.json"
PL_PATH = DATA_DIR / "playlists.json"
ART_DIR = DATA_DIR / "artcache"

AUDIO_EXT = (".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav", ".aiff", ".aif",
             ".ogg", ".oga", ".opus", ".wma", ".alac")

# Safari plays mp3/m4a/wav/flac natively; ogg/opus/wma only work in Chrome or
# Firefox. We serve them all and let the browser decide.
MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4",
        ".aac": "audio/aac", ".flac": "audio/flac", ".wav": "audio/wav",
        ".aiff": "audio/aiff", ".aif": "audio/aiff", ".ogg": "audio/ogg",
        ".oga": "audio/ogg", ".opus": "audio/ogg", ".wma": "audio/x-ms-wma",
        ".alac": "audio/mp4"}

COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork")
COVER_EXT = (".jpg", ".jpeg", ".png", ".webp")
IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp"}

_BRACKETS = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
_LEADING_TRACK = re.compile(r"^\s*\d{1,3}\s*[\.\)\-_]\s*")

_lock = threading.RLock()
_index: dict | None = None
_by_id: dict[str, dict] = {}


# ── index storage ─────────────────────────────────────────────────────────────
def _blank() -> dict:
    return {"roots": [], "tracks": [], "scanned": 0.0}


def _default_roots() -> list[str]:
    """Seed with the folder the daily downloader writes to, if it exists."""
    d = Path.home() / "Downloads" / "MusicXP"
    return [str(d)] if d.is_dir() else []


def load() -> dict:
    global _index
    with _lock:
        if _index is None:
            try:
                with open(LIB_PATH) as f:
                    _index = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                _index = _blank()
                _index["roots"] = _default_roots()
            _index.setdefault("roots", [])
            _index.setdefault("tracks", [])
            _index.setdefault("scanned", 0.0)
            _reindex()
        return _index


def _reindex() -> None:
    global _by_id
    _by_id = {t["id"]: t for t in (_index or {}).get("tracks", [])}


def save() -> None:
    with _lock:
        DATA_DIR.mkdir(exist_ok=True)
        tmp = LIB_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(_index, f)
        tmp.replace(LIB_PATH)


def track(tid: str) -> dict | None:
    load()
    return _by_id.get(tid)


# ── roots ─────────────────────────────────────────────────────────────────────
def add_root(raw: str) -> dict:
    raw = (raw or "").strip().strip('"').strip("'")
    if raw.startswith("file://"):
        from urllib.parse import unquote, urlparse
        raw = unquote(urlparse(raw).path)
    if not raw:
        return {"error": "paste a folder path"}
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return {"error": "could not read that path"}
    if not path.is_dir():
        return {"error": f"not a folder: {path}"}
    idx = load()
    with _lock:
        if str(path) in idx["roots"]:
            return {"error": "that folder is already in your library"}
        # Adding a subfolder of an existing root would index everything twice.
        for r in idx["roots"]:
            if str(path) == r or str(path).startswith(r.rstrip("/") + "/"):
                return {"error": f"already covered by {r}"}
        idx["roots"] = [r for r in idx["roots"]
                        if not r.startswith(str(path).rstrip("/") + "/")]
        idx["roots"].append(str(path))
        save()
    return {"ok": True, "roots": idx["roots"]}


def remove_root(raw: str) -> dict:
    idx = load()
    with _lock:
        if raw not in idx["roots"]:
            return {"error": "not in your library"}
        idx["roots"].remove(raw)
        keep = raw.rstrip("/") + "/"
        idx["tracks"] = [t for t in idx["tracks"] if not t["path"].startswith(keep)]
        _reindex()
        save()
    return {"ok": True, "roots": idx["roots"]}


# ── tags ──────────────────────────────────────────────────────────────────────
def _first(tags, *keys) -> str:
    for k in keys:
        v = tags.get(k)
        if v:
            s = str(v[0] if isinstance(v, list) else v).strip()
            if s:
                return s
    return ""


def _num(raw: str) -> int:
    m = re.match(r"\s*(\d+)", raw or "")
    return int(m.group(1)) if m else 0


def _from_filename(stem: str) -> tuple[str, str]:
    s = _LEADING_TRACK.sub("", _BRACKETS.sub("", stem)).strip()
    if " - " in s:
        a, t = s.split(" - ", 1)
        return a.strip(), t.strip()
    return "", s


def _read(path: Path, root: str) -> dict:
    st = path.stat()
    artist = title = album = albumartist = genre = year = ""
    trackno = discno = 0
    dur = 0.0
    try:
        import mutagen
        f = mutagen.File(str(path), easy=True)
        if f is not None:
            dur = float(getattr(f.info, "length", 0) or 0)
            tags = f.tags or {}
            title = _first(tags, "title")
            artist = _first(tags, "artist")
            album = _first(tags, "album")
            albumartist = _first(tags, "albumartist", "album artist", "performer")
            genre = _first(tags, "genre")
            year = _first(tags, "date", "originaldate", "year")[:4]
            trackno = _num(_first(tags, "tracknumber", "track"))
            discno = _num(_first(tags, "discnumber", "disc"))
    except Exception:
        pass
    if not title or not artist:
        fa, ft = _from_filename(path.stem)
        title = title or ft or path.stem
        artist = artist or fa
    return {"id": hashlib.sha1(str(path).encode()).hexdigest()[:16],
            "path": str(path), "root": root, "title": title,
            "artist": artist or "Unknown artist",
            "album": album or "", "albumartist": albumartist or artist or "",
            "track": trackno, "disc": discno, "year": year, "genre": genre,
            "dur": round(dur, 1), "size": st.st_size, "mtime": int(st.st_mtime),
            "ext": path.suffix.lower(),
            "folder": path.parent.name}


# ── scanning ──────────────────────────────────────────────────────────────────
def scan(progress: Callable[[dict], None] | None = None) -> dict:
    """Walk every root and rebuild the index, reusing unchanged entries."""
    idx = load()
    roots = list(idx["roots"])
    old = {t["path"]: t for t in idx["tracks"]}

    files: list[tuple[Path, str]] = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                if name.lower().endswith(AUDIO_EXT):
                    files.append((Path(dirpath) / name, root))
    if progress:
        progress({"total": len(files), "done": 0})

    tracks: list[dict] = []
    for i, (path, root) in enumerate(files):
        prev = old.get(str(path))
        try:
            st = path.stat()
            if prev and prev.get("size") == st.st_size \
                    and prev.get("mtime") == int(st.st_mtime):
                prev["root"] = root
                tracks.append(prev)
            else:
                tracks.append(_read(path, root))
        except OSError:
            continue
        if progress and (i % 25 == 0 or i == len(files) - 1):
            progress({"total": len(files), "done": i + 1})

    tracks.sort(key=lambda t: (t["albumartist"].lower(), t["album"].lower(),
                               t["disc"], t["track"], t["title"].lower()))
    import time
    with _lock:
        idx["tracks"] = tracks
        idx["scanned"] = time.time()
        _reindex()
        save()
    return {"count": len(tracks), "roots": roots, "scanned": idx["scanned"]}


# ── cover art ─────────────────────────────────────────────────────────────────
def _cover_key(t: dict) -> str:
    # One cached image per album, so a 15-track album isn't stored 15 times.
    if t["album"]:
        raw = (t["albumartist"] or t["artist"]).lower() + "\u0000" + t["album"].lower()
    else:
        raw = t["path"]
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _embedded(path: Path) -> tuple[bytes, str] | None:
    try:
        import mutagen
        f = mutagen.File(str(path))
    except Exception:
        return None
    if f is None:
        return None
    tags = getattr(f, "tags", None)
    try:
        pics = getattr(f, "pictures", None)          # FLAC
        if pics:
            return pics[0].data, pics[0].mime or "image/jpeg"
        if tags is not None:
            if hasattr(tags, "getall"):              # ID3
                apic = tags.getall("APIC")
                if apic:
                    return apic[0].data, apic[0].mime or "image/jpeg"
            covr = tags.get("covr")                  # MP4
            if covr:
                fmt = getattr(covr[0], "imageformat", 13)
                return bytes(covr[0]), "image/png" if fmt == 14 else "image/jpeg"
            block = tags.get("metadata_block_picture")  # Ogg Vorbis/Opus
            if block:
                from mutagen.flac import Picture
                pic = Picture(base64.b64decode(block[0]))
                return pic.data, pic.mime or "image/jpeg"
    except Exception:
        return None
    return None


def _folder_image(path: Path) -> tuple[bytes, str] | None:
    try:
        entries = sorted(path.parent.iterdir())
    except OSError:
        return None
    named = [p for p in entries if p.suffix.lower() in COVER_EXT
             and p.stem.lower().replace(" ", "").replace("_", "") in COVER_NAMES]
    other = [p for p in entries if p.suffix.lower() in COVER_EXT]
    for p in named + other:
        try:
            if p.stat().st_size <= 8_000_000:
                return p.read_bytes(), IMG_MIME.get(p.suffix.lower(), "image/jpeg")
        except OSError:
            continue
    return None


def cover(tid: str) -> tuple[bytes, str] | None:
    t = track(tid)
    if not t:
        return None
    ART_DIR.mkdir(parents=True, exist_ok=True)
    key = _cover_key(t)
    miss = ART_DIR / (key + ".none")
    if miss.exists():
        return None
    for ext, ctype in ((".jpg", "image/jpeg"), (".png", "image/png"),
                       (".webp", "image/webp")):
        hit = ART_DIR / (key + ext)
        if hit.is_file():
            return hit.read_bytes(), ctype

    path = Path(t["path"])
    got = _embedded(path) or _folder_image(path)
    if not got:
        miss.touch()
        return None
    data, ctype = got
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(ctype, ".jpg")
    try:
        (ART_DIR / (key + ext)).write_bytes(data)
    except OSError:
        pass
    return data, ctype


def art_rev() -> int:
    """Changes whenever a cached cover is added or replaced.

    Covers are served with a week-long max-age, so a swapped image would sit
    invisible behind the browser cache. The dashboard hangs this number off the
    cover URL, which makes a replacement a different URL and therefore a refetch.
    """
    try:
        return int(ART_DIR.stat().st_mtime)
    except OSError:
        return 0


def mime_for(suffix: str) -> str:
    return MIME.get(suffix.lower(), "application/octet-stream")


# ── playlists ─────────────────────────────────────────────────────────────────
# Stored apart from the index so a rescan never touches them. A playlist is just
# an ordered list of track ids; ids that no longer resolve are skipped on read
# rather than deleted, so an unplugged drive doesn't wipe the playlist.
_pl: dict | None = None


def _pl_load() -> dict:
    global _pl
    with _lock:
        if _pl is None:
            try:
                with open(PL_PATH) as f:
                    _pl = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError, ValueError):
                _pl = {"playlists": []}
            _pl.setdefault("playlists", [])
        return _pl


def _pl_save() -> None:
    with _lock:
        DATA_DIR.mkdir(exist_ok=True)
        tmp = PL_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(_pl, f)
        tmp.replace(PL_PATH)


def _pl_find(pid: str) -> dict | None:
    return next((p for p in _pl_load()["playlists"] if p["id"] == pid), None)


def playlists() -> list[dict]:
    """Playlists with their track ids resolved against the current index."""
    load()
    out = []
    for p in _pl_load()["playlists"]:
        ids = [i for i in p["tracks"] if i in _by_id]
        out.append({"id": p["id"], "name": p["name"], "created": p.get("created", 0),
                    "tracks": ids, "missing": len(p["tracks"]) - len(ids)})
    return out


def pl_create(name: str) -> dict:
    name = (name or "").strip()[:80]
    if not name:
        return {"error": "name your playlist"}
    import time
    with _lock:
        pl = _pl_load()
        if any(p["name"].lower() == name.lower() for p in pl["playlists"]):
            return {"error": "you already have a playlist with that name"}
        pid = hashlib.sha1(f"{name}\u0000{time.time()}".encode()).hexdigest()[:12]
        pl["playlists"].append({"id": pid, "name": name, "created": time.time(),
                                "tracks": []})
        _pl_save()
    return {"ok": True, "id": pid}


def pl_rename(pid: str, name: str) -> dict:
    name = (name or "").strip()[:80]
    if not name:
        return {"error": "name your playlist"}
    with _lock:
        p = _pl_find(pid)
        if not p:
            return {"error": "no such playlist"}
        p["name"] = name
        _pl_save()
    return {"ok": True}


def pl_delete(pid: str) -> dict:
    with _lock:
        pl = _pl_load()
        n = len(pl["playlists"])
        pl["playlists"] = [p for p in pl["playlists"] if p["id"] != pid]
        if len(pl["playlists"]) == n:
            return {"error": "no such playlist"}
        _pl_save()
    return {"ok": True}


def pl_add(pid: str, tids: list[str]) -> dict:
    load()
    with _lock:
        p = _pl_find(pid)
        if not p:
            return {"error": "no such playlist"}
        have = set(p["tracks"])
        added = 0
        for t in tids:
            if t in _by_id and t not in have:
                p["tracks"].append(t)
                have.add(t)
                added += 1
        _pl_save()
    return {"ok": True, "added": added, "total": len(p["tracks"])}


def pl_remove(pid: str, tid: str) -> dict:
    with _lock:
        p = _pl_find(pid)
        if not p:
            return {"error": "no such playlist"}
        p["tracks"] = [t for t in p["tracks"] if t != tid]
        _pl_save()
    return {"ok": True}


def pl_reorder(pid: str, tids: list[str]) -> dict:
    with _lock:
        p = _pl_find(pid)
        if not p:
            return {"error": "no such playlist"}
        keep = [t for t in tids if t in set(p["tracks"])]
        # Ids the browser didn't send (unresolvable ones it never saw) stay put.
        p["tracks"] = keep + [t for t in p["tracks"] if t not in set(keep)]
        _pl_save()
    return {"ok": True}


def summary() -> dict:
    idx = load()
    return {"roots": idx["roots"], "scanned": idx["scanned"],
            "tracks": idx["tracks"]}


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        r = add_root(arg)
        print(r.get("error") or f"added {arg}")
    res = scan(lambda p: print(f"\r  {p['done']}/{p['total']}", end="", flush=True))
    print(f"\nindexed {res['count']} tracks from {len(res['roots'])} folder(s)")
