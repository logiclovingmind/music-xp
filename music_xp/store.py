"""Tiny JSON-file persistence for the taste model, pick history, and state."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import DATA_DIR

PROFILES = DATA_DIR / "profiles.json"
TASTE = DATA_DIR / "taste.json"
HISTORY = DATA_DIR / "history.json"
DISLIKES = DATA_DIR / "dislikes.json"
DIGESTS = DATA_DIR / "digests.json"
SEEN_LIKES = DATA_DIR / "seen_likes.json"
SEED_CURSORS = DATA_DIR / "seed_cursors.json"


# These files are read several times per web request and written from request
# threads, so they get one parsed copy per version on disk, under a lock.
_lock = threading.RLock()
_memo: dict[Path, tuple[tuple | None, Any]] = {}
_depth = threading.local()
LOCKFILE = DATA_DIR / ".store.lock"


@contextmanager
def transaction():
    """Hold the data files against other processes for a read-modify-write.

    Loading history, changing it and saving it back is three steps, and the
    daily run does it at 07:00 whether or not the dashboard is open. Without a
    lock spanning all three, whoever saves second writes a copy that never saw
    the other's work — a rating or a whole morning's picks quietly disappear.
    """
    with _lock:
        # flock is held per open file, not per thread, so a second open() inside
        # an outer transaction would wait on a lock this very thread already
        # holds. Nesting is therefore a no-op rather than a hang.
        if getattr(_depth, "n", 0):
            yield
            return
        DATA_DIR.mkdir(exist_ok=True)
        with open(LOCKFILE, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _depth.n = 1
            try:
                # Whatever another process wrote while we queued is now the
                # truth, so nothing may be served from before the wait.
                _memo.clear()
                yield
            finally:
                _depth.n = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read(path: Path, default: Any) -> Any:
    """Parsed contents, re-read only when the file changed on disk.

    Callers load, mutate and save the same object, so the memo hands back the
    live copy rather than a snapshot; another process's write moves the stamp
    and forces a fresh parse.
    """
    with _lock:
        now = _stamp(path)
        if now is None:
            return default
        seen, value = _memo.get(path, (None, None))
        if seen is not None and seen == now:
            return value
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            # Reading a damaged file as "empty" invites the next save to make
            # the loss permanent, so keep the evidence and reuse what we had.
            try:
                path.replace(path.with_suffix(path.suffix + ".corrupt"))
            except OSError:
                pass
            return value if seen is not None else default
        _memo[path] = (now, value)
        return value


def _write(path: Path, data: Any) -> None:
    with _lock:
        DATA_DIR.mkdir(exist_ok=True)
        # A temp name per writer: a shared one gets truncated by whoever starts
        # second, and the mangled result is renamed over the real file.
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        _memo[path] = (_stamp(path), data)


def load_profiles() -> dict:
    """The superseded per-language profiles. Only migrate_taste reads this."""
    return _read(PROFILES, {})


def load_taste() -> dict:
    from . import taste
    return taste.ensure(_read(TASTE, taste.empty()))


def save_taste(model: dict) -> None:
    _write(TASTE, model)


def load_history() -> list[dict]:
    return _read(HISTORY, [])


def save_history(history: list[dict]) -> None:
    _write(HISTORY, history)


def load_digests() -> list[dict]:
    return _read(DIGESTS, [])


def save_digests(digests: list[dict]) -> None:
    _write(DIGESTS, digests)


def load_seen_likes() -> set[str] | None:
    """Every YouTube like already accounted for. None means never recorded.

    The distinction matters on the very first run: an empty set would mean your
    whole existing Liked Music is unaccounted for and should be learned, when in
    fact `seed` already absorbed it. None says "no baseline yet", which is the
    signal to take one instead of learning it twice.
    """
    raw = _read(SEEN_LIKES, None)
    return None if raw is None else set(raw)


def save_seen_likes(video_ids: set[str]) -> None:
    _write(SEEN_LIKES, sorted(video_ids))


def load_seed_cursors() -> dict:
    """Per-language position in the rotating scout seed list."""
    return _read(SEED_CURSORS, {})


def save_seed_cursors(cursors: dict) -> None:
    _write(SEED_CURSORS, cursors)


def load_dislikes() -> set[str]:
    return set(_read(DISLIKES, []))


def add_dislike(video_id: str) -> None:
    with transaction():
        _write(DISLIKES, sorted(load_dislikes() | {video_id}))


def remove_dislike(video_id: str) -> None:
    with transaction():
        _write(DISLIKES, sorted(load_dislikes() - {video_id}))
