"""Arranque del bot: python run.py   (ver README.md)"""
import logging, os, pathlib, sys, time, json
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from binance import Binance
from bot import Bot
from sheets import Sheets
from telegram import Telegram


def load_env(path):
    for line in path.read_text("utf-8").splitlines() if path.exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env(HERE / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(HERE / "bot.log", encoding="utf-8")])
    log = logging.getLogger("run")
    cfg = json.loads((HERE / "config.json").read_text("utf-8"))
    mode = cfg["mode"]
    if mode == "real" and not cfg["real_confirmed"]:
        sys.exit("Modo REAL bloqueado: primero pasar el backtest y la testnet, y poner real_confirmed=true en config.json.")
    pre = "BINANCE" if mode == "real" else "TESTNET"
    key, secret = os.environ.get(f"{pre}_API_KEY", ""), os.environ.get(f"{pre}_API_SECRET", "")
    if not key or not secret or not os.environ.get("TELEGRAM_TOKEN"):
        sys.exit(f"Falta completar .env ({pre}_API_KEY, {pre}_API_SECRET, TELEGRAM_TOKEN). Ver README.md")
    ex = Binance(mode, key, secret)
    ex.sync_time()
    if mode == "real" and ex.withdrawals_enabled():
        sys.exit("Tu clave API de Binance tiene PERMISO DE RETIROS. Creá otra sin ese permiso (solo lectura + trading spot).")
    ex.balances()  # prueba de conexion y de permisos
    tg = Telegram(os.environ["TELEGRAM_TOKEN"], os.environ.get("TELEGRAM_CHAT_ID", ""))
    sheets = Sheets(os.environ.get("SHEETS_URL", ""), os.environ.get("SHEETS_SECRET", ""), HERE / "outbox.json")
    bot = Bot(cfg, ex, tg, sheets, HERE)
    if tg.chat_id:
        bot.say(f"🤖 Bot iniciado — modo {mode.upper()}, control {bot.st['control']}. /ayuda para ver comandos.")
    else:
        log.warning("Sin TELEGRAM_CHAT_ID: mandale /start al bot en Telegram para ver tu chat id.")
    last_tick, errors = 0.0, 0
    while True:
        try:
            for upd in tg.get_updates(bot.st["tg_offset"], timeout=10):
                bot.st["tg_offset"] = upd["update_id"] + 1
                bot.handle(upd)
                bot.save()
            if tg.chat_id and time.time() - last_tick >= cfg["poll_seconds"]:
                bot.tick(); last_tick = time.time(); errors = 0
        except KeyboardInterrupt:
            log.info("Detenido por el usuario."); bot.save(); break
        except Exception as e:  # no caer por un error de red
            errors += 1
            log.exception("Error en el ciclo (%s)", e)
            if errors == 5:
                bot.say("⚠️ El bot tiene errores repetidos (revisá bot.log). Sigue intentando.")
            time.sleep(min(60, 5 * errors))


if __name__ == "__main__":
    main()
