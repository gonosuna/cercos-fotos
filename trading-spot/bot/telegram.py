"""Telegram minimo por long-polling. Solo atiende al chat autorizado (TELEGRAM_CHAT_ID)."""
import requests


class Telegram:
    def __init__(self, token, chat_id=""):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id or "")

    def _call(self, method, **params):
        r = requests.post(f"{self.base}/{method}", json=params, timeout=params.get("timeout", 10) + 15)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data}")
        return data["result"]

    def get_updates(self, offset, timeout=10):
        return self._call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"])

    def send(self, text, buttons=None, chat_id=None):
        p = dict(chat_id=chat_id or self.chat_id, text=text)
        if buttons:
            p["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in buttons]]}
        return self._call("sendMessage", **p)["message_id"]

    def edit(self, message_id, text):
        try:
            self._call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text)
        except Exception:
            pass

    def answer(self, callback_id, text=""):
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception:
            pass
