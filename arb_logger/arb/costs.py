"""Cost engine: what a buy-on-A / sell-on-B round trip nets an Indian resident, in INR.

Everything is per trade, conservative by default:
  * taker fees on both legs (+18% GST on fees)
  * USDT<->INR conversion leg when the two venues quote in different currencies
  * blockchain withdrawal fees to move inventory back (pre-funded) or to move the coin (transfer mode)
  * income tax 30% + 4% cess on each profitable trade's gross gain — no loss set-off, no fee deduction
  * 1% TDS on every VDA transfer, reported separately: it's a credit you claim back in your ITR,
    but it locks working capital until then. `net_if_tds_stuck` shows the worst case.
"""


class CostModel:
    def __init__(self, cfg):
        self.cfg = cfg
        t = cfg["tax"]
        self.tax_rate = t["income_tax_rate"] * (1 + t["surcharge"]) * (1 + t["cess"])
        self.tds = t["tds_rate"]
        self.gst = t["gst_on_fees"]
        self.ex = cfg["exchanges"]
        self.fx_fee = cfg["fx"]["fx_leg_taker_fee"]
        self.overrides = cfg.get("withdraw_fee_overrides") or {}
        self.default_taker = {}   # (exchange, base, quote) -> fee from ccxt, filled by logger/analyzer

    def taker(self, exchange, base, quote):
        f = self.ex[exchange].get("taker_fee")
        if f is None:
            f = self.default_taker.get((exchange, base, quote), 0.002)
        return f

    def wfee(self, exchange, coin):
        """Withdrawal fee in coin units."""
        return self.overrides.get(exchange, {}).get(coin, self.cfg["withdraw_fee"].get(coin, 0.0))

    def transfer_minutes(self, from_exchange, coin):
        return self.cfg["transfer_minutes"].get(coin, 30) + self.ex[from_exchange].get("withdraw_processing_min", 10)

    def evaluate(self, *, base, ex_a, quote_a, ex_b, quote_b, qty,
                 buy_px_inr, sell_px_inr, base_mid_inr, usdtinr, usdtinr_spread=0.0,
                 mode="prefunded", rebalance_every=1):
        """Buy `qty` of `base` on (ex_a, quote_a) at buy_px_inr, sell on (ex_b, quote_b) at sell_px_inr.
        Prices already converted to INR. Returns dict of INR amounts."""
        buy_val, sell_val = buy_px_inr * qty, sell_px_inr * qty
        gross = sell_val - buy_val

        fee_buy = buy_val * self.taker(ex_a, base, quote_a)
        fee_sell = sell_val * self.taker(ex_b, base, quote_b)
        gst = (fee_buy * self.ex[ex_a].get("gst", True) + fee_sell * self.ex[ex_b].get("gst", True)) * self.gst

        # Converting proceeds between USDT and INR (only when quotes differ): one more trade
        fx_cost = tds_fx = 0.0
        if quote_a != quote_b:
            half_spread = (usdtinr_spread / 2 / usdtinr) if usdtinr else 0.0
            fx_cost = sell_val * (self.fx_fee * (1 + self.gst) + half_spread)
            tds_fx = sell_val * self.tds        # one direction of the round trip sells USDT

        # TDS: you transfer a VDA when you SELL the coin (always) and when you BUY it with USDT
        tds_buy = buy_val * self.tds if (quote_a != "INR" and self.ex[ex_a].get("tds", True)) else 0.0
        tds_sell = sell_val * self.tds if self.ex[ex_b].get("tds", True) else 0.0

        # Moving money between exchanges
        move = 0.0
        if ex_a != ex_b:
            coin_fee = self.wfee(ex_a, base) * base_mid_inr            # coin goes A -> B
            if quote_a == "INR" and quote_b == "INR":
                quote_fee = 0.0                                         # IMPS between Indian exchanges
            else:
                quote_fee = self.wfee(ex_b, "USDT") * usdtinr           # cash goes B -> A as USDT
            move = (coin_fee + quote_fee) * (1 + self.gst)              # GST on withdrawal fees too
            if mode == "prefunded":
                move /= max(rebalance_every, 1)

        tax = self.tax_rate * max(gross, 0.0)
        costs = fee_buy + fee_sell + gst + fx_cost + move
        net = gross - costs - tax
        tds_locked = tds_buy + tds_sell + tds_fx
        return {
            "notional": buy_val, "gross": gross, "fees": fee_buy + fee_sell, "gst": gst,
            "fx_leg": fx_cost, "transfer": move, "tax": tax, "net": net,
            "tds_locked": tds_locked, "net_if_tds_stuck": net - tds_locked,
            "gross_bps": gross / buy_val * 1e4 if buy_val else 0.0,
        }
