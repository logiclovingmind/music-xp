"""The daily schedule, as a thing the Settings tab can actually turn off.

The daily builder runs from a launchd agent. Until now that was a file you had
to copy into ~/Library/LaunchAgents and drive with launchctl by hand; this wraps
it so the dashboard can read the current state and change it.

launchd, not cron, because a 2012 laptop is asleep at 07:00 more often than not
and launchd runs a missed calendar job on the next wake. cron would just skip it.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from .config import ROOT

LABEL = "com.zei.musicxp"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
DEFAULT_HOUR, DEFAULT_MINUTE = 7, 0


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["launchctl", *args], capture_output=True, text=True,
                           timeout=20)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def _plist_body(hour: int, minute: int) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(ROOT / "run_daily.sh")],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": False,
        "StandardOutPath": str(ROOT / "data" / "launchd.out.log"),
        "StandardErrorPath": str(ROOT / "data" / "launchd.err.log"),
    }


def status() -> dict:
    """Current schedule: whether it's loaded, and the time it fires."""
    hour, minute = DEFAULT_HOUR, DEFAULT_MINUTE
    installed = PLIST.exists()
    if installed:
        try:
            data = plistlib.loads(PLIST.read_bytes())
            cal = data.get("StartCalendarInterval") or {}
            if isinstance(cal, list):        # launchd allows a list of times
                cal = cal[0] if cal else {}
            hour = int(cal.get("Hour", DEFAULT_HOUR))
            minute = int(cal.get("Minute", DEFAULT_MINUTE))
        except Exception:
            pass
    code, _ = _launchctl("print", f"{_domain()}/{LABEL}")
    return {"enabled": code == 0, "installed": installed,
            "hour": hour, "minute": minute,
            "script": str(ROOT / "run_daily.sh")}


def apply(enabled: bool, hour: int, minute: int) -> dict:
    """Turn the daily run on/off and set its time. Returns the new status."""
    hour = max(0, min(23, int(hour)))
    minute = max(0, min(59, int(minute)))

    # Always unload first: launchd reads the plist at load time, so an edit
    # doesn't take effect until the job is re-bootstrapped.
    _launchctl("bootout", f"{_domain()}/{LABEL}")

    if not enabled:
        return {**status(), "message": "Daily run turned off."}

    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(_plist_body(hour, minute)))
    code, out = _launchctl("bootstrap", _domain(), str(PLIST))
    if code != 0:
        return {**status(), "message": f"launchctl refused it: {out.strip()[:200]}"}
    return {**status(),
            "message": f"Daily run set for {hour:02d}:{minute:02d}."}
