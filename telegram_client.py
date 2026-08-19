import logging
import requests

logger = logging.getLogger("telegram_client")


class TelegramClient:
    def __init__(self, cfg):
        self.token = cfg.telegram_token
        self.chat_id = cfg.telegram_chat_id
        self._base = f"https://api.telegram.org/bot{self.token}"
        self._last_update_id = 0

    def send(self, text: str):
        try:
            resp = requests.post(f"{self._base}/sendMessage",
                                  json={"chat_id": self.chat_id, "text": text}, timeout=10)
            if not resp.ok:
                logger.warning("Telegram send failed: HTTP %d %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram send exception: %s", exc)

    def poll_new_messages(self) -> list:
        """Long-poll-free simple fetch - call once per main loop iteration.
        Returns list of new message texts from the configured chat only."""
        try:
            resp = requests.get(f"{self._base}/getUpdates",
                                 params={"offset": self._last_update_id + 1, "timeout": 0}, timeout=10)
            if not resp.ok:
                return []
            updates = resp.json().get("result", [])
        except Exception as exc:
            logger.warning("Telegram getUpdates failed: %s", exc)
            return []

        texts = []
        for u in updates:
            self._last_update_id = max(self._last_update_id, u.get("update_id", 0))
            msg = u.get("message", {})
            if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                continue
            text = msg.get("text", "").strip()
            if text:
                texts.append(text)
        return texts
