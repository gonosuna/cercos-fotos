"""Backtest de la estrategia spot acordada (solo entrada por caida + DCA + trailing).

Reglas (ver README): lun-vie 10-18 hs Argentina (13-21 UTC), max 3 compras/dia,
tramos DCA 15/20/30/35% de un presupuesto del 60% del capital por cripto (min 100 USDT),
a 0/-7/-15/-25% del primer precio de compra. Venta: a +6,5% neto activa trailing
(1,5% desde el maximo), piso duro +5% neto. Ventas 24/7. Sin stop-loss.

Sin mirar el futuro: los indicadores diarios se calculan con velas diarias CERRADAS
(dia anterior). Comision 0,1% y deslizamiento 0,03% por lado.
Uso: python3 -I backtest.py [--drop 0.15] [--rsi 35]
"""
import json, sys, argparse, pathlib
import numpy as np, pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "backtest" / "results"

P = dict(
    capital=1000.0, budget_pct=0.60, tranche_w=(0.15, 0.20, 0.30, 0.35), min_usdt=100.0,
    dca_drops=(0.0, 0.07, 0.15, 0.25),
    entry_drop=0.15, entry_rsi=35.0, max_buys_day=3,
    act=0.065, floor=0.05, trail=0.015, fee=0.001, slip=0.0003,
    h_from=13, h_to=21,  # barras que empiezan 13..20 UTC = 10..17 ART
    warmup_days=35,
)


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def load():
    syms = json.loads((DATA / "universe.json").read_text())
    raw = {}
    for s in syms:
        df = pd.read_csv(DATA / f"{s}.csv")
        df.index = pd.to_datetime(df.t, unit="ms", utc=True)
        raw[s] = df
    idx = sorted(set().union(*[set(d.index) for d in raw.values()]))
    idx = pd.DatetimeIndex(idx)
    sig = {}
    for s, df in raw.items():
        d = df.resample("1D").agg({"o": "first", "h": "max", "l": "min", "c": "last", "qv": "sum"}).dropna()
        d["rsi"] = rsi(d.c)
        d["hi30"] = d.h.rolling(30).max()
        d["vol20"] = d.qv.rolling(20).mean()
        d["vmax5"] = d.qv.rolling(5).max()
        spike = d.vmax5 > 2 * d.vol20
        cooled = (~spike) | ((d.qv <= 0.8 * d.vmax5) & (d.l >= d.l.shift(1)))
        d["cooled"] = cooled.astype(float)
        d = d[["rsi", "hi30", "cooled"]].shift(1)  # solo dias cerrados
        h = df.reindex(idx)
        d = d.reindex(idx, method="ffill")
        h[["rsi", "hi30", "cooled"]] = d[["rsi", "hi30", "cooled"]].values
        sig[s] = h
    return idx, sig


def run(P, idx, sig, verbose=False):
    syms = list(sig)
    arr = {s: {k: sig[s][k].values for k in ("o", "h", "l", "c", "rsi", "hi30", "cooled")} for s in syms}
    fee, slip = P["fee"], P["slip"]
    budget = P["capital"] * P["budget_pct"]
    sizes = [max(P["min_usdt"], w * budget) for w in P["tranche_w"]]
    sizes[-1] = max(P["min_usdt"], budget - sum(sizes[:-1]))
    cash = P["capital"]
    pos = {}  # sym -> dict(qty, cost, n, first, act, peak, open_t, last_buy_day)
    trades, eq_rows = [], []
    day_buys, cur_day = 0, None
    warm_end = idx[0] + pd.Timedelta(days=P["warmup_days"])
    mult_buy, mult_sell = 1 + slip, 1 - slip

    for i, t in enumerate(idx):
        art = t - pd.Timedelta(hours=3)
        day = art.date()
        if day != cur_day:
            cur_day, day_buys = day, 0
            bought_today = set()
        # ---- ventas (24/7) ----
        for s in list(pos):
            a = arr[s]
            if np.isnan(a["c"][i]):
                continue
            p = pos[s]
            k = 1 / (p["qty"] * (1 - fee) * (1 - slip))
            lvl_act, lvl_floor = p["cost"] * (1 + P["act"]) * k, p["cost"] * (1 + P["floor"]) * k
            sold = None
            if p["act"]:
                stop = max(p["peak"] * (1 - P["trail"]), lvl_floor)
                if a["l"][i] <= stop:
                    fill = min(a["o"][i], stop) if a["o"][i] < stop else stop
                    sold = fill * mult_sell
                else:
                    p["peak"] = max(p["peak"], a["h"][i])
            elif a["h"][i] >= lvl_act:
                p["act"], p["peak"] = True, a["h"][i]
            if sold is not None:
                proceeds = p["qty"] * sold * (1 - fee)
                cash += proceeds
                trades.append(dict(t=t, sym=s, side="SELL", price=sold, usdt=proceeds, pnl=proceeds - p["cost"],
                                   pnl_pct=proceeds / p["cost"] - 1, tranches=p["n"],
                                   days=(t - p["open_t"]).total_seconds() / 86400))
                del pos[s]
        # ---- compras (solo ventana lun-vie 10-18 ART) ----
        in_win = art.weekday() < 5 and 10 <= art.hour < 18
        if in_win and t >= warm_end and day_buys < P["max_buys_day"]:
            cands = []
            for s in syms:
                a = arr[s]
                c = a["c"][i]
                if np.isnan(c) or np.isnan(a["hi30"][i]) or np.isnan(a["rsi"][i]):
                    continue
                if s in pos:
                    p = pos[s]
                    n = p["n"]
                    if n < 4 and s not in bought_today and c <= p["first"] * (1 - P["dca_drops"][n]):
                        cands.append((c / p["first"] - 1, s, n))
                else:
                    dd = c / a["hi30"][i] - 1
                    if dd <= -P["entry_drop"] and a["rsi"][i] < P["entry_rsi"] and a["cooled"][i] == 1:
                        cands.append((dd, s, 0))
            for _, s, n in sorted(cands):
                if day_buys >= P["max_buys_day"]:
                    break
                size = sizes[n]
                if cash < size:
                    continue
                fill = arr[s]["c"][i] * mult_buy
                qty = size * (1 - fee) / fill
                cash -= size
                if s in pos:
                    p = pos[s]
                    p["qty"] += qty; p["cost"] += size; p["n"] += 1
                else:
                    pos[s] = dict(qty=qty, cost=size, n=1, first=arr[s]["c"][i], act=False, peak=0.0, open_t=t)
                bought_today.add(s)
                day_buys += 1
                trades.append(dict(t=t, sym=s, side="BUY", price=fill, usdt=size, pnl=0.0, pnl_pct=0.0, tranches=pos[s]["n"], days=0))
        # ---- equity diario a las 00:00 UTC ----
        if t.hour == 0:
            val = cash + sum(p["qty"] * arr[s]["c"][i] * (1 - fee) for s, p in pos.items() if not np.isnan(arr[s]["c"][i]))
            inv = sum(p["cost"] for p in pos.values())
            eq_rows.append(dict(t=t, equity=val, invested=inv, positions=len(pos)))
    open_pos = {s: dict(cost=p["cost"], value=p["qty"] * arr[s]["c"][-1] * (1 - fee), n=p["n"],
                        days=(idx[-1] - p["open_t"]).total_seconds() / 86400) for s, p in pos.items()}
    return pd.DataFrame(trades), pd.DataFrame(eq_rows).set_index("t"), open_pos, warm_end


