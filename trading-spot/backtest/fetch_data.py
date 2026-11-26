"""Baja datos PUBLICOS (sin claves) de Binance y arma el universo de criptos.

Universo: top por capitalizacion (CoinGecko) que tengan par USDT en Binance spot,
sin stablecoins ni tokens apalancados, volumen promedio 30d >= 3M USD y > 1 anio de historia.
Guarda velas de 1 hora en data/<SIMBOLO>.csv
"""
import sys, time, json, pathlib, requests
import pandas as pd

BASE = "https://data-api.binance.vision"
DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(exist_ok=True)
YEARS = 2
STABLES = {"USDT","USDC","FDUSD","TUSD","BUSD","DAI","USDP","USDD","EUR","AEUR","PYUSD","USDE","XUSD","USD1","BFUSD","RLUSD","USDS"}
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")

def get(url, **params):
    r = None
    for i in range(8):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
        except requests.RequestException:
            pass
        time.sleep(2 * (i + 1))
    if r is not None:
        r.raise_for_status()
    raise RuntimeError(f"sin respuesta: {url}")

def universe(top_n=70):
    info = get(f"{BASE}/api/v3/exchangeInfo")
    ok = {s["baseAsset"]: s["symbol"] for s in info["symbols"]
          if s["quoteAsset"] == "USDT" and s["status"] == "TRADING" and s["isSpotTradingAllowed"]
          and s["baseAsset"] not in STABLES and not s["baseAsset"].endswith(LEVERAGED)}
    cg = get("https://api.coingecko.com/api/v3/coins/markets", vs_currency="usd",
             order="market_cap_desc", per_page=150, page=1)
    out = []
    for c in cg:
        base = c["symbol"].upper()
        sym = ok.get(base)
        if sym and base not in STABLES:
            out.append(sym)
        if len(out) >= top_n:
            break
    return out

def klines(symbol, start_ms, end_ms):
    rows, cur = [], start_ms
    while cur < end_ms:
        data = get(f"{BASE}/api/v3/klines", symbol=symbol, interval="1h", startTime=cur, limit=1000)
        if not data:
            break
        rows += data
        cur = data[-1][0] + 3600_000
        if len(data) < 1000:
            break
    df = pd.DataFrame(rows, columns=["t","o","h","l","c","v","ct","qv","n","tb","tq","x"])
    df = df[["t","o","h","l","c","qv"]].astype({"t":"int64","o":float,"h":float,"l":float,"c":float,"qv":float})
    return df.drop_duplicates("t")

if __name__ == "__main__":
    syms = universe()
    print(len(syms), "simbolos:", " ".join(syms))
    end = int(time.time() * 1000)
    start = end - YEARS * 365 * 24 * 3600_000
    kept = []
    for s in syms:
        f = DATA / f"{s}.csv"
        df = pd.read_csv(f) if f.exists() else klines(s, start, end)
        days = (df.t.iloc[-1] - df.t.iloc[0]) / 86400_000 if len(df) else 0
        if days < 365:
            print(f"  {s}: solo {days:.0f} dias de historia -> excluida (< 1 anio)")
            continue
        v30 = df.qv.tail(24 * 30).sum() / 30
        if v30 < 3e6:
            print(f"  {s}: volumen 30d {v30/1e6:.1f}M < 3M -> excluida")
            continue
        df.to_csv(DATA / f"{s}.csv", index=False)
        kept.append(s)
        print(f"  {s}: {len(df)} velas, {days:.0f} dias")
    (DATA / "universe.json").write_text(json.dumps(kept))
    print("Universo final:", len(kept))
