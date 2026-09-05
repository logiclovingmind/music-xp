"""Give a known-good lyric sheet the timings of a whisper transcription.

Whisper mishears words but rarely misses *where* singing happens, so its segment
times are usable anchors even when its text is wrong — badly enough wrong, for a
language it wasn't trained on, that the words themselves are worthless. This
walks the two word streams side by side, matches what it can, and interpolates
the rest, so the sheet keeps the real words while borrowing whisper's clock.

That covers both cases where we hold words without a clock: a sheet pasted in by
hand, and lyrics a source returned as flat text with no stamps.

A "0:34" style marker in the sheet is treated as a hard anchor and beats the
match, since that came from someone actually listening.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

TOK = re.compile(r"[^\W_]+", re.UNICODE)

# Arabic is written with several interchangeable letter forms and whisper picks
# whichever it likes: عيونة for the sheet's عيونه, أطرب for اقرب. Folding them
# together is the difference between matching a line and missing it entirely.
_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                     "ة": "ه", "ى": "ي", "ئ": "ي", "ؤ": "و"})
_AR_MARKS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")

STAMP = re.compile(r"^\s*\[?(\d{1,2}):(\d{2})(?:\.\d+)?\]?\s*")
SECTION = re.compile(
    r"^\s*\[?(chorus|verse|pre[- ]?chorus|post[- ]?chorus|outro|intro|bridge|"
    r"hook|refrain|ch)\b[^\]]*\]?\s*:?\s*$", re.I)
NOTE = re.compile(r"^[♪♫\s]+|[♪♫\s]+$")
STAGE = re.compile(r"^[(\[][^)\]]*[)\]]$")
# Whisper's filler over silence. Left in, a trailing "you" thirty seconds past
# the last sung word anchors the outro and drags it across the instrumental tail.
JUNK = re.compile(r"^\W*(you|thanks?( for watching)?[.!]?|thank you[.!]?|"
                  r"bye[.!]?|subtitles? by.*|amara\.org.*)\W*$", re.I)

MIN_GAP = 0.35


def fold(w: str) -> str:
    return _AR_MARKS.sub("", w.translate(_AR)).lower()


def read_sheet(text: str) -> list[dict]:
    """Sheet -> [{line, anchor}], section headers stripped."""
    out = []
    for raw in text.splitlines():
        s = raw.strip().strip('"')
        if not s:
            continue
        anchor = None
        m = STAMP.match(s)
        if m:
            anchor = int(m.group(1)) * 60 + int(m.group(2))
            s = s[m.end():].strip()
        if not s or SECTION.match(s):
            # A bare "0:34 [Verse 1]" still carries its anchor to the next line.
            if anchor is not None:
                out.append({"line": None, "anchor": anchor})
            continue
        out.append({"line": s, "anchor": anchor})

    fixed, pending = [], None
    for e in out:
        if e["line"] is None:
            pending = e["anchor"] if pending is None else pending
            continue
        if pending is not None and e["anchor"] is None:
            e["anchor"] = pending
        pending = None
        fixed.append(e)
    return fixed


def word_stream(segs: list[dict], dur: float) -> list[tuple[str, float]]:
    """Transcription segments -> a word stream, each word timed inside its segment."""
    keep = []
    for s in segs:
        text = NOTE.sub("", s.get("line", "")).strip()
        if JUNK.match(text):
            continue      # a phantom: it must not bound the segment before it
        keep.append((float(s["t"]), "" if STAGE.match(text) else text))
    keep.sort()

    # The last segment has no successor to bound it, and stretching it to the end
    # of the file smears a short outro across whatever instrumental follows. Give
    # it the song's own median word rate instead.
    rates = []
    for i in range(len(keep) - 1):
        n = len(TOK.findall(keep[i][1]))
        span = keep[i + 1][0] - keep[i][0]
        if n and span > 0:
            rates.append(span / n)
    per_word = sorted(rates)[len(rates) // 2] if rates else 0.4

    stream = []
    for i, (t, text) in enumerate(keep):
        words = [fold(w) for w in TOK.findall(text)]
        if not words:
            continue
        end = (keep[i + 1][0] if i + 1 < len(keep)
               else t + per_word * len(words))
        if dur:
            end = min(end, dur)
        step = max(0.0, end - t) / len(words)
        for j, w in enumerate(words):
            stream.append((w, t + j * step))
    return stream


def align(sheet: list[dict], stream: list[tuple[str, float]], dur: float) -> list[dict]:
    wwords = [w for w, _ in stream]
    wtimes = [t for _, t in stream]

    ltokens, line_start = [], []
    for e in sheet:
        toks = [fold(w) for w in TOK.findall(e["line"])]
        line_start.append(len(ltokens) if toks else None)
        ltokens.extend(toks)
    if not ltokens or not wwords:
        return []

    pairs = []
    for a, b, n in SequenceMatcher(None, ltokens, wwords,
                                   autojunk=False).get_matching_blocks():
        for k in range(n):
            pairs.append((a + k, wtimes[b + k]))

    hard = [(line_start[i], float(e["anchor"])) for i, e in enumerate(sheet)
            if e["anchor"] is not None and line_start[i] is not None]
    # Each anchor clears a window of fuzzy matches around itself, since a match a
    # token or two away pulls the line off the time someone actually heard. The
    # windows are applied against the matches alone — short lines sit two tokens
    # apart, so letting an anchor clear the window would delete its neighbour.
    for li, _ in hard:
        pairs = [(a, x) for a, x in pairs if abs(a - li) > 2]
    pairs += hard
    pairs.sort()

    mono = []
    for a, t in pairs:
        while mono and t < mono[-1][1] and a > mono[-1][0]:
            mono.pop()
        if not mono or (a > mono[-1][0] and t >= mono[-1][1]):
            mono.append((a, t))
    if not mono:
        return []

    out = []
    for i, e in enumerate(sheet):
        if line_start[i] is None:
            continue
        out.append({"t": round(interp(line_start[i], mono), 2), "line": e["line"]})
    for i in range(1, len(out)):      # never let a line start before its predecessor
        if out[i]["t"] <= out[i - 1]["t"]:
            out[i]["t"] = round(out[i - 1]["t"] + MIN_GAP, 2)
    return out


def interp(li: int, mono: list[tuple[int, float]]) -> float:
    if li <= mono[0][0]:
        return mono[0][1]
    if li >= mono[-1][0]:
        return mono[-1][1]
    for k in range(1, len(mono)):
        if mono[k][0] >= li:
            a0, t0 = mono[k - 1]
            a1, t1 = mono[k]
            return t0 + (li - a0) / max(1, a1 - a0) * (t1 - t0)
    return mono[-1][1]


def sync(sheet_text: str, segs: list[dict], dur: float = 0) -> list[dict]:
    """A pasted or plain-text sheet, timed against a transcription. [] if hopeless."""
    sheet = read_sheet(sheet_text)
    if not sheet:
        return []
    return align(sheet, word_stream(segs, dur), dur)
