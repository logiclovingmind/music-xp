"""Read taste signal from an offline music collection (folders of mp3/m4a).

A hand-curated local library is a strong taste signal. We read the artist from
ID3/MP4 tags where present, and fall back to parsing 'Artist - Title' filenames.
Files under a 'favorite'-named folder count extra.
"""
from __future__ import annotations

import os
import re

AUDIO_EXT = (".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg")

# junk to strip from filenames before parsing the artist
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_LEADING_TRACK = re.compile(r"^\s*\d{1,3}[\.\)\-_ ]+\s*")
_SPLIT = re.compile(r"\s*(?:,|&|/|\bfeat\.?\b|\bft\.?\b|\bx\b|\bvs\.?\b|\bwith\b)\s*",
                    re.IGNORECASE)


def _artist_from_filename(name: str) -> list[str]:
    stem = os.path.splitext(name)[0]
    stem = _LEADING_TRACK.sub("", stem)
    stem = _BRACKETS.sub("", stem)
    if " - " not in stem:
        return []
    artist_part = stem.split(" - ", 1)[0].strip()
    return _split_artists(artist_part)


def _split_artists(raw: str) -> list[str]:
    out = []
    for piece in _SPLIT.split(raw):
        p = piece.strip(" -_.")
        if 1 < len(p) <= 60 and not p.isdigit():
            out.append(p)
    return out


def _tag_artists(path: str) -> list[str]:
    try:
        from mutagen import File
        a = File(path, easy=True)
        if not a:
            return []
        raw = a.get("artist") or a.get("albumartist") or []
        result = []
        for r in raw:
            result += _split_artists(r)
        return result
    except Exception:
        return []


def _norm(text: str) -> str:
    """Latin-alphanumeric skeleton for fuzzy title/artist matching."""
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).strip()


def _title_artist_from_tags(path: str) -> tuple[str, str] | None:
    try:
        from mutagen import File
        a = File(path, easy=True)
        if not a:
            return None
        title = (a.get("title") or [""])[0]
        artist = (a.get("artist") or a.get("albumartist") or [""])[0]
        return (artist, title)
    except Exception:
        return None


def _title_artist_from_filename(name: str) -> tuple[str, str] | None:
    """Parse 'Artist - Title' filenames (our MusicXP downloads use this)."""
    stem = os.path.splitext(name)[0]
    stem = _LEADING_TRACK.sub("", stem)
    if " - " not in stem:
        return None
    artist, title = stem.split(" - ", 1)
    return (artist.strip(), title.strip())


def owned_index(dirs: list[str]) -> dict[str, set[str]]:
    """Map normalized title -> set of normalized artists across owned libraries.

    Reads (title, artist) from tags, falling back to 'Artist - Title' filenames.
    Used to skip re-downloading songs the user already has locally.
    """
    index: dict[str, set[str]] = {}
    for root in dirs:
        if not os.path.isdir(root):
            continue
        for dp, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(AUDIO_EXT):
                    continue
                pair = _title_artist_from_tags(os.path.join(dp, f)) \
                    or _title_artist_from_filename(f)
                if not pair:
                    continue
                artist, title = pair
                nt, na = _norm(title), _norm(artist)
                if not nt or not na:
                    continue
                index.setdefault(nt, set()).add(na)
    return index


# Tokens too generic to prove two artists are the same act.
_ARTIST_STOP = {"the", "a", "x", "and", "feat", "ft", "with"}


def is_owned(index: dict[str, set[str]], artist: str, title: str) -> bool:
    """True if a track with this title+artist is already in an owned library.

    Requires a title-skeleton match plus a shared meaningful artist token, so
    unrelated songs that happen to share a title aren't wrongly treated as owned.
    """
    nt, na = _norm(title), _norm(artist)
    if not nt or not na:
        return False
    owners = index.get(nt)
    if not owners:
        return False
    cand = set(na.split()) - _ARTIST_STOP
    for owner in owners:
        if owner == na or (cand & (set(owner.split()) - _ARTIST_STOP)):
            return True
    return False


def local_artist_counts(dirs: list[str], verbose: bool = False) -> dict[str, float]:
    """Weighted artist signal from local audio files across the given dirs."""
    counts: dict[str, float] = {}
    n_files = tagged = named = 0

    for root in dirs:
        if not os.path.isdir(root):
            if verbose:
                print(f"    · (skip, not found) {root}")
            continue
        for dp, _dirs, files in os.walk(root):
            fav = "favorite" in dp.lower()
            for f in files:
                if not f.lower().endswith(AUDIO_EXT):
                    continue
                n_files += 1
                path = os.path.join(dp, f)
                artists = _tag_artists(path)
                weight = 2.5
                if artists:
                    tagged += 1
                else:
                    artists = _artist_from_filename(f)
                    weight = 2.0
                    if artists:
                        named += 1
                if not artists:
                    continue
                if fav:
                    weight *= 1.5
                for artist in artists:
                    counts[artist] = counts.get(artist, 0.0) + weight

    if verbose:
        print(f"    · local files: {n_files} scanned "
              f"({tagged} via tags, {named} via filename) → "
              f"{len(counts)} artists")
    return counts
