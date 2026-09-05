"""Loads config.yaml and .env, and resolves data-directory paths."""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.yaml"
OVERRIDES_PATH = DATA_DIR / "overrides.json"

# Scalar settings the web UI may override (kept out of the commented config.yaml
# so that file stays a pristine, documented source of defaults).
OVERRIDABLE = {
    "min_score", "adventurousness", "picks_per_day", "release_window_days",
    "max_per_artist", "feedback_window_days", "playlist_privacy",
    "discovery_ratio", "taste_decay", "playlist_name_prefix",
}


def load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        return json.loads(OVERRIDES_PATH.read_text())
    except (json.JSONDecodeError, ValueError):
        return {}


def save_overrides(overrides: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = OVERRIDES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(overrides, ensure_ascii=False, indent=2))
    tmp.replace(OVERRIDES_PATH)


def _apply_overrides(cfg: dict) -> None:
    ov = load_overrides()
    for key in OVERRIDABLE:
        if key in ov:
            cfg[key] = ov[key]
    disabled = {d.lower() for d in ov.get("disabled_languages", [])}
    if disabled:
        cfg["languages"] = [l for l in cfg["languages"]
                            if l["name"].lower() not in disabled]


def _load_dotenv() -> None:
    """Minimal .env loader (no external dependency)."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def load_config() -> dict:
    _load_dotenv()
    DATA_DIR.mkdir(exist_ok=True)
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    _apply_overrides(cfg)
    cfg["_env"] = {
        "lastfm_key": os.environ.get("LASTFM_API_KEY", ""),
        "ytm_oauth_client_id": os.environ.get("YTM_OAUTH_CLIENT_ID", ""),
        "ytm_oauth_client_secret": os.environ.get("YTM_OAUTH_CLIENT_SECRET", ""),
    }
    return cfg


def language_names(cfg: dict) -> list[str]:
    return [lang["name"] for lang in cfg["languages"]]
