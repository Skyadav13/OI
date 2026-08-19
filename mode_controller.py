"""
Command vocabulary (all via Telegram, chat-restricted to the configured
chat_id by TelegramClient):
    PAPER    - switch to PAPER, immediate, no friction
    LIVE     - request switch to LIVE, requires CONFIRM within the window
    CONFIRM  - confirms a pending LIVE request
    STOP     - kill switch: halts new entries + force-closes any open
               position, in either mode, no confirm-again friction
    RESUME   - clears a STOP state, allows new entries again

Mode persists indefinitely once set (locked design) - no daily ping, no
forced reconfirmation, only changes on an explicit command here.
"""
import logging
import datetime

from telegram_client import TelegramClient

logger = logging.getLogger("mode_controller")

CONFIRM_WINDOW_SECONDS = 300   # 5 min to send CONFIRM after requesting LIVE


class ModeController:
    def __init__(self, cfg, state, telegram: TelegramClient):
        self.cfg = cfg
        self.state = state
        self.telegram = telegram
        self._pending_live_request_ts = None

    # ---------- day rollover ----------
    def check_day_rollover(self):
        today = datetime.date.today().isoformat()
        if self.state.get("current_date") != today:
            logger.info("New trading day (%s) - resetting daily counters.", today)
            self.state.update(
                current_date=today, trades_today_count=0, loss_today=0.0,
                cooldown_until=None, closed_positions_today=[],
            )
            # STOP state does NOT auto-clear on rollover - the whole point of
            # a kill switch is that it stays halted until you explicitly say
            # otherwise, a new day silently un-halting it would defeat that.

    # ---------- command dispatch ----------
    def poll_and_dispatch(self):
        messages = self.telegram.poll_new_messages()
        for text in messages:
            self._handle(text.strip().upper())

        if self._pending_live_request_ts and \
                (datetime.datetime.now().timestamp() - self._pending_live_request_ts) > CONFIRM_WINDOW_SECONDS:
            self._pending_live_request_ts = None
            self.telegram.send("LIVE mode request expired (no CONFIRM received in time). Still PAPER.")

    def _handle(self, command: str):
        if command == "PAPER":
            self._set_mode("PAPER")
            self._pending_live_request_ts = None
            self.telegram.send("Mode set to PAPER. Effective on next restart.")

        elif command == "LIVE":
            if self.state.get("current_mode") == "LIVE":
                self.telegram.send("Already in LIVE mode.")
                return
            self._pending_live_request_ts = datetime.datetime.now().timestamp()
            self.telegram.send(
                "LIVE mode requested. This arms real order placement. "
                "Reply CONFIRM within 5 minutes to proceed, or ignore to stay in PAPER."
            )

        elif command == "CONFIRM":
            if self._pending_live_request_ts is None:
                self.telegram.send("No pending LIVE request to confirm.")
                return
            self._set_mode("LIVE")
            self._pending_live_request_ts = None
            self.telegram.send(
                "Mode set to LIVE. Effective on next restart. "
                "Confirm the SEBI Algo-ID is registered with Kotak before restarting - "
                "the bot will not place real orders correctly without it."
            )

        elif command == "STOP":
            self.state.set("stop_requested", True)
            self.telegram.send("STOP received. Halting new entries and closing any open position now.")

        elif command == "RESUME":
            self.state.set("stop_requested", False)
            self.telegram.send("Resumed. New entries allowed again.")

        elif command.startswith("APPLY "):
            self._handle_apply(command)

        else:
            logger.info("Unrecognized Telegram command: %s", command)

    def _handle_apply(self, command: str):
        from weekly_analytics import apply_param_change
        parts = command.split()
        if len(parts) != 3:
            self.telegram.send("Usage: APPLY <param> <value>")
            return
        _, param, raw_value = parts
        param = param.lower()
        try:
            value = float(raw_value) if "." in raw_value else int(raw_value)
        except ValueError:
            self.telegram.send(f"Could not parse value '{raw_value}' as a number.")
            return

        ok, message = apply_param_change(
            self.state, self.cfg, param, value,
            reason="Manually applied via Telegram APPLY command.", source="manual",
        )
        self.telegram.send(("Applied: " if ok else "Rejected: ") + message)

    def _set_mode(self, mode: str):
        self.state.update(current_mode=mode, mode_set_date=datetime.date.today().isoformat())

    def is_stopped(self) -> bool:
        return bool(self.state.get("stop_requested", False))

    def current_mode(self) -> str:
        return self.state.get("current_mode", self.cfg.default_mode)
