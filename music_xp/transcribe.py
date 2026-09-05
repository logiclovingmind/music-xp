"""Last-resort lyrics: transcribe the song's own audio with whisper.cpp.

Some tracks have no lyrics anywhere. LRCLIB, NetEase, Kugou and lyrics.ovh are
all crowd- or catalogue-fed, so a brand-new or niche release can be catalogued
with zero words attached, and the sites that *do* have it (Genius, AZLyrics)
refuse headless requests outright. For those the only remaining source is the
recording itself.

whisper.cpp runs locally, needs no key and emits LRC, so what comes back is
timestamped rather than a flat block of text. It is slow — around twenty minutes
for a three-minute song on this machine, which has no AVX2 — so it never runs
inline with a request. Tracks are queued one at a time and the result is written
into the normal lyrics cache, where it looks like any other hit and is never
recomputed.

Accuracy is lower than a human transcription: Whisper is trained on speech, so
sung vowels, ad-libs and heavy production cost it words. It is used only where
the alternative is nothing at all.
"""
from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import local

MODEL = Path.home() / ".cache" / "whisper" / "ggml-small.bin"
# Segment cap in characters, split on word boundaries: whisper's own segments run
# ten to fifteen seconds, which is several sung lines held on screen at once.
MAX_LINE = 45

# Whisper was trained on captioned video, so it writes like a caption track: it
# brackets sung lines in ♪, narrates instrumental passages as stage directions,
# and fills silence with sign-off phrases from the videos it learned on. None of
# that is lyrics.
_NOTE = re.compile(r"^[♪♫\s]+|[♪♫\s]+$")
_STAGE = re.compile(r"^[(\[][^)\]]*[)\]]$")
_JUNK = re.compile(
    r"^\W*(you|thanks?( for watching)?[.!]?|thank you[.!]?|bye[.!]?|"
    r"subtitles? by.*|subs? by.*|amara\.org.*)\W*$", re.I)


def available() -> bool:
    return bool(shutil.which("whisper-cli")) and MODEL.exists()


def _find_audio(artist: str, title: str) -> Path | None:
    """A downloaded copy of the track, if we already have one.

    The artist has to agree, not just the title. Two different songs can share a
    name — Skeletron's "Nani" and Saweetie's "NANi" both sit in the download
    folder — and transcribing the wrong one would fill the window with confident,
    completely wrong words. Better to find nothing.
    """
    from .download import DEST_ROOT
    want_t, want_a = local._norm(title), local._norm(artist)
    if not want_t or not want_a or not DEST_ROOT.exists():
        return None
    for p in DEST_ROOT.rglob("*"):
        if not p.name.lower().endswith(local.AUDIO_EXT):
            continue
        got = local._title_artist_from_tags(str(p)) \
            or local._title_artist_from_filename(p.name)
        if not got or local._norm(got[1]) != want_t:
            continue
        # One side often carries the featured artists and the other doesn't, so
        # containment either way counts as agreement.
        got_a = local._norm(got[0])
        if got_a and (got_a in want_a or want_a in got_a):
            return p
    return None


def _grab_audio(vid: str, into: Path) -> Path | None:
    """Pull the audio down when the track isn't on disk (a streamed pick)."""
    from .download import _youtube_args
    out = into / "src.%(ext)s"
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "bestaudio[ext=m4a]/bestaudio",
           "--no-playlist", "-o", str(out), *_youtube_args(),
           f"https://www.youtube.com/watch?v={vid}"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=600)
    except (subprocess.SubprocessError, OSError):
        return None
    got = sorted(into.glob("src.*"))
    return got[0] if got else None


