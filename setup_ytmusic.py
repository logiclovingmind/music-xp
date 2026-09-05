"""Turn copied YouTube Music request headers into browser.json.

You copy the request headers from your logged-in music.youtube.com tab; this
script reads them (from your clipboard by default, or a file) and writes the
auth file, then verifies it by making one real API call.

  python setup_ytmusic.py            # read headers from clipboard (macOS pbpaste)
  python setup_ytmusic.py headers.txt

We can't skip the copy step: that clipboard content includes your session
cookie, which is your login — only you can produce it from your own browser.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from ytmusicapi import YTMusic, setup

OUT = Path(__file__).parent / "browser.json"


def read_headers() -> str:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).read_text()
    # macOS clipboard
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def curl_to_headers(raw: str) -> str:
    """Extract 'Name: value' header lines from a copied cURL command."""
    pairs = re.findall(r"-H\s+'([^']+)'", raw)
    pairs += re.findall(r'-H\s+"([^"]+)"', raw)
    pairs += re.findall(r"--header\s+'([^']+)'", raw)
    # cookie sometimes copied as -b '...'
    cookie = re.findall(r"-b\s+'([^']+)'", raw) + re.findall(r'-b\s+"([^"]+)"', raw)
    lines = list(pairs)
    if cookie and not any(p.lower().startswith("cookie:") for p in pairs):
        lines.append("cookie: " + cookie[0])
    return "\n".join(lines)


def normalize(raw: str) -> str:
    """Accept either a raw request-headers block or a 'Copy as cURL' command."""
    if "curl " in raw[:20].lower() or " -H " in raw or "--header" in raw:
        headers = curl_to_headers(raw)
        if headers:
            return headers
    return raw


def looks_valid(raw: str) -> bool:
    low = raw.lower()
    return "cookie" in low and ("user-agent" in low or "authorization" in low
                                or "x-goog" in low)


def main() -> None:
    raw = normalize(read_headers().strip())
    if not raw:
        sys.exit("No headers found. Copy the request headers first, then re-run.")
    if not looks_valid(raw):
        sys.exit("That doesn't look like YT Music request headers "
                 "(no Cookie line found). Re-copy the full 'Request Headers' block.")

    setup(filepath=str(OUT), headers_raw=raw)
    print(f"Wrote {OUT.name}. Verifying with a real API call…")

    yt = YTMusic(str(OUT))
    songs = yt.get_library_songs(limit=1)
    n = len(songs) if isinstance(songs, list) else len(songs.get("tracks", []))
    print(f"Success — authenticated. (library reachable, sample size {n})")
    print("Next: python -m music_xp.seed")


if __name__ == "__main__":
    main()
