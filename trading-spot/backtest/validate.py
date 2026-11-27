"""Test de dos mitades: se elige con la 1a mitad y se comprueba en la 2a (que el ajuste no vio)."""
import sys, itertools, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pandas as pd
import backtest as B

idx, sig = B.load()
mid = idx[0] + (idx[-1] - idx[0]) / 2
halves = {"A (1er año)": idx < mid, "B (2do año)": idx >= mid}
btc = sig.get("BTCUSDT")


def cut(mask):
    return idx[mask], {s: df[mask] for s, df in sig.items()}


def one(drop, rsi, mask):
    P = dict(B.P, entry_drop=drop, entry_rsi=rsi, warmup_days=0)
    i, sg = cut(mask)
    tr, eq, op, we = B.run(P, i, sg)
    if tr.empty:
        return None
    S, m, dd = B.report(P, tr, eq, op, we)
    return S


rows = []
for drop, rsi in itertools.product((0.15, 0.20, 0.25), (35, 30, 25)):
    for name, mask in halves.items():
        S = one(drop, rsi, mask)
        if S:
            rows.append(dict(caida=f"{drop:.0%}", rsi=rsi, mitad=name, ret_total=S["retorno_total_pct"],
                             mes_equity=S["ganancia_mensual_equity_prom_pct"], mes_realizado=S["ganancia_mensual_realizada_prom_pct"],
                             meses_ok=S["meses_equity_no_negativo_pct"], peor_caida=S["peor_caida_equity_desde_maximo_pct"],
                             max_dias_trab=max(S["max_dias_posicion_cerrada"], S["max_dias_posicion_abierta_al_final"]),
                             compras_mes=S["compras_por_mes"], abiertas=S["posiciones_abiertas_al_final"]))
df = pd.DataFrame(rows).round(1)
pd.set_option("display.width", 250)
print(f"{len(sig)} criptos | corte entre mitades: {mid.date()}")
for name in halves:
    m = halves[name]; c = btc["c"][m].dropna()
    print(f"BTC comprar y mantener en {name}: {(c.iloc[-1]/c.iloc[0]-1)*100:+.0f}%")
print(df.to_string(index=False))
df.to_csv(B.OUT / "validacion_dos_mitades.csv", index=False)