def _to_wav(src: Path, dst: Path) -> bool:
    """whisper.cpp only reads 16 kHz mono PCM."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
            capture_output=True, timeout=300)
        return r.returncode == 0 and dst.exists()
    except (subprocess.SubprocessError, OSError):
        return False


def _whisper(wav: Path, stem: Path) -> str:
    try:
        subprocess.run(
            # -l auto matters: left alone whisper.cpp assumes English rather than
            # detecting, and an Arabic or Hindi track then decodes to a column of
            # "[MUSIC]" — which reads as "this song has no words" when in fact it
            # was never listened to in the right language.
            ["whisper-cli", "-m", str(MODEL), "-f", str(wav), "-t", "4",
             "-l", "auto", "-ml", str(MAX_LINE), "-sow", "-olrc", "-of", str(stem)],
            capture_output=True, timeout=7200)
    except (subprocess.SubprocessError, OSError):
        return ""
    lrc = stem.with_suffix(".lrc")
    return lrc.read_text(errors="replace") if lrc.exists() else ""


def _clean(lines: list[dict]) -> list[dict]:
    """Strip caption furniture and drop the filler over instrumental passages."""
    out = []
    for l in lines:
        text = _NOTE.sub("", l["line"]).strip()
        if not text or _STAGE.match(text) or _JUNK.match(text):
            continue
        out.append({"t": l["t"], "line": text})
    return out


def transcribe_file(path: Path) -> dict:
    """Timed lyrics read straight off an audio file. Empty dict if it failed."""
    from .lyrics import parse_lrc
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wav = tmp / "a.wav"
        if not _to_wav(path, wav):
            return {}
        lines = _clean(parse_lrc(_whisper(wav, tmp / "a")))
    if not lines:
        return {}
    # The segments are kept even after a sheet is aligned over them: they are the
    # only clock we have for this track, and re-earning it costs another twenty
    # minutes the next time someone pastes corrected words.
    return {"synced": lines, "plain": "\n".join(l["line"] for l in lines),
            "source": "transcribed from audio", "transcribed": True,
            "whisper": lines}


# ── queue ─────────────────────────────────────────────────────────────────────
# One track at a time: two whisper runs on two cores would only make both slow,
# and skipping through a playlist should not spawn a job per track.
_q: queue.Queue = queue.Queue()
_state: dict[str, str] = {}          # cache key -> queued | running | done | failed
_lock = threading.Lock()
_worker: threading.Thread | None = None


def apply_sheet(entry: dict, sheet: str, dur: float = 0,
                source: str = "") -> dict:
    """Time `sheet` against a transcription already held in `entry`.

    Empty dict if there is no clock yet or nothing lined up, so a sheet is never
    silently swapped in for a worse result than what is already on screen.
    """
    from . import align
    segs = entry.get("whisper") or (entry.get("synced")
                                    if entry.get("transcribed") else None)
    if not segs:
        return {}
    timed = align.sync(sheet, segs, dur)
    if not timed:
        return {}
    return dict(entry, synced=timed, whisper=segs, sheet=sheet,
                plain="\n".join(l["line"] for l in timed),
                source=source or entry.get("sheet_source")
                or "your lyrics, timed against the audio",
                ok=True, at=time.time())


def _pump() -> None:
    from . import lyrics
    while True:
        key, artist, title, vid, dur = _q.get()
        with _lock:
            _state[key] = "running"
        got = {}
        try:
            src = _find_audio(artist, title)
            with tempfile.TemporaryDirectory() as td:
                if src is None and vid:
                    src = _grab_audio(vid, Path(td))
                if src is not None:
                    got = transcribe_file(src)
        except Exception:
            got = {}
        if got:
            with lyrics._cache_lock:
                # Someone may have pasted the real words while this was in the
                # queue, in which case whisper was only wanted for its timings.
                old = lyrics._load().get(key) or {}
                sheet = old.get("sheet")
                if sheet:
                    got = apply_sheet(dict(got, sheet_source=old.get("sheet_source")),
                                      sheet, dur) or got
                got["at"] = time.time()
                got["ok"] = True
                lyrics._put(key, got)
        with _lock:
            _state[key] = "done" if got else "failed"
        _q.task_done()


def request(key: str, artist: str, title: str, vid: str = "",
            dur: float = 0) -> str:
    """Queue a transcription for a track nothing else had timed words for."""
    if not available():
        return "unavailable"
    with _lock:
        if key in _state:
            return _state[key]
        _state[key] = "queued"
        global _worker
        if _worker is None:
            _worker = threading.Thread(target=_pump, daemon=True)
            _worker.start()
    _q.put((key, artist, title, vid, dur))
    return "queued"


def status(key: str) -> str:
    with _lock:
        return _state.get(key, "idle")


if __name__ == "__main__":
    got = transcribe_file(Path(sys.argv[1]))
    print(len(got.get("synced", [])), "lines")
    for l in got.get("synced", [])[:12]:
        print(f'  {l["t"]:7.2f}  {l["line"]}')
