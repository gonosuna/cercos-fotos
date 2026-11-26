"""Pruebas con un exchange, Telegram y reloj simulados (no tocan Binance). Correr: python -m unittest discover -s tests"""
import json, pathlib, sys, tempfile, unittest
from datetime import datetime, timezone, timedelta
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import strategy as S
from bot import Bot
from binance import BinanceError

ART = timezone(timedelta(hours=-3))
CFG = json.loads((pathlib.Path(__file__).resolve().parent.parent / "config.json").read_text())
CFG["universe"] = ["UNIUSDT", "NEARUSDT"]


def ts(y, m, d, h=11, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ART).timestamp()


class FakeEx:
    def __init__(self, price=10.0):
        self.price, self.orders, self.free_usdt = {"UNIUSDT": price, "NEARUSDT": 3.0}, [], 5000.0
        self.hold = {}
        self.falling = True

    def klines(self, sym, interval, limit):  # 60 dias bajistas: ultimo cierre muy debajo del maximo, volumen enfriado
        out = []
        for i in range(60):
            c = 20.0 - i * 0.2 if i < 59 else 9.9
            out.append([0, c, c * 1.02, c * 0.98, c, 0, 10**15, 1000.0 if i < 55 else 800.0 - i, 0, 0, 0, 0])
        return out

    def prices(self, syms):
        return {s: p for s, p in self.price.items() if s in syms}

    def filters(self, sym):
        return dict(base=sym[:-4], step=0.001, min_qty=0.001, min_notional=5.0)

    def balances(self):
        return dict(USDT=self.free_usdt, **self.hold)

    def market_buy(self, sym, usdt):
        p = self.price[sym]; qty = usdt / p; fee = usdt * 0.001
        self.free_usdt -= usdt; self.hold[sym[:-4]] = self.hold.get(sym[:-4], 0) + qty
        self.orders.append(("BUY", sym, usdt))
        return dict(executedQty=str(qty), cummulativeQuoteQty=str(usdt),
                    fills=[dict(price=str(p), qty=str(qty), commission=str(fee), commissionAsset="USDT")])

    def market_sell(self, sym, qty):
        p = self.price[sym]; quote = qty * p; fee = quote * 0.001
        self.free_usdt += quote; self.hold[sym[:-4]] -= qty
        self.orders.append(("SELL", sym, qty))
        return dict(executedQty=str(qty), cummulativeQuoteQty=str(quote),
                    fills=[dict(price=str(p), qty=str(qty), commission=str(fee), commissionAsset="USDT")])


class FakeTG:
    chat_id = "1"
    def __init__(self): self.sent, self.edits = [], []
    def send(self, text, buttons=None, chat_id=None):
        self.sent.append((text, buttons)); return len(self.sent)
    def edit(self, mid, text): self.edits.append(text)
    def answer(self, *a): pass


class FakeSheets:
    def __init__(self): self.events = []
    def push(self, e): self.events.append(e)
    def flush(self): pass


class Env:
    def __init__(self, when=ts(2026, 11, 25), cfg=None):  # miercoles 11:00 ART
        self.now = when
        self.tmp = tempfile.TemporaryDirectory()
        self.ex, self.tg, self.sh = FakeEx(), FakeTG(), FakeSheets()
        self.bot = Bot(dict(CFG, **(cfg or {})), self.ex, self.tg, self.sh, self.tmp.name, clock=lambda: self.now)

    def click(self, yes=True, idx=-1):
        pid = list(self.bot.st["pending"])[idx]
        self.bot.handle({"callback_query": {"id": "x", "data": f"{'y' if yes else 'n'}:{pid}", "message": {"chat": {"id": 1}}}})


class StrategyTests(unittest.TestCase):
    def test_rsi_extremos(self):
        self.assertEqual(S.rsi14([float(i) for i in range(1, 40)]), 100.0)
        self.assertLess(S.rsi14([float(i) for i in range(40, 1, -1)]), 1)

    def test_sell_state_machine(self):
        cfg = CFG
        pos = dict(qty=10.0, cost=100.0, n=1, first=10.0, act=False, peak=0.0)
        act, floor = S.levels(pos, cfg)
        self.assertGreater(act, floor)
        self.assertGreater(floor * 10 * (1 - cfg["fee"]) * (1 - cfg["slip"]), 100 * 1.0499)  # +5% neto
        self.assertIsNone(S.sell_check(pos, act * 0.99, cfg))          # todavia no activa
        self.assertIsNone(S.sell_check(pos, act * 1.00, cfg)); self.assertTrue(pos["act"])
        self.assertIsNone(S.sell_check(pos, act * 1.10, cfg))           # sigue subiendo: sube el pico
        sig = S.sell_check(pos, act * 1.10 * 0.98, cfg)                 # cae >1,5% desde el maximo
        self.assertEqual(sig["reason"], "trailing")

    def test_piso_duro(self):
        pos = dict(qty=10.0, cost=100.0, n=1, first=10.0, act=False, peak=0.0)
        act, floor = S.levels(pos, CFG)
        S.sell_check(pos, act, CFG)
        sig = S.sell_check(pos, floor * 0.999, CFG)
        self.assertIn("piso", sig["reason"])

    def test_tramos_suman_presupuesto(self):
        sizes = S.tranche_sizes(CFG)
        self.assertAlmostEqual(sum(sizes), 600.0); self.assertTrue(all(s >= 100 for s in sizes))


