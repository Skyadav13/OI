"""
Distinct from feed_health.py: that module tracks whether BROKER DATA is
stale. This tracks whether the BOT PROCESS ITSELF is still alive and
looping - if the whole server goes down, systemd/cron can't restart a
process on a dead machine, and nothing else in this design would notice.

Honest limitation: heartbeat_check.py as shipped here still assumes SOME
scheduler (cron) is running to invoke it - if the entire VM is down, a cron
job on that same VM won't fire either. For genuine redundancy this check
should ideally run from a different host (a free external cron-ping
service, a second small always-on machine, etc.) - included here as the
baseline, not a full guarantee.
"""
import time
import os


def write_heartbeat(path: str):
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass   # heartbeat failure to write should never crash the main loop itself


def read_heartbeat_age_seconds(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            ts = float(f.read().strip())
        return time.time() - ts
    except (ValueError, OSError):
        return None
