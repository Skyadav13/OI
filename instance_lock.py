"""
Prevents two copies of the bot running simultaneously (cron overlap, manual
run while scheduled one's active) - locked as a required safety piece.
Stale locks (PID no longer running, e.g. after a crash) are auto-cleared.
"""
import os
import sys
import logging

logger = logging.getLogger("instance_lock")


class InstanceLock:
    def __init__(self, path: str):
        self.path = path
        self._acquired = False

    def _pid_running(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
        except OverflowError:
            return False

    def acquire(self):
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    old_pid = int(f.read().strip())
            except (ValueError, OSError):
                old_pid = None

            if old_pid and self._pid_running(old_pid):
                logger.error("Another instance is already running (PID %d). Refusing to start.", old_pid)
                sys.exit(1)
            else:
                logger.warning("Stale lock file found (PID %s not running) - clearing and proceeding.", old_pid)
                os.remove(self.path)

        with open(self.path, "w") as f:
            f.write(str(os.getpid()))
        self._acquired = True
        logger.info("Instance lock acquired (PID %d).", os.getpid())

    def release(self):
        if self._acquired and os.path.exists(self.path):
            try:
                os.remove(self.path)
                logger.info("Instance lock released.")
            except OSError as exc:
                logger.warning("Could not remove lock file on exit: %s", exc)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