class BotTests(unittest.TestCase):
    def test_propone_y_compra_si_se_aprueba(self):
        e = Env(); e.bot.tick()
        self.assertEqual(len(e.bot.st["pending"]), 2)  # UNI y NEAR cumplen la senal
        self.assertEqual(e.ex.orders, [])              # nada se ejecuta sin respuesta
        e.click(True)
        self.assertEqual(len(e.ex.orders), 1); self.assertEqual(len(e.bot.st["positions"]), 1)
        self.assertEqual(e.sh.events[0]["lado"], "BUY")

    def test_sin_respuesta_no_hace_nada(self):
        e = Env(); e.bot.tick(); e.now += 21 * 60; e.bot.tick()
        self.assertEqual(e.ex.orders, []); self.assertEqual(e.bot.st["pending"], {})
        e.now += 60; e.bot.tick()
        self.assertEqual(e.bot.st["pending"], {})      # en cooldown: no insiste

    def test_no_compra_fin_de_semana_ni_fuera_de_horario(self):
        for when in (ts(2026, 11, 28, 12), ts(2026, 11, 25, 19)):
            e = Env(when); e.bot.tick(); self.assertEqual(e.bot.st["pending"], {})

    def test_dca_y_venta_con_ganancia(self):
        e = Env(cfg=dict(universe=["UNIUSDT"])); e.bot.tick(); e.click(True)
        pos = e.bot.st["positions"]["UNIUSDT"]; self.assertEqual(pos["n"], 1)
        e.now += 86400; e.ex.price["UNIUSDT"] = 9.2; e.bot.tick()          # -8%: DCA tramo 2
        self.assertEqual(e.bot.st["pending"] and list(e.bot.st["pending"].values())[0]["kind"], "DCA")
        e.click(True); self.assertEqual(pos["n"], 2)
        avg = pos["cost"] / pos["qty"]
        e.now += 3600 * 30; e.ex.price["UNIUSDT"] = avg * 1.075; e.bot.tick()  # activa trailing
        self.assertTrue(pos["act"]); self.assertEqual(e.bot.st["pending"], {})
        e.ex.price["UNIUSDT"] = avg * 1.075 * 0.98; e.bot.tick()
        self.assertEqual(list(e.bot.st["pending"].values())[0]["kind"], "SELL")
        e.click(True)
        self.assertEqual(e.bot.st["positions"], {})
        self.assertGreater(e.bot.st["realized"], 0)
        self.assertGreaterEqual(e.sh.events[-1]["pnl_pct"], 5.0)

    def test_venta_en_fin_de_semana_si(self):
        e = Env(cfg=dict(universe=["UNIUSDT"])); e.bot.tick(); e.click(True)
        pos = e.bot.st["positions"]["UNIUSDT"]; avg = pos["cost"] / pos["qty"]
        e.now = ts(2026, 11, 28, 15); e.ex.price["UNIUSDT"] = avg * 1.08; e.bot.tick()
        e.ex.price["UNIUSDT"] = avg * 1.05; e.bot.tick()
        self.assertEqual(list(e.bot.st["pending"].values())[0]["kind"], "SELL")

    def test_congelar_bloquea_ventas(self):
        e = Env(cfg=dict(universe=["UNIUSDT"])); e.bot.tick(); e.click(True)
        e.bot.command("/congelar UNI")
        avg = e.bot.st["positions"]["UNIUSDT"]["cost"] / e.bot.st["positions"]["UNIUSDT"]["qty"]
        e.ex.price["UNIUSDT"] = avg * 1.2; e.bot.tick(); e.ex.price["UNIUSDT"] = avg * 1.1; e.bot.tick()
        self.assertEqual(e.bot.st["pending"], {})

    def test_limite_de_compras_por_dia(self):
        e = Env(cfg=dict(max_buys_day=1)); e.bot.tick()
        self.assertEqual(len(e.bot.st["pending"]), 1)

    def test_tope_por_cripto_60pct(self):
        e = Env(cfg=dict(universe=["UNIUSDT"], capital=1000)); e.bot.tick(); e.click(True)
        pos = e.bot.st["positions"]["UNIUSDT"]; pos["cost"] = 550.0      # ya casi en el tope
        e.now += 86400; e.ex.price["UNIUSDT"] = 9.0; e.bot.tick()
        self.assertEqual(e.bot.st["pending"], {})

    def test_automatico_requiere_confirmar_el_comando(self):
        e = Env()
        self.assertIn("SIN preguntarte", e.bot.command("/modo automatico"))
        self.assertEqual(e.bot.st["control"], "asistido")
        e.bot.command("/modo automatico si"); self.assertEqual(e.bot.st["control"], "automatico")

    def test_precio_se_movio_cancela(self):
        e = Env(cfg=dict(universe=["UNIUSDT"])); e.bot.tick(); e.ex.price["UNIUSDT"] = 10.5; e.click(True)
        self.assertEqual(e.ex.orders, [])

    def test_chat_desconocido_ignorado(self):
        e = Env()
        e.bot.handle({"message": {"chat": {"id": 999}, "text": "/pausa"}})
        self.assertFalse(e.bot.st["paused"])

    def test_resumen_diario_a_planilla(self):
        e = Env(); e.bot.tick(); e.now += 86400; e.bot.tick()
        self.assertEqual(e.sh.events[-1]["tipo"], "diario")
        self.assertEqual(e.sh.events[-1]["fecha"], "2026-11-25")

    def test_estado_persiste(self):
        e = Env(cfg=dict(universe=["UNIUSDT"])); e.bot.tick(); e.click(True); e.bot.save()
        b2 = Bot(e.bot.cfg, e.ex, e.tg, e.sh, e.tmp.name, clock=lambda: e.now)
        self.assertIn("UNIUSDT", b2.st["positions"])


if __name__ == "__main__":
    unittest.main()
