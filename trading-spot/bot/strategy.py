"""Logica pura de la estrategia (sin red ni estado): indicadores, entrada, DCA y venta con trailing.
Las formulas son las MISMAS que uso el backtest (backtest/backtest.py)."""


def rsi14(closes, n=14):
    if len(closes) < n + 2:
        return None
    up = dn = None
    for prev, cur in zip(closes, closes[1:]):
        d = cur - prev
        u, w = max(d, 0.0), max(-d, 0.0)
        up = u if up is None else (1 - 1 / n) * up + (1 / n) * u
        dn = w if dn is None else (1 - 1 / n) * dn + (1 / n) * w
    if dn == 0:
        return 100.0
    return 100 - 100 / (1 + up / dn)


def daily_features(klines):
    """klines: velas diarias CERRADAS, mas viejas primero ([t,o,h,l,c,v,ct,qv,...]). Usa hasta el dia anterior."""
    if len(klines) < 40:
        return None
    h = [float(k[2]) for k in klines]; l = [float(k[3]) for k in klines]
    c = [float(k[4]) for k in klines]; qv = [float(k[7]) for k in klines]
    vol20 = sum(qv[-20:]) / 20
    vmax5 = max(qv[-5:])
    spike = vmax5 > 2 * vol20
    cooled = (not spike) or (qv[-1] <= 0.8 * vmax5 and l[-1] >= l[-2])
    return dict(rsi=rsi14(c), hi30=max(h[-30:]), cooled=cooled, spike=spike, vol_ratio=qv[-1] / vol20)


def entry_signal(feat, price, cfg):
    """Compra por caida: precio >= entry_drop bajo el maximo de 30 dias, RSI sobrevendido y pico de volumen ya enfriado."""
    if not feat or feat["rsi"] is None:
        return None
    dd = price / feat["hi30"] - 1
    if dd <= -cfg["entry_drop"] and feat["rsi"] < cfg["entry_rsi"] and feat["cooled"]:
        return dict(drop=dd, rsi=feat["rsi"], vol_ratio=feat["vol_ratio"])
    return None


def tranche_sizes(cfg):
    budget = cfg["capital"] * cfg["budget_pct"]
    sizes = [max(cfg["min_usdt"], w * budget) for w in cfg["tranche_w"]]
    sizes[-1] = max(cfg["min_usdt"], budget - sum(sizes[:-1]))
    return sizes


def dca_signal(pos, price, cfg):
    n = pos["n"]
    if n < len(cfg["dca_drops"]) and price <= pos["first"] * (1 - cfg["dca_drops"][n]):
        return dict(tranche=n + 1, drop=price / pos["first"] - 1)
    return None


def levels(pos, cfg):
    """Precios de activacion (+act neto) y piso (+floor neto), ya descontadas comisiones y deslizamiento."""
    k = 1 / (pos["qty"] * (1 - cfg["fee"]) * (1 - cfg["slip"]))
    return pos["cost"] * (1 + cfg["act"]) * k, pos["cost"] * (1 + cfg["floor"]) * k


def sell_check(pos, price, cfg):
    """Actualiza pos (act/peak) con el precio observado. Devuelve None o dict(reason, stop) si hay que vender."""
    lvl_act, lvl_floor = levels(pos, cfg)
    if not pos["act"]:
        if price >= lvl_act:
            pos["act"], pos["peak"] = True, price
        return None
    stop = max(pos["peak"] * (1 - cfg["trail"]), lvl_floor)
    if price <= stop:
        return dict(reason="trailing" if stop > lvl_floor else "piso +%.1f%%" % (cfg["floor"] * 100), stop=stop)
    pos["peak"] = max(pos["peak"], price)
    return None


def net_pct(pos, price, cfg):
    return pos["qty"] * price * (1 - cfg["fee"]) / pos["cost"] - 1
