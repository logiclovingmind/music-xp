"""Download a day's playlist as local, Mac-playable audio files.

Reads the most recent playlist from history.json (we store every track's video
id) and downloads each track's best audio stream from YouTube with yt-dlp.
Everything is saved as .m4a so it plays natively in Music/QuickTime/iPhone:
AAC sources are remuxed with zero quality loss; opus/webm sources are converted
at high VBR (~256k, transparent). Title/artist tags and cover art are embedded.
Files land in ~/Downloads/MusicXP/<date>/ as 'Artist - Title.m4a'.

Requires yt-dlp (in the venv) and ffmpeg (for tag/thumbnail embedding).

    python -m music_xp.download            # download the latest playlist
    python -m music_xp.download 2026-07-19 # a specific date
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

from . import local, store
from .config import DATA_DIR, load_config

DEST_ROOT = Path.home() / "Downloads" / "MusicXP"
# Manual "Get" downloads (arbitrary URLs pasted in the web UI) land here, one
# subfolder per playlist/channel, kept apart from the daily-playlist folders.
GET_ROOT = DEST_ROOT / "Get"
_OWNED_CACHE = DATA_DIR / "owned_index.json"
_DEST_CACHE = DATA_DIR / "dest_index.json"
# The offline library rarely changes; scanning its tags takes ~30s, so cache the
# index and only rebuild it once a day.
_OWNED_MAX_AGE = 24 * 3600
# Homebrew ffmpeg on Intel macOS. Passed explicitly so it's found even when the
# server is launched from the Desktop app (which has a minimal PATH).
FFMPEG_DIR = "/usr/local/bin"


def _safe(name: str) -> str:
    """Filesystem-safe 'Artist - Title', collapsed whitespace."""
    name = re.sub(r'[/\\:*?"<>|]+', " ", name or "").strip()
    return re.sub(r"\s+", " ", name) or "track"


def _offline_index() -> dict[str, set[str]]:
    """Cached title->artists index of the offline library (config dirs)."""
    dirs = load_config().get("local_music_dirs") or []
    if not dirs:
        return {}
    if _OWNED_CACHE.exists() and \
            time.time() - _OWNED_CACHE.stat().st_mtime < _OWNED_MAX_AGE:
        try:
            raw = json.loads(_OWNED_CACHE.read_text())
            return {t: set(a) for t, a in raw.items()}
        except (json.JSONDecodeError, ValueError):
            pass
    index = local.owned_index(dirs)
    DATA_DIR.mkdir(exist_ok=True)
    tmp = _OWNED_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({t: sorted(a) for t, a in index.items()},
                              ensure_ascii=False))
    tmp.replace(_OWNED_CACHE)
    return index


def _dest_index() -> dict[str, set[str]]:
    """title -> artists for prior MusicXP downloads, cached per file.

    Re-reading every file's tags cost ~14s per run and grew with each download.
    Entries are keyed by path+mtime, so only files new or changed since the last
    run are opened and a freshly downloaded track still counts immediately.
    """
    try:
        cache = json.loads(_DEST_CACHE.read_text())
    except (OSError, json.JSONDecodeError, ValueError):
        cache = {}

    fresh: dict[str, list[str]] = {}
    index: dict[str, set[str]] = {}
    for p in DEST_ROOT.rglob("*"):
        if not p.name.lower().endswith(local.AUDIO_EXT):
            continue
        try:
            key = f"{p}:{int(p.stat().st_mtime)}"
        except OSError:
            continue
        pair = cache.get(key)
        if pair is None:
            got = local._title_artist_from_tags(str(p)) \
                or local._title_artist_from_filename(p.name)
            if not got:
                continue
            pair = list(got)
        fresh[key] = pair
        nt, na = local._norm(pair[1]), local._norm(pair[0])
        if nt and na:
            index.setdefault(nt, set()).add(na)

    DATA_DIR.mkdir(exist_ok=True)
    tmp = _DEST_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fresh, ensure_ascii=False))
    tmp.replace(_DEST_CACHE)
    return index


def owned_index() -> dict[str, set[str]]:
    """Everything the user already has: offline library + prior MusicXP folders."""
    index = {t: set(a) for t, a in _offline_index().items()}
    if DEST_ROOT.exists():
        for title, artists in _dest_index().items():
            index.setdefault(title, set()).update(artists)
    return index


def playlist_for(date_str: str | None) -> tuple[str, list[dict]]:
    history = store.load_history()
    if not date_str:
        date_str = max((h.get("date", "") for h in history), default="")
    tracks = [h for h in history
              if h.get("date") == date_str and h.get("video_id")]
    return date_str, tracks


_PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")
# yt-dlp's message when a video is age-gated and no auth cookies are supplied.
_AGE_RE = re.compile(r"confirm your age|age.restricted", re.I)
# A dead media URL from the android_vr client — see url_download() for why.
_403_RE = re.compile(r"HTTP Error 403")


def _download_one(video_id: str, dest: Path, base: str,
                  on_pct=None) -> tuple[int, str]:
    """Download one video's audio. Returns (exit_code, combined_output)."""
    url = f"https://music.youtube.com/watch?v={video_id}"
    cmd = [
        sys.executable, "-m", "yt_dlp",
        # Prefer the native AAC stream: it remuxes to .m4a instantly, where
        # opus (which bestaudio picks for being a hair higher bitrate) costs a
        # full transcode — minutes per track on this machine, for a lossy
        # re-encode of an already-lossy source.
        "-f", "bestaudio[ext=m4a]/bestaudio",
        "--no-playlist",
        # .m4a everywhere: AAC sources remux losslessly, opus is converted at
        # best VBR (-q 0 ≈ 256k) so it stays transparent but plays on macOS.
        "-x", "--audio-format", "m4a", "--audio-quality", "0",
        "--embed-metadata",
        "--ffmpeg-location", FFMPEG_DIR,
        *_youtube_args(),
        "--newline", "--no-warnings",
        "-o", str(dest / (base + ".%(ext)s")),
        url,
    ]
    if on_pct is None:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    # Stream yt-dlp's per-line progress so callers can show a live bar.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    lines: list[str] = []
    for line in proc.stdout:
        lines.append(line)
        m = _PCT_RE.search(line)
        if m:
            try:
                on_pct(float(m.group(1)))
            except Exception:
                pass
    proc.wait()
    return proc.returncode, "".join(lines)


