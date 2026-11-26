"""Cliente minimo de la API REST de Binance Spot (solo requests, sin librerias de terceros)."""
import hashlib, hmac, math, time
from urllib.parse import urlencode
import requests

URLS = {"testnet": "https://testnet.binance.vision", "real": "https://api.binance.com"}


class BinanceError(Exception):
    pass


class Binance:
    def __init__(self, mode, key="", secret=""):
        self.base = URLS[mode]
        self.mode, self.key, self.secret = mode, key, secret
        self.s = requests.Session()
        if key:
            self.s.headers["X-MBX-APIKEY"] = key
        self.offset = 0
        self._filters = {}

    def _req(self, method, path, params=None, signed=False):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.offset
            params["recvWindow"] = 10000
            q = urlencode(params)
            params["signature"] = hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        r = self.s.request(method, self.base + path, params=params, timeout=20)
        if r.status_code != 200:
            raise BinanceError(f"{r.status_code} {path}: {r.text[:300]}")
        return r.json()

    def sync_time(self):
        self.offset = self._req("GET", "/api/v3/time")["serverTime"] - int(time.time() * 1000)

    # ---- publico ----
    def klines(self, symbol, interval, limit):
        return self._req("GET", "/api/v3/klines", dict(symbol=symbol, interval=interval, limit=limit))

    def prices(self, symbols):
        """Precios de los simbolos pedidos (se baja la lista completa: en testnet no existen todos los pares)."""
        want = set(symbols)
        return {d["symbol"]: float(d["price"]) for d in self._req("GET", "/api/v3/ticker/price") if d["symbol"] in want}

    def filters(self, symbol):
        if symbol not in self._filters:
            info = self._req("GET", "/api/v3/exchangeInfo", dict(symbol=symbol))["symbols"][0]
            f = {x["filterType"]: x for x in info["filters"]}
            notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
            self._filters[symbol] = dict(
                base=info["baseAsset"],
                step=float(f["LOT_SIZE"]["stepSize"]), min_qty=float(f["LOT_SIZE"]["minQty"]),
                min_notional=float(notional.get("minNotional", 5)))
        return self._filters[symbol]

    # ---- privado ----
    def balances(self):
        acc = self._req("GET", "/api/v3/account", signed=True)
        return {b["asset"]: float(b["free"]) for b in acc["balances"]}

    def withdrawals_enabled(self):
        """Solo en cuenta real: True si la clave API permite retiros (el bot se niega a arrancar)."""
        return bool(self._req("GET", "/sapi/v1/account/apiRestrictions", signed=True).get("enableWithdrawals"))

    def market_buy(self, symbol, usdt):
        return self._req("POST", "/api/v3/order", dict(symbol=symbol, side="BUY", type="MARKET",
                         quoteOrderQty=f"{usdt:.2f}", newOrderRespType="FULL"), signed=True)

    def market_sell(self, symbol, qty):
        qty = floor_step(qty, self.filters(symbol)["step"])
        return self._req("POST", "/api/v3/order", dict(symbol=symbol, side="SELL", type="MARKET",
                         quantity=f"{qty:.12f}".rstrip("0").rstrip("."), newOrderRespType="FULL"), signed=True)


def floor_step(qty, step):
    return math.floor(qty / step + 1e-9) * step


def summarize_fills(order, base, fee_rate):
    """-> dict(qty=cantidad neta de base, quote=USDT operado, fee_extra=comision pagada fuera de la base (USDT/BNB, en USDT),
    fee_total=comision total en USDT, avg=precio promedio). Si la comision se paga en la moneda comprada, ya baja qty."""
    qty = float(order["executedQty"])
    quote = float(order["cummulativeQuoteQty"])
    fee_extra = fee_total = 0.0
    for f in order.get("fills", []):
        c, asset = float(f["commission"]), f["commissionAsset"]
        if asset == base:
            qty -= c
            fee_total += c * float(f["price"])
        elif asset == "USDT":
            fee_extra += c; fee_total += c
        else:  # BNB u otra: estimacion por tasa
            est = float(f["price"]) * float(f["qty"]) * fee_rate
            fee_extra += est; fee_total += est
    return dict(qty=qty, quote=quote, fee_extra=fee_extra, fee_total=fee_total,
                avg=quote / float(order["executedQty"]) if float(order["executedQty"]) else 0.0)
