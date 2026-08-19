"""
Run this via its own cron entry, separate from the bot process itself -
e.g. every 5 minutes during market hours:
    */5 9-15 * * 1-5 /usr/bin/python3 /path/to/heartbeat_check.py

For genuine dead-man's-switch redundancy this should run from a DIFFERENT
host than the bot itself (see heartbeat.py docstring) - a small free VM,
an external cron-ping service, etc. Running it on the same VM as the bot
still catches a hung/crashed-but-not-restarted process, just not a full
VM outage.
"""
import sys
from config import Config
from telegram_client import TelegramClient
from heartbeat import read_heartbeat_age_seconds

STALE_THRESHOLD_SECONDS = 180   # ~3x default poll interval + buffer


def main():
    cfg = Config()
    age = read_heartbeat_age_seconds(cfg.heartbeat_file)

    if age is None:
        message = "🚨 Bot heartbeat file not found - the bot may never have started, or is on a different path."
    elif age > STALE_THRESHOLD_SECONDS:
        message = f"🚨 Bot heartbeat is stale ({age:.0f}s old, threshold {STALE_THRESHOLD_SECONDS}s) - process may be dead or hung."
    else:
        return   # healthy, no alert needed

    telegram = TelegramClient(cfg)
    telegram.send(message)
    print(message)
    sys.exit(1)


if __name__ == "__main__":
    main()
