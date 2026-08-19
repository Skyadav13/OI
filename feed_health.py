"""
Tracks per-broker feed health and provides the bounded exponential-backoff
retry used for transient reconnects (locked self-heal design). A broker
that exhausts all backoff attempts is NOT retried forever - that's the
line between "self-heal" (transient blip) and "heartbeat failure"
(escalates to degraded mode / alert), per locked design.
"""
import time
import logging

logger = logging.getLogger("feed_health")


class FeedHealthMonitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_success = {}     # {broker_name: timestamp}
        self.reconnect_counts_today = {}   # {broker_name: int} - surfaced in daily report per locked design

    def mark_success(self, broker_name: str):
        self.last_success[broker_name] = time.time()

    def is_stale(self, broker_name: str) -> bool:
        ts = self.last_success.get(broker_name)
        if ts is None:
            return True
        return (time.time() - ts) > self.cfg.heartbeat_timeout_seconds

    def seconds_since_success(self, broker_name: str):
        ts = self.last_success.get(broker_name)
        return (time.time() - ts) if ts else None

    def record_reconnect(self, broker_name: str):
        self.reconnect_counts_today[broker_name] = self.reconnect_counts_today.get(broker_name, 0) + 1

    def reset_daily_counts(self):
        self.reconnect_counts_today = {}


def retry_with_backoff(fn, backoff_seconds: tuple, broker_name: str, health: FeedHealthMonitor, *args, **kwargs):
    """Runs fn(*args, **kwargs); on exception, retries with the configured
    backoff schedule. Returns fn's result on success, None if all attempts
    are exhausted - caller treats None the same as a heartbeat failure,
    does not retry further itself."""
    last_exc = None
    for attempt, delay in enumerate([0] + list(backoff_seconds)):
        if delay:
            time.sleep(delay)
        try:
            result = fn(*args, **kwargs)
            health.mark_success(broker_name)
            if attempt > 0:
                health.record_reconnect(broker_name)
                logger.info("%s: recovered after %d retr%s.", broker_name, attempt, "y" if attempt == 1 else "ies")
            return result
        except Exception as exc:
            last_exc = exc
            logger.warning("%s: attempt %d failed: %s", broker_name, attempt + 1, exc)

    logger.error("%s: exhausted all reconnect attempts, last error: %s", broker_name, last_exc)
    return None
