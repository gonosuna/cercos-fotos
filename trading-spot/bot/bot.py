"""Bot de trading spot (modo asistido por Telegram). Ver README.md.

Reglas: ventana lun-vie 10-18 hs Argentina para COMPRAS; ventas 24/7; max 3 compras/dia;
entrada por caida + DCA en 4 tramos; venta neta +5% con trailing desde +6,5%. Sin stop-loss.
"""
import csv, json, logging, os, pathlib, time
from datetime import datetime, timezone, timedelta

import strategy as S
from binance import BinanceError, summarize_fills

ART = timezone(timedelta(hours=-3))  # Argentina no tiene horario de verano
log = logging.getLogger("bot")


def norm(sym):
    s = sym.upper().strip()
    return s if s.endswith("USDT") else s + "USDT"


class Bot:
    def __init__(self, cfg, ex, tg, sheets, folder, clock=time.time):
        self.cfg, self.ex, self.tg, self.sheets, self.clock = cfg, ex, tg, sheets, clock
        self.dir = pathlib.Path(folder)
        self.spath, self.dpath = self.dir / "state.json", self.dir / "decisions.csv"
        self.sizes = S.tranche_sizes(cfg)
        self.feat_cache, self.last_prices, self.warned = {}, {}, set()
        self.st = dict(positions={}, day=None, buys=0, props=0, bought=[], pending={}, cooldown={}, realized=0.0,
                       trades=[], paused=False, control=cfg["control"], next_id=1, tg_offset=0)
        if self.spath.exists():
            self.st.update(json.loads(self.spath.read_text("utf-8")))

    # ------------------------------------------------------------ utilidades
    def now(self):
        return datetime.fromtimestamp(self.clock(), ART)

    def in_window(self):
        n = self.now()
        return n.weekday() in self.cfg["weekdays"] and self.cfg["window_from_hour"] <= n.hour < self.cfg["window_to_hour"]

    def save(self):
        tmp = self.spath.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.spath)

    def say(self, text, buttons=None):
        try:
            return self.tg.send(text, buttons)
        except Exception as e:
            log.warning("Telegram no disponible: %s", e)
            return None

    def log_decision(self, p, decision):
        new = not self.dpath.exists()
        i = p.get("info", {})
        with open(self.dpath, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["fecha", "id", "tipo", "par", "precio", "caida_pct", "rsi", "vol_ratio", "tramo", "decision"])
            w.writerow([self.now().isoformat(timespec="seconds"), p["id"], p["kind"], p["sym"], p["price"],
                        round(i.get("drop", 0) * 100, 2), round(i.get("rsi") or 0, 1), round(i.get("vol_ratio") or 0, 2),
                        i.get("tranche", ""), decision])

    def invested(self):
        return sum(p["cost"] for p in self.st["positions"].values())

    def available(self):
        reserved = sum(p["usdt"] for p in self.st["pending"].values() if p["kind"] in ("ENTRY", "DCA", "MANUAL_BUY"))
        return self.cfg["capital"] + self.st["realized"] - self.invested() - reserved

    def equity(self):
        fee = self.cfg["fee"]
        unreal = sum(p["qty"] * self.last_prices.get(s, p["first"]) * (1 - fee) - p["cost"]
                     for s, p in self.st["positions"].items())
        return self.cfg["capital"] + self.st["realized"] + unreal

    def in_cooldown(self, key):
        until = self.st["cooldown"].get(key)
        return bool(until and until > self.clock())

    def features(self, sym):
        t, f = self.feat_cache.get(sym, (0, None))
        if t and self.clock() - t < 6 * 3600:
            return f
        try:
            k = self.ex.klines(sym, "1d", 120)
        except BinanceError as e:
            if sym not in self.warned:
                log.warning("Sin datos de %s (%s): se omite", sym, e)
                self.warned.add(sym)
            self.feat_cache[sym] = (self.clock(), None)
            return None
        if k and k[-1][6] > self.clock() * 1000:  # la vela de hoy sigue abierta: no se usa
            k = k[:-1]
        f = S.daily_features(k)
        self.feat_cache[sym] = (self.clock(), f)
        return f

    # ------------------------------------------------------------ ciclo
    def tick(self):
        self.rollover()
        cfg = self.cfg
        syms = sorted(set(cfg["universe"]) | set(self.st["positions"]))
        prices = self.ex.prices(syms)
        self.last_prices = prices
        self.expire_pending()
        self.check_sells(prices)
        if not self.st["paused"] and self.in_window():
            self.scan_buys(prices)
        self.sheets.flush()
        self.save()

    def rollover(self):
        today = self.now().date().isoformat()
        if self.st["day"] == today:
            return
        if self.st["day"]:
            self.daily_summary(self.st["day"])
        self.st.update(day=today, buys=0, props=0, bought=[])

    def daily_summary(self, day):
        tr = [t for t in self.st["trades"] if t["t"][:10] == day]
        sells = [t for t in tr if t["side"] == "SELL"]
        eq = self.equity()
        self.sheets.push(dict(
            tipo="diario", fecha=day, resultado_dia=round(sum(t["pnl"] for t in sells), 2),
            acumulado=round(self.st["realized"], 2), equity=round(eq, 2),
            pct_capital=round((eq / self.cfg["capital"] - 1) * 100, 2), invertido=round(self.invested(), 2),
            posiciones=len(self.st["positions"]), compras=len([t for t in tr if t["side"] == "BUY"]), ventas=len(sells)))

    # ------------------------------------------------------------ propuestas
    def pending_for(self, kind_prefix, sym):
        return any(p["sym"] == sym and p["kind"].endswith(kind_prefix) for p in self.st["pending"].values())

    def propose(self, kind, sym, price, usdt=0.0, info=None, text=""):
        pid = str(self.st["next_id"]); self.st["next_id"] += 1
        p = dict(id=pid, kind=kind, sym=sym, price=price, usdt=usdt, info=info or {}, created=self.clock())
        self.st["pending"][pid] = p
        p["msg"] = self.say(text, [("✅ Sí", f"y:{pid}"), ("❌ No", f"n:{pid}")])
        return p

    def expire_pending(self):
        ttl = self.cfg["proposal_ttl_min"] * 60
        for pid, p in list(self.st["pending"].items()):
            if self.clock() - p["created"] > ttl:
                del self.st["pending"][pid]
                self.log_decision(p, "expirada")
                self.set_cooldown(p)
                if p.get("msg"):
                    self.tg.edit(p["msg"], f"⌛ Sin respuesta ({p['sym']}): no hice nada.")

    def set_cooldown(self, p):
        side = "SELL" if p["kind"].endswith("SELL") else "BUY"
        secs = self.cfg["sell_cooldown_min"] * 60 if side == "SELL" else self.cfg["cooldown_hours"] * 3600
        if not p["kind"].startswith("MANUAL"):
            self.st["cooldown"][f"{side}:{p['sym']}"] = self.clock() + secs

    def check_sells(self, prices):
        for sym, pos in list(self.st["positions"].items()):
            price = prices.get(sym)
            if pos.get("hold") or price is None:
                continue
            sig = S.sell_check(pos, price, self.cfg)
            if not sig or self.pending_for("SELL", sym) or self.in_cooldown(f"SELL:{sym}"):
                continue
            net = S.net_pct(pos, price, self.cfg)
            info = dict(reason=sig["reason"], net=net)
            if self.cfg["sell_requires_confirm"] and self.st["control"] == "asistido":
                self.propose("SELL", sym, price, info=info,
                             text=f"💰 VENDER {sym}\nMotivo: {sig['reason']}\nPrecio {price:g} | ganancia neta {net*100:+.2f}%\n"
                                  f"Posición: {pos['n']} tramo(s), costo {pos['cost']:.2f} USDT")
            else:
                self.exec_sell(sym, sig["reason"], "automatico")

    def scan_buys(self, prices):
        cfg, st = self.cfg, self.st
        pend_buys = sum(1 for p in st["pending"].values() if p["kind"] in ("ENTRY", "DCA"))
        cands = []
        for sym in cfg["universe"]:
            price = prices.get(sym)
            if price is None:
                continue
            if self.in_cooldown(f"BUY:{sym}") or any(p["sym"] == sym and p["kind"] in ("ENTRY", "DCA") for p in st["pending"].values()):
                continue
            pos = st["positions"].get(sym)
            if pos:
                if pos.get("hold") or sym in st["bought"]:
                    continue
                sig = S.dca_signal(pos, price, cfg)
                if sig:
                    cands.append((sig["drop"], sym, "DCA", dict(sig, size=self.sizes[pos["n"]])))
            else:
                sig = S.entry_signal(self.features(sym), price, cfg)
                if sig:
                    cands.append((sig["drop"], sym, "ENTRY", dict(sig, size=self.sizes[0], tranche=1)))
        for _, sym, kind, sig in sorted(cands, key=lambda c: c[0]):
            if st["buys"] + pend_buys >= cfg["max_buys_day"] or st["props"] >= cfg["max_proposals_day"]:
                break
            size, price = sig["size"], prices[sym]
            invested_sym = st["positions"].get(sym, {}).get("cost", 0.0)
            if invested_sym + size > cfg["capital"] * cfg["budget_pct"] + 0.01 or size > self.available():
                continue
            st["props"] += 1
            auto = st["control"] == "automatico"
            info = dict(sig, tranche=sig.get("tranche", 0))
            if auto:
                self.exec_buy(sym, size, kind, info, "automatico")
            else:
                pend_buys += 1
                label = "Primera compra" if kind == "ENTRY" else f"DCA tramo {sig['tranche']}/4"
                extra = "" if kind == "DCA" else f"\nRSI diario {sig['rsi']:.0f} | volumen {sig['vol_ratio']:.1f}x del promedio"
                self.propose(kind, sym, price, size, info,
                             f"🟢 COMPRAR {sym} — {label}\nPrecio {price:g} | {sig['drop']*100:+.1f}% "
                             f"{'vs máx. 30d' if kind == 'ENTRY' else 'vs 1ª compra'}{extra}\nMonto: {size:.0f} USDT")

    # ------------------------------------------------------------ respuestas
    def decide(self, pid, yes):
        p = self.st["pending"].pop(pid, None)
        if not p:
            self.say("Esa propuesta ya no está vigente.")
            return
        if not yes:
            self.log_decision(p, "no"); self.set_cooldown(p)
            self.tg.edit(p.get("msg"), f"❌ Rechazada: {p['kind']} {p['sym']}")
            self.save(); return
        try:
            price = self.ex.prices([p["sym"]])[p["sym"]]
        except Exception as e:
            self.say(f"No pude leer el precio de {p['sym']}: {e}"); return
        buy = p["kind"] in ("ENTRY", "DCA", "MANUAL_BUY")
        if buy:
            if p["kind"] != "MANUAL_BUY" and not self.in_window():
                self.log_decision(p, "fuera_de_ventana")
                self.tg.edit(p.get("msg"), f"⏰ Fuera del horario de compra (lun-vie 10-18): {p['sym']} cancelada."); return
            if abs(price / p["price"] - 1) > self.cfg["price_tolerance"]:
                self.log_decision(p, "precio_cambio")
                self.tg.edit(p.get("msg"), f"↔️ {p['sym']} se movió más de {self.cfg['price_tolerance']*100:.0f}%: cancelada, se vuelve a evaluar."); return
            if p["kind"] != "MANUAL_BUY" and self.st["buys"] >= self.cfg["max_buys_day"]:
                self.say("Ya se usaron las compras del día."); return
        self.log_decision(p, "si")
        self.tg.edit(p.get("msg"), f"✅ Aprobada: {p['kind']} {p['sym']}")
        mode = "manual" if p["kind"].startswith("MANUAL") else "asistido"
        if buy:
            self.exec_buy(p["sym"], p["usdt"], p["kind"], p["info"], mode)
        else:
            self.exec_sell(p["sym"], p["info"].get("reason", "manual"), mode)
        self.save()

    # ------------------------------------------------------------ ejecucion
    def record(self, sym, side, kind, o, pnl, pnl_pct, tranche, mode, reason=""):
        now = self.now()
        t = dict(t=now.isoformat(timespec="seconds"), sym=sym, side=side, kind=kind, price=o["avg"], qty=o["qty"],
                 usdt=o["quote"], fee=o["fee_total"], pnl=pnl, pnl_pct=pnl_pct, tranche=tranche, mode=mode)
        self.st["trades"].append(t)
        self.sheets.push(dict(tipo="operacion", fecha=now.date().isoformat(), hora=now.strftime("%H:%M:%S"), par=sym,
                              lado=side, motivo=reason or kind, precio=o["avg"], cantidad=o["qty"], usdt=round(o["quote"], 2),
                              comision=round(o["fee_total"], 4), pnl_usdt=round(pnl, 2), pnl_pct=round(pnl_pct * 100, 2),
                              tramo=tranche, modo=self.cfg["mode"], decision=mode))

    def exec_buy(self, sym, usdt, kind, info, mode):
        try:
            f = self.ex.filters(sym)
            if usdt < f["min_notional"]:
                raise BinanceError(f"monto {usdt} menor al mínimo {f['min_notional']}")
            if self.ex.balances().get("USDT", 0) < usdt:
                raise BinanceError("USDT libre insuficiente en la cuenta")
            o = summarize_fills(self.ex.market_buy(sym, usdt), f["base"], self.cfg["fee"])
        except BinanceError as e:
            self.say(f"⚠️ No pude comprar {sym}: {e}"); return
        cost = o["quote"] + o["fee_extra"]
        pos = self.st["positions"].get(sym)
        if pos:
            pos["qty"] += o["qty"]; pos["cost"] += cost
            if kind != "MANUAL_BUY":
                pos["n"] += 1
        else:
            self.st["positions"][sym] = dict(qty=o["qty"], cost=cost, n=1, first=o["avg"], act=False, peak=0.0,
                                             open_t=self.now().isoformat(timespec="seconds"), hold=kind == "MANUAL_BUY")
        self.st["buys"] += 1
        self.st["bought"].append(sym)
        self.record(sym, "BUY", kind, o, 0.0, 0.0, info.get("tranche", 0), mode)
        pos = self.st["positions"][sym]
        self.say(f"✅ Compré {sym}: {o['qty']:g} a {o['avg']:g} ({o['quote']:.2f} USDT). "
                 f"Tramo {pos['n']}/4. Promedio {pos['cost']/pos['qty']:.6g}.")
        self.save()

    def exec_sell(self, sym, reason, mode):
        pos = self.st["positions"].get(sym)
        if not pos:
            self.say(f"No tengo posición en {sym}."); return
        try:
            f = self.ex.filters(sym)
            qty = min(pos["qty"], self.ex.balances().get(f["base"], 0.0))
            o = summarize_fills(self.ex.market_sell(sym, qty), f["base"], self.cfg["fee"])
        except BinanceError as e:
            self.say(f"⚠️ No pude vender {sym}: {e}"); return
        proceeds = o["quote"] - o["fee_total"]
        pnl = proceeds - pos["cost"]
        self.st["realized"] += pnl
        del self.st["positions"][sym]
        self.record(sym, "SELL", "SELL", o, pnl, pnl / pos["cost"], pos["n"], mode, reason)
        self.say(f"💰 Vendí {sym}: {o['qty']:g} a {o['avg']:g}. Resultado {pnl:+.2f} USDT ({pnl/pos['cost']*100:+.2f}% neto). "
                 f"Acumulado {self.st['realized']:+.2f} USDT.")
        self.save()

    # ------------------------------------------------------------ Telegram
    def handle(self, upd):
        if "callback_query" in upd:
            cq = upd["callback_query"]
            if str(cq["message"]["chat"]["id"]) != self.tg.chat_id:
                return
            self.tg.answer(cq["id"])
            op, pid = cq["data"].split(":", 1)
            self.decide(pid, op == "y")
        elif "message" in upd and "text" in upd["message"]:
            m = upd["message"]
            chat = str(m["chat"]["id"])
            if chat != self.tg.chat_id:
                if m["text"].startswith("/start"):
                    self.tg.send(f"Tu chat id es {chat}. Pegalo en el archivo .env como TELEGRAM_CHAT_ID y reiniciá el bot.", chat_id=chat)
                return
            reply = self.command(m["text"])
            if reply:
                self.say(reply)

    def command(self, text):
        a = text.strip().split()
        if not a:
            return None
        c, args, st, cfg = a[0].lower().split("@")[0], a[1:], self.st, self.cfg
        if c in ("/start", "/ayuda", "/help"):
            return ("Comandos:\n/estado — posiciones y límites del día\n/reporte — resultados\n/pausa y /reanudar — frena o retoma propuestas de compra\n"
                    "/modo asistido | /modo automatico si\n/congelar UNI — el bot no toca esa cripto (manual)\n/liberar UNI — vuelve al bot\n"
                    "/comprar UNI 150 — compra manual (pide confirmación)\n/vender UNI — venta manual (pide confirmación)")
        if c == "/estado":
            lines = [f"Modo: {cfg['mode']} | control: {st['control']}{' | EN PAUSA' if st['paused'] else ''}",
                     f"Ventana de compra: {'abierta' if self.in_window() else 'cerrada'} | compras hoy {st['buys']}/{cfg['max_buys_day']}",
                     f"Capital {cfg['capital']} | disponible {self.available():.2f} | acumulado {st['realized']:+.2f} | equity {self.equity():.2f}"]
            for s, p in st["positions"].items():
                pr = self.last_prices.get(s)
                net = f"{S.net_pct(p, pr, cfg)*100:+.1f}%" if pr else "?"
                lines.append(f"• {s}: tramo {p['n']}/4, costo {p['cost']:.0f}, neto {net}{' [CONGELADA]' if p.get('hold') else ''}{' [trailing]' if p['act'] else ''}")
            if not st["positions"]:
                lines.append("Sin posiciones (100% USDT).")
            if st["pending"]:
                lines.append(f"Propuestas pendientes: {len(st['pending'])}")
            return "\n".join(lines)
        if c == "/reporte":
            tr = st["trades"]; today = self.now().date().isoformat(); month = today[:7]
            sells = [t for t in tr if t["side"] == "SELL"]
            f = lambda pre: sum(t["pnl"] for t in sells if t["t"].startswith(pre))
            return (f"Hoy: {f(today):+.2f} USDT | Mes: {f(month):+.2f} USDT | Total realizado: {st['realized']:+.2f} USDT\n"
                    f"Equity {self.equity():.2f} ({(self.equity()/cfg['capital']-1)*100:+.2f}% sobre {cfg['capital']}) | "
                    f"{len([t for t in tr if t['side']=='BUY'])} compras, {len(sells)} ventas")
        if c == "/pausa":
            st["paused"] = True; self.save(); return "⏸ En pausa: no propongo compras nuevas (las ventas siguen)."
        if c == "/reanudar":
            st["paused"] = False; self.save(); return "▶️ Reanudado."
        if c == "/modo":
            if args[:1] == ["asistido"]:
                st["control"] = "asistido"; self.save(); return "Modo ASISTIDO: te consulto cada operación."
            if args[:1] == ["automatico"]:
                if args[1:2] != ["si"]:
                    return "El modo automático opera SIN preguntarte. Para activarlo mandá: /modo automatico si"
                st["control"] = "automatico"; self.save(); return "⚠️ Modo AUTOMÁTICO activado. /modo asistido para volver."
            return "Usá /modo asistido o /modo automatico si"
        if c in ("/congelar", "/liberar") and args:
            s = norm(args[0])
            if s not in st["positions"]:
                return f"No tengo posición en {s}."
            st["positions"][s]["hold"] = c == "/congelar"; self.save()
            return f"{s}: {'CONGELADA (manual): no la compro ni la vendo hasta /liberar' if c == '/congelar' else 'liberada, vuelve a las reglas del bot'}."
        if c == "/comprar" and len(args) == 2:
            s = norm(args[0])
            try:
                usdt = float(args[1]); price = self.ex.prices([s])[s]
            except (ValueError, KeyError, BinanceError):
                return "Uso: /comprar UNI 150"
            if usdt < cfg["min_usdt"]:
                return f"El mínimo de compra es {cfg['min_usdt']} USDT."
            if usdt > self.available():
                return f"Solo hay {self.available():.2f} USDT disponibles."
            self.propose("MANUAL_BUY", s, price, usdt, dict(tranche=0),
                         f"🟢 COMPRA MANUAL {s}: {usdt:.0f} USDT a {price:g}. Queda CONGELADA (manual). ¿Confirmás?")
            return None
        if c == "/vender" and args:
            s = norm(args[0])
            if s not in st["positions"]:
                return f"No tengo posición en {s}."
            pr = self.ex.prices([s])[s]; p = st["positions"][s]
            self.propose("MANUAL_SELL", s, pr, 0, dict(reason="manual"),
                         f"💰 VENTA MANUAL {s} a {pr:g} (neto {S.net_pct(p, pr, cfg)*100:+.2f}%). ¿Confirmás?")
            return None
        return "No entendí. /ayuda para ver los comandos."
