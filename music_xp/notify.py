"""Best-effort macOS desktop notification when the daily playlist is ready.

Getting the app's own logo onto the banner is fiddly on modern macOS:
  • Plain `osascript display notification` is posted by the system's Script-Editor
    automation host, so it always shows that generic scroll icon — nothing we do
    to our own bundle can change it.
  • Since macOS 11 Apple ignores custom icons (-appIcon/-contentImage) from
    third-party senders.
The one reliable route is `terminal-notifier -sender <bundle-id>`: it posts the
banner *as* a registered app and borrows that app's icon. We ship a tiny
"Music XP.app" (icon = the logo, id = com.zei.musicxp.notifier) purely to lend
its icon here. Fallbacks (applet, then raw osascript) still deliver a banner —
just with the generic icon — if terminal-notifier isn't installed.

Everything is wrapped so a headless run or non-mac host simply does nothing.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# The branded applet lives in ~/Applications so macOS caches its icon (a copy in
# the project's Downloads folder never gets icon-cached, so -sender can't borrow
# its icon from there).
APP = Path.home() / "Applications" / "Music XP.app"
SENDER_ID = "com.zei.musicxp.notifier"


def _terminal_notifier() -> str | None:
    """Locate terminal-notifier, incl. Homebrew paths launchd's PATH omits."""
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for p in ("/usr/local/bin/terminal-notifier",
              "/opt/homebrew/bin/terminal-notifier"):
        if Path(p).exists():
            return p
    return None


def _run(cmd: list[str]) -> bool:
    try:
        subprocess.run(cmd, timeout=10, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def notify(title: str, message: str, subtitle: str = "") -> None:
    """Post a Notification Center banner. Silent no-op on any failure."""
    if sys.platform != "darwin":
        return

    # Preferred: terminal-notifier borrowing our app's icon via -sender.
    tn = _terminal_notifier()
    if tn:
        cmd = [tn, "-title", title, "-message", message, "-sender", SENDER_ID]
        if subtitle:
            cmd += ["-subtitle", subtitle]
        if _run(cmd):
            return

    # Fallback: our applet (delivers, but with the generic icon).
    if APP.is_dir() and _run(["open", "-n", str(APP), "--args",
                              title, message, subtitle]):
        return

    # Last resort: raw osascript.
    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{esc(message)}" with title "{esc(title)}"'
    if subtitle:
        script += f' subtitle "{esc(subtitle)}"'
    _run(["osascript", "-e", script])