def report(P, tr, eq, open_pos, warm_end):
    cap = P["capital"]
    eq = eq[eq.index >= warm_end]
    sells, buys = tr[tr.side == "SELL"], tr[tr.side == "BUY"]
    m_eq = eq.equity.resample("MS").last()
    m_start = eq.equity.resample("MS").first()
    months = pd.DataFrame({"equity_fin": m_eq})
    months["equity_chg_pct"] = (m_eq / m_eq.shift(1).fillna(cap) - 1) * 100
    if len(sells):
        months["realizado_usdt"] = sells.set_index("t").pnl.resample("MS").sum()
        months["ventas"] = sells.set_index("t").pnl.resample("MS").count()
    else:
        months["realizado_usdt"], months["ventas"] = 0.0, 0
    months["compras"] = buys.set_index("t").usdt.resample("MS").count() if len(buys) else 0
    months = months.fillna(0)
    months["realizado_pct"] = months.realizado_usdt / cap * 100
    dd = (eq.equity / eq.equity.cummax() - 1) * 100
    worst_vs_init = (eq.equity.min() / cap - 1) * 100
    nm = max(len(months), 1)
    ops_days = tr.assign(d=(tr.t - pd.Timedelta(hours=3)).dt.date, m=tr.t.dt.strftime("%Y-%m")).groupby("m").d.nunique()
    S = dict(
        meses=len(months),
        ganancia_mensual_realizada_prom_pct=months.realizado_pct.mean(),
        ganancia_mensual_equity_prom_pct=months.equity_chg_pct.mean(),
        meses_con_realizado_positivo_pct=(months.realizado_usdt > 0).mean() * 100,
        meses_equity_no_negativo_pct=(months.equity_chg_pct >= 0).mean() * 100,
        compras_por_mes=len(buys) / nm, ventas_por_mes=len(sells) / nm,
        dias_operados_por_mes=ops_days.mean() if len(ops_days) else 0,
        peor_caida_equity_desde_maximo_pct=dd.min(),
        peor_equity_vs_capital_inicial_pct=worst_vs_init,
        max_dias_posicion_cerrada=sells.days.max() if len(sells) else 0,
        max_dias_posicion_abierta_al_final=max([p["days"] for p in open_pos.values()], default=0),
        capital_invertido_prom_pct=eq.invested.mean() / cap * 100,
        dias_100pct_en_usdt_pct=(eq.invested < 1).mean() * 100,
        equity_final=eq.equity.iloc[-1], retorno_total_pct=(eq.equity.iloc[-1] / cap - 1) * 100,
        ciclos_cerrados=len(sells), posiciones_abiertas_al_final=len(open_pos),
    )
    return S, months, dd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", type=float); ap.add_argument("--rsi", type=float)
    ap.add_argument("--tag", default="base")
    a = ap.parse_args()
    if a.drop: P["entry_drop"] = a.drop
    if a.rsi: P["entry_rsi"] = a.rsi
    idx, sig = load()
    tr, eq, op, we = run(P, idx, sig)
    OUT.mkdir(exist_ok=True)
    if tr.empty:
        print("Sin operaciones"); return
    S, months, dd = report(P, tr, eq, op, we)
    tr.to_csv(OUT / f"trades_{a.tag}.csv", index=False)
    months.to_csv(OUT / f"mensual_{a.tag}.csv")
    eq.to_csv(OUT / f"equity_{a.tag}.csv")
    print(f"== {a.tag}: caida>={P['entry_drop']*100:.0f}% RSI<{P['entry_rsi']:.0f} | {len(sig)} criptos | {idx[0].date()} -> {idx[-1].date()}")
    for k, v in S.items():
        print(f"{k:45s} {v:,.2f}")
    print(months.round(2).to_string())
    if op:
        print("Abiertas al final:", {s: (round(p['value'] / p['cost'] - 1, 3), p['n'], round(p['days'])) for s, p in op.items()})


if __name__ == "__main__":
    main()
