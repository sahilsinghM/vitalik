"""Read-only market data adapters. No API keys: everything here is public data."""
import asyncio
import random
import time

import aiohttp

try:
    import ccxt.async_support as ccxta
except ImportError:  # simulator still works without ccxt
    ccxta = None


class Book:
    """Order book snapshot. bids/asks: list of (price, qty), best first."""
    __slots__ = ("bids", "asks", "ts")

    def __init__(self, bids, asks, ts):
        self.bids, self.asks, self.ts = bids, asks, ts


class CcxtVenue:
    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.ex = getattr(ccxta, name)({"enableRateLimit": True, "timeout": 10000})
        self.markets = {}   # (base, quote) -> ccxt symbol
        self.default_taker = {}

    async def load(self, assets, quotes):
        await self.ex.load_markets()
        for sym, m in self.ex.markets.items():
            if m.get("spot") and m.get("active", True) is not False and m["base"] in assets and m["quote"] in quotes:
                self.markets[(m["base"], m["quote"])] = sym
                if m.get("taker") is not None:
                    self.default_taker[(m["base"], m["quote"])] = m["taker"]

    async def book(self, base, quote):
        sym = self.markets[(base, quote)]
        ob = await self.ex.fetch_order_book(sym, self.cfg.get("ob_limit"))
        return Book([(float(p), float(q)) for p, q, *_ in ob["bids"]],
                    [(float(p), float(q)) for p, q, *_ in ob["asks"]], int(time.time() * 1000))

    async def close(self):
        await self.ex.close()


class CoinDCXVenue:
    """CoinDCX isn't in ccxt — thin adapter over its public REST API."""
    MARKETS = "https://api.coindcx.com/exchange/v1/markets_details"
    BOOK = "https://public.coindcx.com/market_data/orderbook?pair={pair}"

    def __init__(self, name, cfg):
        self.name, self.cfg = name, cfg
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self.markets = {}
        self.default_taker = {}

    async def load(self, assets, quotes):
        async with self.session.get(self.MARKETS) as r:
            data = await r.json(content_type=None)
        for m in data:
            # CoinDCX naming: target = the coin, base_currency = the quote
            base, quote = m.get("target_currency_short_name"), m.get("base_currency_short_name")
            if base in assets and quote in quotes and m.get("status") == "active":
                pair = m.get("pair")
                # prefer the native INR book ("I-") over routed ones ("B-" = Binance-routed)
                if (base, quote) not in self.markets or (pair or "").startswith("I-"):
                    self.markets[(base, quote)] = pair

    async def book(self, base, quote):
        async with self.session.get(self.BOOK.format(pair=self.markets[(base, quote)])) as r:
            d = await r.json(content_type=None)
        bids = sorted(((float(p), float(q)) for p, q in d.get("bids", {}).items()), reverse=True)
        asks = sorted((float(p), float(q)) for p, q in d.get("asks", {}).items())
        return Book(bids, asks, int(time.time() * 1000))

    async def close(self):
        await self.session.close()


class SimVenue:
    """Synthetic venue for offline testing: random-walk mids, per-venue bias, INR premium."""
    BASE_PX = {"BTC": 62000, "ETH": 2400, "BNB": 580, "XRP": 0.55, "SOL": 145, "TRX": 0.16,
               "ZEC": 40, "USDC": 1.0, "USDT": 1.0}
    _mids = {}

    def __init__(self, name, cfg, seed):
        self.name, self.cfg = name, cfg
        self.rng = random.Random(seed)
        self.bias = self.rng.gauss(0, 0.0015)          # persistent venue mispricing
        self.markets, self.default_taker = {}, {}
        self.indian = cfg.get("indian", False)

    async def load(self, assets, quotes):
        for a in assets:
            if self.indian:
                self.markets[(a, "INR")] = f"{a}/INR"
                if a in ("BTC", "ETH"):
                    self.markets[(a, "USDT")] = f"{a}/USDT"
            elif a != "USDT":
                self.markets[(a, "USDT")] = f"{a}/USDT"
        if self.indian:
            self.markets[("USDT", "INR")] = "USDT/INR"

    @classmethod
    def tick(cls):
        for a, p in cls.BASE_PX.items():
            m = cls._mids.get(a, p)
            vol = 0.0 if a in ("USDT", "USDC") else 0.0008
            cls._mids[a] = m * (1 + random.gauss(0, vol))

    async def book(self, base, quote):
        await asyncio.sleep(0)
        mid = self._mids.get(base, self.BASE_PX[base]) * (1 + self.bias + self.rng.gauss(0, 0.0007))
        if quote == "INR":
            usdtinr = 90.7 * (1 + self.rng.gauss(0, 0.002))   # Indian USDT/INR (carries a premium over forex)
            mid = usdtinr if base == "USDT" else mid * usdtinr
        half = mid * (0.0004 if not self.indian else 0.0015)
        lvl = lambda i: mid * 0.0002 * i
        size = 25000 / max(mid, 1e-9) if self.indian else 150000 / max(mid, 1e-9)
        bids = [(mid - half - lvl(i), size * (1 + i)) for i in range(15)]
        asks = [(mid + half + lvl(i), size * (1 + i)) for i in range(15)]
        return Book(bids, asks, int(time.time() * 1000) - self.rng.randint(0, 400))

    async def close(self):
        pass


def make_venue(name, cfg, simulate=False, seed=0):
    if simulate:
        return SimVenue(name, cfg, seed)
    if name == "coindcx":
        return CoinDCXVenue(name, cfg)
    if ccxta is None or not hasattr(ccxta, name):
        raise ValueError(f"exchange '{name}' not supported (not in ccxt, no custom adapter)")
    return CcxtVenue(name, cfg)
