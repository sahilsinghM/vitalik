from statistics import median


def to_inr(px, quote, usdtinr):
    if px is None:
        return None
    return px if quote == "INR" else px * usdtinr


def fx_from_books(usdt_books):
    """usdt_books: list of (bid, ask) for USDT/INR on Indian exchanges."""
    mids = [(b + a) / 2 for b, a in usdt_books if b and a]
    spreads = [a - b for b, a in usdt_books if b and a]
    if not mids:
        return None, None
    return median(mids), median(spreads)


def reference_mids(books_by_base, usdtinr):
    """books_by_base: {base: [(quote, bid, ask), ...]} -> {base: median INR mid}"""
    out = {}
    for base, lst in books_by_base.items():
        mids = [to_inr((b + a) / 2, q, usdtinr) for q, b, a in lst if b and a]
        if mids:
            out[base] = median(mids)
    return out


def evaluate_cycle(rows, cost, usdtinr, usdtinr_spread, ref_mid, *, max_skew_ms=2000,
                   rebalance_every=1, mode="prefunded"):
    """rows: quote dicts for one cycle & one size. Yields (route_key, eval_dict) for every
    ordered venue pair on the same base where the buy side is cheaper than the sell side."""
    by_base = {}
    for r in rows:
        by_base.setdefault(r["base"], []).append(r)
    for base, lst in by_base.items():
        for a in lst:
            if not a["full_buy"] or a["vwap_buy"] is None:
                continue
            buy_inr = to_inr(a["vwap_buy"], a["quote"], usdtinr)
            for b in lst:
                if b is a or not b["full_sell"] or b["vwap_sell"] is None:
                    continue
                if a["exchange"] == b["exchange"] and a["quote"] == b["quote"]:
                    continue
                if abs(a["ts"] - b["ts"]) > max_skew_ms:
                    continue
                sell_inr = to_inr(b["vwap_sell"], b["quote"], usdtinr)
                if sell_inr <= buy_inr:
                    continue
                ev = cost.evaluate(base=base, ex_a=a["exchange"], quote_a=a["quote"],
                                   ex_b=b["exchange"], quote_b=b["quote"], qty=a["qty"],
                                   buy_px_inr=buy_inr, sell_px_inr=sell_inr,
                                   base_mid_inr=ref_mid.get(base, buy_inr), usdtinr=usdtinr,
                                   usdtinr_spread=usdtinr_spread or 0.0, mode=mode,
                                   rebalance_every=rebalance_every)
                key = (base, a["exchange"], a["quote"], b["exchange"], b["quote"])
                yield key, ev


def route_name(key):
    base, ea, qa, eb, qb = key
    return f"{base}: buy {ea}({qa}) -> sell {eb}({qb})"
