import sys, time
from pathlib import Path
from ytmusicapi.auth.oauth import OAuthCredentials
from ytmusicapi.auth.oauth.token import RefreshingToken

CLIENT_ID = "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"
OUT = Path("oauth.json")

cred = OAuthCredentials(CLIENT_ID, CLIENT_SECRET)
code = cred.get_code()
print("VERIFY_URL:", code["verification_url"])
print("USER_CODE:", code["user_code"])
print("Enter this code, sign in as zeixdream@gmail.com, then approve.", flush=True)

interval = int(code.get("interval", 5))
deadline = time.time() + int(code.get("expires_in", 1800))
while time.time() < deadline:
    time.sleep(interval)
    raw = cred.token_from_code(code["device_code"])
    if raw.get("access_token"):
        exp = raw.get("refresh_token_expires_in", raw["expires_in"])
        tok = RefreshingToken(credentials=cred, access_token=raw["access_token"],
                              refresh_token=raw["refresh_token"], scope=raw["scope"],
                              token_type=raw["token_type"], expires_in=exp)
        tok.update(raw)
        tok.store_token(str(OUT))
        print("OAUTH_OK: wrote", OUT, flush=True)
        sys.exit(0)
    err = raw.get("error")
    if err in ("authorization_pending", "slow_down"):
        if err == "slow_down":
            interval += 5
        continue
    print("OAUTH_ERR:", raw, flush=True)
    sys.exit(1)
print("OAUTH_TIMEOUT: code expired before approval", flush=True)
sys.exit(2)
