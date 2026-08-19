"""
State is written atomically (temp file + rename) so a crash mid-write can
never leave a half-written, corrupt state file that breaks the next
startup's reconciliation - locked as a self-heal requirement earlier.
"""
import json
import os
import tempfile
import logging

logger = logging.getLogger("state")

DEFAULT_STATE = {
    "current_mode": "PAPER",
    "mode_set_date": None,
    "open_position": None,        # dict or None
    "closed_positions_today": [],
    "trades_today_count": 0,
    "loss_today": 0.0,
    "cooldown_until": None,       # ISO timestamp or None
    "last_stop_loss_time": None,
    "current_date": None,
    "stop_requested": False,      # kill switch flag, checked at top of every poll loop iteration
    "param_change_log": [],       # audit trail for analytics-driven param changes
}


class StateManager:
    def __init__(self, path: str):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if not os.path.exists(self.path):
            return dict(DEFAULT_STATE)
        try:
            with open(self.path) as f:
                data = json.load(f)
            merged = dict(DEFAULT_STATE)
            merged.update(data)
            return merged
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("State file corrupt or unreadable (%s) - starting from defaults. "
                         "Manual reconciliation against broker may be needed.", exc)
            return dict(DEFAULT_STATE)

    def save(self):
        dir_ = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".state_tmp_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, default=str)
            os.replace(tmp_path, self.path)   # atomic on POSIX
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def update(self, **kwargs):
        self.data.update(kwargs)
        self.save()

    def log_param_change(self, param: str, old_value, new_value, reason: str, source: str):
        import datetime
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "param": param, "old_value": old_value, "new_value": new_value,
            "reason": reason, "source": source,   # source: "manual" | "analytics_recommendation"
        }
        self.data.setdefault("param_change_log", []).append(entry)
        self.save()
