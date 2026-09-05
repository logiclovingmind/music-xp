"""Resolve a representative photo for an artist, for the dashboard hero wall.

Album/song covers repeat when a day has many songs by the same artist, so the
hero shows one photo per *artist* instead. Sources are headless-friendly (no
login, no cookies): Fanart.tv, YT Music, TheAudioDB, then Wikipedia's REST
summary. Every candidate is run through macOS Vision face detection and the
first one showing a face wins — plenty of these sources hand back channel
banners, wordmarks and album art, which look like stock graphics on the wall.
Results — including misses, stored as "" — are cached in
data/artist_images.json so we resolve each artist at most once.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from urllib.parse import quote

import requests

from . import musicbrainz
from .config import DATA_DIR, _load_dotenv

# Collaboration separators — pick data joins artists like "Hòa Minzy, RHYDER" or
# "Gran Error x ANTONIA", none of which match a photo source as one string. We
# split on these and look each part up, primary (first) artist first.
_SPLIT = re.compile(r"\s*(?:,|&|\+|/|×|;| x | vs\.? | feat\.? | ft\.? "
                    r"| featuring )\s*", re.IGNORECASE)


def _candidates(artist: str) -> list[str]:
    parts = [p.strip() for p in _SPLIT.split(artist) if p.strip()]
    cands: list[str] = []
    for name in [artist, *parts]:      # try the full string, then each artist
        if name and name.lower() not in {c.lower() for c in cands}:
            cands.append(name)
    return cands

CACHE = DATA_DIR / "artist_images_faces.json"  # pre-face-check picks are stale
THUMBS = DATA_DIR / "thumbs"
_AUDIODB = "https://www.theaudiodb.com/api/v1/json/2/search.php"
_WIKI = "https://en.wikipedia.org/api/rest_v1/page/summary/"
_FANART = "https://webservice.fanart.tv/v3/music/"
_UA = {"User-Agent": "MusicXP/1.0 (local dashboard; artist art)"}


def _fanart_key() -> str:
    _load_dotenv()
    return os.environ.get("FANART_API_KEY", "")


def _from_fanart(artist: str) -> list[str]:
    """Full-HD artist photos from Fanart.tv (1920x1080 backgrounds).

    Fanart.tv is keyed by MusicBrainz ID, so we resolve the MBID first. It has
    the highest-res, hand-curated photos but only covers well-known artists —
    misses fall through to the other sources. Several entries per field are
    returned: the first is often a logo or a stage-lights shot with no face.
    """
    key = _fanart_key()
    if not key:
        return []
    mbid = musicbrainz.mbid_for_artist(artist)
    if not mbid:
        return []
    try:
        r = requests.get(_FANART + mbid, params={"api_key": key}, timeout=15)
        if r.status_code != 200:
            return []
        d = r.json() or {}
    except (requests.RequestException, ValueError):
        return []
    out = []
    for field in ("artistbackground", "artistthumb"):  # 1920x1080, then square
        for item in (d.get(field) or [])[:4]:
            url = item.get("url")
            if url:
                out.append(url)
    return out


def _load() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    tmp.replace(CACHE)


_ytm = None


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z\u00c0-\uffff]+", "", s.casefold())


def _upsize(url: str, w: int, h: int) -> str:
    """Googleusercontent art URLs carry the requested size in the path; asking
    for a bigger size returns a genuinely higher-res image, not an upscale."""
    return re.sub(r"=w\d+-h\d+[^/]*$", f"=w{w}-h{h}-l90-rj", url)


def _from_ytmusic(artist: str) -> list[str]:
    """Hi-res artist photos from YT Music — current photos, never cover art.

    The square avatar comes first: it is the artist's profile picture, usually
    a portrait. The artist page's wide banner is bigger (up to 2880x1200) but
    is frequently a wordmark, so it is only a fallback. We match by name first
    so an obscure query can't return the wrong artist.
    """
    global _ytm
    try:
        if _ytm is None:
            from ytmusicapi import YTMusic
            _ytm = YTMusic()
        results = _ytm.search(artist, filter="artists", limit=3)
    except Exception:
        return []
    want = _norm(artist)
    out = []
    for a in results or []:
        got = _norm(a.get("artist") or "")
        if not got or (want not in got and got not in want):
            continue
        thumbs = a.get("thumbnails") or []
        if thumbs:
            out.append(_upsize(thumbs[-1].get("url") or "", 1080, 1080))
        bid = a.get("browseId")
        if bid:
            try:
                thumbs = _ytm.get_artist(bid).get("thumbnails") or []
                if thumbs:
                    out.append(_upsize(thumbs[-1].get("url") or "", 2880, 1200))
            except Exception:
                pass
    return [u for u in out if u]


def _from_audiodb(artist: str) -> list[str]:
    try:
        r = requests.get(_AUDIODB, params={"s": artist}, timeout=10)
        if r.status_code != 200:
            return []
        arts = (r.json() or {}).get("artists") or []
    except (requests.RequestException, ValueError):
        return []
    if not arts:
        return []
    a = arts[0]
    keys = ("strArtistThumb", "strArtistFanart", "strArtistFanart2",
            "strArtistClearart")
    return [a[k] for k in keys if a.get(k)]


def _from_wikipedia(artist: str) -> list[str]:
    try:
        r = requests.get(_WIKI + quote(artist), headers=_UA, timeout=10)
        if r.status_code != 200:
            return []
        d = r.json() or {}
    except (requests.RequestException, ValueError):
        return []
    if d.get("type") == "disambiguation":
        return []
    url = ((d.get("originalimage") or {}).get("source")
           or (d.get("thumbnail") or {}).get("source") or "")
    return [url] if url else []


def _has_face(url: str) -> bool:
    """True when the image at `url` shows at least one human face.

    Uses the macOS Vision framework, which is on-device and needs no model
    download. Anything Vision can't read (SVG, a dead link) counts as no face.
    """
    try:
        r = requests.get(url, headers=_UA, timeout=15)
        if r.status_code != 200 or len(r.content) > 12_000_000:
            return False
        import Vision
        from Foundation import NSData
        data = NSData.dataWithBytes_length_(r.content, len(r.content))
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
            data, None)
        req = Vision.VNDetectFaceRectanglesRequest.alloc().init()
        ok, _err = handler.performRequests_error_([req], None)
        return bool(ok and (req.results() or []))
    except Exception:
        return False


def image_for(artist: str, cache: dict | None = None) -> str:
    """Photo URL for one artist ("" if none). Cache-first; fetches on a miss.

    Every source is asked for candidates up front, then they are face-checked in
    priority order — a wordmark banner from a strong source loses to a portrait
    from a weaker one, which is the whole point of the wall.
    """
    artist = (artist or "").strip()
    if not artist:
        return ""
    owns = cache is None
    if owns:
        cache = _load()
    key = artist.lower()
    if key in cache:
        return cache[key]
    cands = _candidates(artist)
    urls: list[str] = []
    for source in (_from_fanart, _from_ytmusic, _from_audiodb):
        for name in cands:
            urls += [u for u in source(name) if u not in urls]
            if urls:
                break                  # a hit on the full credit beats a member
    wiki = _from_wikipedia(cands[-1] if len(cands) == 1 else cands[1])
    urls += [u for u in wiki if u not in urls]
    url = next((u for u in urls if _has_face(u)), "")
    cache[key] = url
    time.sleep(0.25)  # be polite to the free endpoints (only on a real fetch)
    if owns:
        _save(cache)
    return url


def thumb(url: str, size: int = 240) -> bytes:
    """A square, cover-cropped JPEG of `url`, cached on disk.

    The hero draws ~100 tiles from full-HD sources; decoding those at full size
    stalls the wall tile by tile on a slow machine. Pre-shrinking them once
    means a refresh serves a few KB each, same-origin, straight from cache.
    """
    if not url:
        return b""
    path = THUMBS / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}_{size}.jpg"
    if path.exists():
        return path.read_bytes()
    from io import BytesIO

    from PIL import Image, ImageOps
    r = requests.get(url, headers=_UA, timeout=20)
    r.raise_for_status()
    im = ImageOps.fit(Image.open(BytesIO(r.content)).convert("RGB"),
                      (size, size), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, "JPEG", quality=82, optimize=True)
    THUMBS.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())
    return buf.getvalue()


def is_known(url: str) -> bool:
    """Only URLs we resolved ourselves may be fetched through the thumbnailer."""
    return bool(url) and url in set(_load().values())


def images_for_artists(names: list[str]) -> dict:
    """Resolve many artists in one pass with a single cache read/write."""
    cache = _load()
    before = dict(cache)
    out: dict[str, str] = {}
    for name in names:
        out[name] = image_for(name, cache)
    if cache != before:
        _save(cache)
    for url in set(out.values()):      # build the tiles before anyone asks
        try:
            thumb(url)
        except Exception:
            pass
    return out