def _search_alternate_ids(query: str, limit: int = 6) -> list[str]:
    """YouTube search video ids for `query`, best matches first."""
    cmd = [sys.executable, "-m", "yt_dlp", f"ytsearch{limit}:{query}",
           "--flat-playlist", "--print", "%(id)s", "--no-warnings"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def _youtube_args() -> list[str]:
    """Point yt-dlp at Node and pin the one player client that works here.

    Node is needed to solve YouTube's JS challenges (yt-dlp only enables Deno by
    default, which isn't installed). Left to itself yt-dlp then prefers
    web_safari, whose media URLs 403 from this IP — the run saves only the cover
    art. android_vr used to be the way around that, but as of 2026-08 its URLs
    403 too, as do every other JS-free client (android_music, ios, tv_embedded,
    mweb) — and a bgutil PO token does not revive them. Only the web clients,
    which must solve a JS challenge, still get working URLs.

    That solve costs ~9s of node CPU per track and is the bulk of a download's
    time. It can't be avoided or batched (one solve per video either way), but
    the `tce` player JS is a smaller script than the default `main` and cuts the
    solve by roughly a third.
    """
    node = shutil.which("node")
    return ["--js-runtimes", f"node:{node}" if node else "node",
            "--extractor-args",
            "youtube:player_client=web_embedded,web_music;player_js_variant=tce"]


def url_download_cmd(url: str, mode: str = "audio",
                     height: int | None = None,
                     archive: Path | None = None) -> list[str]:
    """yt-dlp command to grab an arbitrary URL (single video OR full playlist).

    mode="audio" -> best audio as .m4a (like the daily downloads).
    mode="video" -> best video+audio up to `height`p, merged to .mp4.
    Playlists download every entry; --ignore-errors skips any single blocked or
    age-gated item instead of aborting the whole batch.
    `archive` records the ids that finished, so a retry pass re-runs only the
    ones that didn't.
    """
    out_tmpl = str(GET_ROOT / "%(playlist_title,channel,uploader|Music XP)s"
                   / "%(title).180s [%(id)s].%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--newline", "--no-warnings", "--ignore-errors",
        "--embed-metadata", "--embed-thumbnail",
        "--ffmpeg-location", FFMPEG_DIR,
        *_youtube_args(),
        # YouTube 403s this IP's android_vr media URLs once a run fetches too
        # many too fast, and the block then covers every video for a while.
        # Pacing a playlist keeps a normal run under that threshold.
        "--sleep-requests", "1",
        "--sleep-interval", "2", "--max-sleep-interval", "6",
        "-o", out_tmpl,
    ]
    if archive:
        cmd += ["--download-archive", str(archive)]
    if mode == "video":
        h = int(height or 1080)
        cmd += ["-f", f"bv*[height<={h}]+ba/b[height<={h}]/b",
                "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestaudio/best",
                "-x", "--audio-format", "m4a", "--audio-quality", "0",
                # YouTube art is 16:9; embedded as-is players letterbox it into
                # the square cover slot. Centre-crop to the short edge first so
                # the tag holds a real square, matching the daily downloads.
                "--convert-thumbnails", "jpg",
                # Art is always 16:9, so cropping to the height squares it.
                # Keep this expression comma-free: yt-dlp shlex-splits these
                # args and strips the quoting an if(a,b,c) form would need,
                # leaving ffmpeg to read the commas as filter separators.
                "--ppa", "ThumbnailsConvertor+ffmpeg_o:-c:v mjpeg "
                         "-vf crop=ih:ih"]
    cmd.append(url)
    return cmd


_ERR_RE = re.compile(r"^ERROR:", re.M)


# A handful of 403s is the usual per-item flakiness and is worth one retry;
# more than this means YouTube has blocked the IP outright, and retrying then
# only deepens the block.
_RETRY_ERR_LIMIT = 3


def url_download(url: str, mode: str = "audio",
                 height: int | None = None) -> Iterator[str]:
    """Run the Get download, retrying the odd failure, yielding output lines.

    Every player client here is SABR- or PO-token-gated except the one pinned in
    _youtube_args(), and its media URLs 403 once the IP has fetched too many. A
    dead URL can't be resumed, so a failed item is re-extracted from scratch —
    but only when few enough failed that it looks like a blip.
    """
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "done.txt"
        for attempt in (1, 2):
            if attempt > 1:
                yield "\nRe-extracting the items that failed…\n"
            errors = 0
            proc = subprocess.Popen(
                url_download_cmd(url, mode, height, archive),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            try:
                for line in proc.stdout:
                    if _ERR_RE.match(line):
                        errors += 1
                    yield line
                proc.wait()
            finally:
                if proc.poll() is None:
                    proc.terminate()
            if not errors:
                return
            if errors > _RETRY_ERR_LIMIT:
                yield (f"\n{errors} items failed. YouTube is rate-limiting this "
                       "IP — wait a few hours before running Get again.\n")
                return
        yield "\nSome items still failed. Run Get again later to pick them up.\n"


def normalize_volume(path: Path, target_mean: float = -11.0,
                     min_gain: float = 2.0) -> bool:
    """Boost unusually quiet tracks up to roughly the loudness of the rest.

    YT Music normalizes loudness while streaming, but downloads are the raw
    master — some (often indie/vocaloid) releases are mastered ~7 dB quieter.
    Measures mean volume; if it's more than `min_gain` below `target_mean`,
    re-encodes with that gain plus a limiter so boosted peaks can't clip.
    """
    ff = FFMPEG_DIR + "/ffmpeg"
    probe = subprocess.run(
        [ff, "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", probe.stderr)
    if not m:
        return False
    gain = target_mean - float(m.group(1))
    if gain < min_gain:
        return False

    tmp = path.with_suffix(".norm.m4a")
    r = subprocess.run(
        # -vn: drop any embedded cover stream (re-embedded after normalizing).
        [ff, "-y", "-i", str(path), "-vn",
         "-af", f"volume={gain:.1f}dB,alimiter=limit=0.97",
         "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(tmp)],
        capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(path)
    return True


def tag_file(path: Path, title: str, artist: str, album: str = "") -> None:
    """Write title/artist (and album) tags so Finder/Music show real metadata."""
    from mutagen.mp4 import MP4

    mp4 = MP4(path)
    if title:
        mp4["\xa9nam"] = [title]
    if artist:
        mp4["\xa9ART"] = [artist]
        mp4["aART"] = [artist]
    if album:
        mp4["\xa9alb"] = [album]
    mp4.save()


def embed_cover(path: Path, video_id: str) -> bool:
    """Embed the track's YouTube thumbnail as m4a cover art.

    Letterbox bars are trimmed and the result is center-cropped to a square,
    so YT Music's padded album art comes out clean.
    """
    import io

    import requests
    from mutagen.mp4 import MP4, MP4Cover
    from PIL import Image

    data = None
    for name in ("maxresdefault", "sddefault", "hqdefault"):
        r = requests.get(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg",
                         timeout=20)
        if r.status_code == 200 and len(r.content) > 1000:
            data = r.content
            break
    if not data:
        return False

    im = Image.open(io.BytesIO(data)).convert("RGB")
    bbox = im.convert("L").point(lambda v: 255 if v > 16 else 0).getbbox()
    # Trim near-black borders, but never so hard that real (dark) art vanishes.
    if bbox and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) >= 0.3 * im.width * im.height:
        im = im.crop(bbox)
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)

    mp4 = MP4(path)
    mp4["covr"] = [MP4Cover(buf.getvalue(), imageformat=MP4Cover.FORMAT_JPEG)]
    mp4.save()
    return True


def download(date_str: str | None = None,
             only_ids: set[str] | None = None,
             progress=None) -> Iterator[str]:
    """Download a playlist, yielding one human-readable progress line per step.

    only_ids limits the run to those video_ids (selective download from the UI).
    progress, if given, is called with structured event dicts (init / status /
    pct / done) so a live UI can render per-track progress bars.
    """
    def emit(ev: dict) -> None:
        if progress:
            try:
                progress(ev)
            except Exception:
                pass

    if only_ids:
        # Selective download: match anywhere in history (any date), not just
        # one day's playlist. Each file lands in its own date folder.
        seen: set[str] = set()
        tracks = []
        for t in store.load_history():
            v = t.get("video_id")
            if v and v in only_ids and v not in seen:
                seen.add(v)
                tracks.append(t)
    else:
        date_str, tracks = playlist_for(date_str)
    if not tracks:
        yield "No playlist found in history yet. Build one first, then download.\n"
        return

    total = len(tracks)
    emit({"type": "init", "total": total, "items": [
        {"video_id": t.get("video_id", ""), "title": t.get("title", ""),
         "artist": t.get("artist", ""), "status": "queued", "pct": 0}
        for t in tracks]})
    yield f"Downloading {total} tracks (best quality .m4a) -> {DEST_ROOT}\n"

    # Skip songs the user already owns (offline library or a prior MusicXP run),
    # matched by fuzzy title+artist so we don't re-download the same track.
    owned = owned_index()

    ok = 0
    last_dest = DEST_ROOT
    for i, t in enumerate(tracks, 1):
        idx = i - 1
        t_date = t.get("date") or date_str or "misc"
        dest = DEST_ROOT / t_date
        dest.mkdir(parents=True, exist_ok=True)
        last_dest = dest
        base = _safe(f"{t.get('artist', '')} - {t.get('title', '')}")
        if (dest / (base + ".m4a")).exists():
            emit({"type": "status", "index": idx, "status": "have", "pct": 100})
            yield f"[{i}/{total}] have: {base}\n"
            ok += 1
            continue
        if local.is_owned(owned, t.get("artist", ""), t.get("title", "")):
            emit({"type": "status", "index": idx, "status": "have", "pct": 100})
            yield f"[{i}/{total}] have (owned): {base}\n"
            ok += 1
            continue
        emit({"type": "status", "index": idx, "status": "downloading", "pct": 0})
        yield f"[{i}/{total}] {t.get('artist', '')} - {t.get('title', '')} ...\n"
        on_pct = lambda p, j=idx: emit({"type": "pct", "index": j, "pct": p})
        vid = t["video_id"]
        code, log = _download_one(vid, dest, base, on_pct=on_pct)
        for _ in range(2):
            if code == 0 or not _403_RE.search(log):
                break
            yield "    media URL expired; re-extracting...\n"
            code, log = _download_one(vid, dest, base, on_pct=on_pct)
        # YT Music's own pick is sometimes age-gated; without auth cookies
        # yt-dlp can't fetch it. Fall back to a non-gated upload of the same
        # track found via search, so the download still succeeds headless.
        if code != 0 and _AGE_RE.search(log):
            yield "    age-restricted; searching for an alternate upload...\n"
            query = f"{t.get('artist_display') or t.get('artist', '')} " \
                    f"{t.get('title', '')}".strip()
            for alt in _search_alternate_ids(query):
                if alt == t["video_id"]:
                    continue
                code, log = _download_one(alt, dest, base, on_pct=on_pct)
                if code == 0:
                    vid = alt
                    yield f"    using alternate upload {alt}\n"
                    break
        if code == 0:
            emit({"type": "status", "index": idx, "status": "tagging", "pct": 100})
            f = dest / (base + ".m4a")
            try:
                normalize_volume(f)
                tag_file(f, t.get("title", ""),
                         t.get("artist_display") or t.get("artist", ""),
                         f"Music XP {t_date}")
                embed_cover(f, vid)
            except Exception:
                pass
            emit({"type": "status", "index": idx, "status": "done", "pct": 100})
            ok += 1
        else:
            emit({"type": "status", "index": idx, "status": "failed", "pct": 0})
            yield f"    ! failed (yt-dlp exit {code})\n"

    emit({"type": "done"})
    yield f"\nDone: {ok}/{total} saved in {last_dest}\n"


def main() -> None:
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    for line in download(date_arg):
        sys.stdout.write(line)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
