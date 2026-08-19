import json
import os

OVERRIDES_PATH = "config_overrides.json"


def load_overrides(path: str = OVERRIDES_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_override(param: str, value, path: str = OVERRIDES_PATH):
    overrides = load_overrides(path)
    overrides[param] = value
    with open(path, "w") as f:
        json.dump(overrides, f, indent=2)


def apply_overrides(cfg, path: str = OVERRIDES_PATH):
    """Applied at bot startup - lets a human-confirmed parameter change
    (via APPLY) persist across restarts without hardcoding it back into
    config.py."""
    overrides = load_overrides(path)
    for param, value in overrides.items():
        if hasattr(cfg, param):
            setattr(cfg, param, value)
    return cfg
