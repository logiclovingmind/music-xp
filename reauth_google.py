"""Mint a fresh Google OAuth token for the YouTube Data API → oauth.json.

While the Cloud project's consent screen is in "Testing", Google expires refresh
tokens after ~7 days, after which every run dies with `invalid_grant: Token has
been expired or revoked`.

Our Cloud client (`client_secret_*.json`) is a **TV / Limited Input device**
client — its JSON carries no `redirect_uris`, and the browser redirect flow is
rejected with `Error 400: invalid_request`. It only accepts the device-code
flow, which is what this runs: you get a short code to type at google.com/device.

    .venv/bin/python3 reauth_google.py
"""
from __future__ import annotations

import glob
import json
import time
import webbrowser
from pathlib import Path

import requests

ROOT = Path(__file__).parent
OUT = ROOT / "oauth.json"
SCOPE = "https://www.googleapis.com/auth/youtube"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"

secret_file = sorted(glob.glob(str(ROOT / "client_secret_*.json")))
if not secret_file:
    raise SystemExit("No client_secret_*.json in the project root.")
creds = json.loads(Path(secret_file[-1]).read_text())["installed"]

r = requests.post(DEVICE_URL, data={"client_id": creds["client_id"],
                                    "scope": SCOPE}, timeout=30)
r.raise_for_status()
code = r.json()

print(f"\n  Go to: {code['verification_url']}")
print(f"  Enter code: {code['user_code']}")
print("  Sign in as the account that owns the playlists, then approve.\n",
      flush=True)
webbrowser.open(code["verification_url"])

interval = int(code.get("interval", 5))
deadline = time.time() + int(code.get("expires_in", 1800))
while time.time() < deadline:
    time.sleep(interval)
    d = requests.post(TOKEN_URL, data={
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "device_code": code["device_code"],
        "grant_type": GRANT,
    }, timeout=30).json()
    if d.get("access_token"):
        OUT.write_text(json.dumps({
            "access_token": d["access_token"],
            "refresh_token": d["refresh_token"],
            "scope": d.get("scope", SCOPE),
            "token_type": d.get("token_type", "Bearer"),
            "expires_in": d.get("expires_in", 3600),
            "expires_at": int(time.time()) + int(d.get("expires_in", 3600)),
        }, indent=1))
        print("OAUTH_OK: wrote", OUT, flush=True)
        raise SystemExit(0)
    err = d.get("error")
    if err == "slow_down":
        interval += 5
    elif err != "authorization_pending":
        raise SystemExit(f"Authorisation failed: {d}")
raise SystemExit("Code expired before approval.")
