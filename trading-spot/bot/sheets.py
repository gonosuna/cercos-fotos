"""Envia eventos a la planilla de Google Sheets via Apps Script (apps_script/Code.gs). Con reintentos (outbox)."""
import json, pathlib
import requests


class Sheets:
    def __init__(self, url, secret, outbox_path):
        self.url, self.secret = url, secret
        self.path = pathlib.Path(outbox_path)
        self.outbox = json.loads(self.path.read_text("utf-8")) if self.path.exists() else []

    def _save(self):
        self.path.write_text(json.dumps(self.outbox[-500:], ensure_ascii=False), "utf-8")

    def push(self, event):
        self.outbox.append(event)
        self.flush()

    def flush(self):
        if not self.url:
            self._save(); return
        while self.outbox:
            try:
                r = requests.post(self.url, json=dict(secret=self.secret, **self.outbox[0]), timeout=30)
                if r.status_code != 200 or '"ok":true' not in r.text.replace(" ", ""):
                    break
            except requests.RequestException:
                break
            self.outbox.pop(0)
        self._save()
