"""Read-only cross-exchange spread logger.

    python logger.py                 # live, public market data, no API keys
    python logger.py --simulate      # offline synthetic markets (for testing the pipeline)
    python logger.py --cycles 60     # stop after N polls

Logs order-book VWAPs for each configured trade size into SQLite, and prints the best
routes after every cost (fees, GST, TDS, tax, transfers) each cycle. Run analyze.py later.
"""
import argparse
import asyncio
import json
import logging
import time

import yaml

from arb import store
from arb.book import vwap
from arb.costs import CostModel
from arb.routes import evaluate_cycle, fx_from_books, reference_mids, route_name
from arb.venues import SimVenue, make_venue

log = logging.getLogger("logger")


async def safe_book(v, base, quote):
    try:
        return v.name, base, quote, await v.book(base, quote)
    except Exception as e:  # one bad market must not kill the cycle
        log.debug("book %s %s/%s failed: %s", v.name, base, quote, e)
        return v.name, base, quote, None


async def setup(cfg, simulate):
    assets = set(cfg["assets"]) | {"USDT"}
    venues = []
    for i, (name, ecfg) in enumerate(cfg["exchanges"].items()):
        if not ecfg.get("enabled", True):
            continue
        quotes = {"INR", "USDT"} if ecfg.get("indian") else {"USDT"}
        try:
            v = make_venue(name, ecfg, simulate=simulate, seed=i)
            await v.load(assets, quotes)
            # USDT itself only makes sense against INR
            v.markets = {k: s for k, s in v.markets.items() if not (k[0] == "USDT" and k[1] != "INR")}
            log.info("%-9s %d markets: %s", name, len(v.markets),
                     ", ".join(f"{b}/{q}" for b, q in sorted(v.markets)))
            venues.append(v)
        except Exception as e:
            log.warning("skipping %s: %s", name, e)
    return venues


async def run(cfg, simulate, cycles, quiet, full_books=False, depth=25):
    db = store.connect(cfg["db_path"])
    cost = CostModel(cfg)
    venues = await setup(cfg, simulate)
    if not venues:
        raise SystemExit("no exchanges reachable — check your internet connection / config")
    for v in venues:
        for (b, q), f in v.default_taker.items():
            cost.default_taker[(v.name, b, q)] = f

    sizes = cfg["trade_sizes_inr"]
    show_size = sizes[len(sizes) // 2]
    an = cfg["analysis"]
    n = 0
    sim_start = int(time.time() * 1000)
    try:
        while cycles is None or n < cycles:
            t0 = time.time()
            if simulate:
                SimVenue.tick()
            results = await asyncio.gather(*(safe_book(v, b, q) for v in venues for (b, q) in v.markets))
            books = {(e, b, q): bk for e, b, q, bk in results if bk and bk.bids and bk.asks}
            if simulate:  # virtual clock so transfer-time logic can be tested without waiting
                vnow = sim_start + n * cfg["sim_virtual_interval"] * 1000
                for bk in books.values():
                    bk.ts = vnow - (int(time.time() * 1000) - bk.ts)

            usdt = [(bk.bids[0][0], bk.asks[0][0]) for (e, b, q), bk in books.items() if b == "USDT" and q == "INR"]
            usdtinr, spread = fx_from_books(usdt)
            src = "indian_exchanges"
            if usdtinr is None:
                usdtinr, spread, src = cfg["fx"]["usdtinr_fallback"], 0.0, "fallback"

            by_base = {}
            for (e, b, q), bk in books.items():
                by_base.setdefault(b, []).append((q, bk.bids[0][0], bk.asks[0][0]))
            ref = reference_mids(by_base, usdtinr)

            cyc = store.next_cycle(db)
            now = vnow if simulate else int(time.time() * 1000)
            db.execute("INSERT INTO cycles VALUES (?,?,?,?,?)", (cyc, now, usdtinr, spread, src))
            for v in venues:
                ok = sum(1 for (e, _b, _q) in books if e == v.name)
                db.execute("INSERT INTO cycle_status VALUES (?,?,?,?)", (cyc, v.name, len(v.markets), ok))
            if full_books:
                for (e, b, q), bk in books.items():
                    for side, lv in (("bid", bk.bids), ("ask", bk.asks)):
                        db.execute("INSERT INTO levels VALUES (?,?,?,?,?,?)",
                                   (cyc, e, q, b, side, json.dumps([[p, s] for p, s in lv[:depth]])))
            rows_for_show = []
            for (e, b, q), bk in books.items():
                if b not in ref:
                    continue
                for size in sizes:
                    qty = size / ref[b]
                    vb, fb = vwap(bk.asks, qty)
                    vs, fs = vwap(bk.bids, qty)
                    full_b, full_s = int(fb >= qty * 0.999), int(fs >= qty * 0.999)
                    row = (cyc, bk.ts, e, q, b, size, bk.bids[0][0], bk.asks[0][0], vb, vs, qty, full_b, full_s)
                    db.execute("INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                    if size == show_size:
                        rows_for_show.append(dict(exchange=e, quote=q, base=b, vwap_buy=vb, vwap_sell=vs,
                                                  qty=qty, full_buy=full_b, full_sell=full_s, ts=bk.ts))
            db.commit()
            n += 1

            if not quiet:
                evs = sorted(evaluate_cycle(rows_for_show, cost, usdtinr, spread, ref,
                                            max_skew_ms=an["max_book_skew_ms"],
                                            rebalance_every=an["rebalance_every_n_trades"]),
                             key=lambda kv: kv[1]["net"], reverse=True)
                print(f"\n[{time.strftime('%H:%M:%S')}] cycle {cyc}  books={len(books)}  "
                      f"USDT/INR={usdtinr:.2f} ({src})  size=₹{show_size:,}")
                if not evs:
                    print("  no positive gross spreads this cycle")
                for k, ev in evs[:5]:
                    print(f"  {route_name(k):48s} gross {ev['gross_bps']:6.1f}bps  ₹{ev['gross']:8.0f}  "
                          f"→ net ₹{ev['net']:8.0f}  (TDS locked ₹{ev['tds_locked']:.0f})")

            await asyncio.sleep(max(0.0, cfg["poll_interval_sec"] - (time.time() - t0)))
    finally:
        for v in venues:
            await v.close()
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--simulate", action="store_true", help="synthetic markets, no network")
    ap.add_argument("--cycles", type=int, default=None)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--full-books", action="store_true", help="also store the top N order-book levels per side")
    ap.add_argument("--depth", type=int, default=25, help="levels per side for --full-books")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    with open(a.config) as f:
        cfg = yaml.safe_load(f)
    if a.simulate:
        cfg["db_path"] = cfg["db_path"].replace(".sqlite", "_sim.sqlite")
        cfg["sim_virtual_interval"] = cfg["poll_interval_sec"]
        cfg["poll_interval_sec"] = 0.0
    try:
        asyncio.run(run(cfg, a.simulate, a.cycles, a.quiet, a.full_books, a.depth))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
