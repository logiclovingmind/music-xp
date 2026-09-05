"""Turn a YouTube video into an (artist, title) pair the taste model can read.

The taste model is keyed on plain artist names, so a raw upload title like
"Artist - Song (Official Music Video) [4K]" has to be reduced before it can be
scored at all. Getting this wrong doesn't throw — it silently invents an unknown
artist and the track scores as a stranger, so the parsing is deliberately
conservative: when a title doesn't look like "Artist - Song", fall back to the
channel, which for "- Topic" and VEVO channels is the label-delivered truth.
"""
from __future__ import annotations

import re

# Version/edition noise that is never part of the song's name.
_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*\b(?:official|officiel\w*|oficial|offiziell\w*|ufficiale|"
    r"lyric|lyrics|letra|legendado|visuali[sz]er|audio|video|videoclip|"
    r"teledysk|mv|m/v|4k|hd|hq|remaster(?:ed)?|explicit|clean|clip|"
    r"performance|color coded|sub\w*|eng)\b[^)\]]*[\)\]]",
    re.IGNORECASE,
)

# Words that are never an artist. When the title's left-hand side is only this,
# it was a label or an upload tag, not a name — the channel is the better guess.
_NOT_AN_ARTIST = {
    "new release", "release", "new", "official", "topic", "various artists",
    "va", "music", "audio", "video", "lyrics", "premiere", "out now", "full album",
}
# The same noise unbracketed, e.g. "… - 'MOON' M/V" or "… | Official Video".
# Repeated because a title often carries two of them stacked.
_TRAILING = re.compile(
    r"(?:\s*[\|\-–—]\s*|\s+)(?:official\s+)?(?:music\s+)?"
    r"(?:m/v|mv|video|audio|lyrics?|visuali[sz]er|4k|hd|hq)\s*$",
    re.IGNORECASE,
)

# Live/stage recordings — we want the studio song. Kept narrow so real titles
# survive: "Live Forever", "Alive", "Live Your Life" must not match.
_LIVE = re.compile(
    r"[\(\[]\s*live\b"
    r"|[-–—]\s*live\b"
    r"|\blive\s+(?:at|in|from|on|@|session|sessions|version|performance|"
    r"recording|edit|acoustic|concert|tour)\b"
    r"|\bunplugged\b|\bin\s+concert\b",
    re.IGNORECASE,
)

_DASHES = [" - ", " – ", " — ", " -- "]

# Only the codes YouTube actually hands back for music, mapped to the names the
# taste model already uses in its per-language tables.
_LANGS = {
    "en": "english", "es": "spanish", "fr": "french", "pt": "portuguese",
    "de": "german", "nl": "dutch", "el": "greek", "ro": "romanian",
    "sr": "serbian", "sq": "albanian", "hy": "armenian", "ar": "arabic",
    "th": "thai", "vi": "vietnamese", "ko": "korean", "ja": "japanese",
    "zh": "mandarin", "yo": "yoruba", "ha": "hausa", "fa": "persian",
}


def is_live(*texts: str) -> bool:
    return any(_LIVE.search(t or "") for t in texts)


def _strip_noise(text: str) -> str:
    text = _NOISE.sub("", text)
    # Stacked suffixes ("… Official Video 4K") need more than one pass.
    for _ in range(3):
        stripped = _TRAILING.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = text.strip(" -–—|·")
    # Titles are often quoted whole: 'MOON', "Glitter", «Zeina».
    if len(text) > 1 and text[0] in "\"'“‘«" and text[-1] in "\"'”’»":
        text = text[1:-1].strip()
    return text


def clean_channel(channel: str) -> str:
    """'Artist - Topic' and 'ArtistVEVO' both mean the artist.

    Returns "" when what's left is a generic word rather than a name. A topic
    channel is usually the label's own truth, but not always: "Quiereme" was
    delivered under "Release - Topic", which would file a real song under an
    invented artist called Release and then teach the taste model about it.
    """
    name = re.sub(r"\s*-\s*Topic\s*$", "", channel, flags=re.IGNORECASE)
    name = re.sub(r"VEVO\s*$", "", name, flags=re.IGNORECASE)
    name = name.strip() or channel.strip()
    return "" if name.lower() in _NOT_AN_ARTIST else name


def language_of(code: str) -> str | None:
    """Map YouTube's audio-language tag ('es-419', 'pt-BR') to our name."""
    if not code:
        return None
    return _LANGS.get(code.split("-")[0].lower())


def split_artist_title(raw_title: str, channel: str) -> tuple[str, str]:
    """Best-effort (artist, title). Falls back to the channel as the artist.

    The artist comes back empty when neither the title nor the channel names one
    — the caller should drop the track rather than score a made-up name.
    """
    title = _strip_noise(raw_title)
    for dash in _DASHES:
        if dash in title:
            left, _, right = title.partition(dash)
            left, right = left.strip(), _strip_noise(right)
            if not (left and right):
                continue
            if left.lower() in _NOT_AN_ARTIST:
                # "New Release - Go !" — the song is real, the artist isn't.
                return clean_channel(channel), right
            # "Latifa - Latifa –Shabhi…": the artist repeated inside the song.
            if right.lower().startswith(left.lower()):
                right = _strip_noise(right[len(left):]) or right
            return left, right
    # No separator: the channel names the artist and the whole title is the song.
    return clean_channel(channel), title or raw_title.strip()
