"""Paper-trade the logged spreads and report what ₹10L would actually have made.

    python analyze.py                       # reads data/spreads.sqlite
    python analyze.py --db data/spreads_sim.sqlite

Two execution models:
  prefunded  money already sits on both exchanges; both legs fire together (after execution lag);
             inventory is moved back with a blockchain withdrawal every N trades.
  transfer   buy on A, withdraw the coin to B, sell on B only once it has confirmed —
             at whatever B's price is by then.

Writes report.md, routes.csv, trades.csv into --out.
"""
import argparse
import bisect
import csv
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

import yaml

from arb.costs import CostModel
from arb.routes import evaluate_cycle, route_name, to_inr

COLS = ["cycle", "ts", "exchange", "quote", "base", "size_inr", "bid", "ask",
        "vwap_buy", "vwap_sell", "qty", "full_buy", "full_sell"]


def load(db_path):
    db = sqlite3.connect(db_path)
    cycles = {c: dict(ts=ts, usdtinr=fx, spread=sp or 0.0) for c, ts, fx, sp, _ in
              db.execute("SELECT * FROM cycles ORDER BY cycle")}
    rows = defaultdict(lambda: defaultdict(list))     # size -> cycle -> [row]
    for r in db.execute(f"SELECT {','.join(COLS)} FROM quotes"):
        d = dict(zip(COLS, r))
        rows[d["size_inr"]][d["cycle"]].append(d)
    db.close()
    return cycles, rows


def ref_mids(cycle_rows, usdtinr):
    by = defaultdict(list)
    for r in cycle_rows:
        by[r["base"]].append(to_inr((r["bid"] + r["ask"]) / 2, r["quote"], usdtinr))
    return {b: sorted(v)[len(v) // 2] for b, v in by.items()}


def eval_route(cost, key, ra, rb, fx, ref, mode, rebalance_every, qty=None):
    base, ea, qa, eb, qb = key
    q = qty or ra["qty"]
    return cost.evaluate(base=base, ex_a=ea, quote_a=qa, ex_b=eb, quote_b=qb, qty=q,
                         buy_px_inr=to_inr(ra["vwap_buy"], qa, fx["usdtinr"]),
                         sell_px_inr=to_inr(rb["vwap_sell"], qb, fx["usdtinr"]),
                         base_mid_inr=ref.get(base, 0), usdtinr=fx["usdtinr"],
                         usdtinr_spread=fx["spread"], mode=mode, rebalance_every=rebalance_every)


def simulate(cfg, cost, cycles, size_rows, size, mode):
    an = cfg["analysis"]
    lag, rb_every, min_net = an["execution_lag_cycles"], an["rebalance_every_n_trades"], an["min_net_profit_inr"]
    max_concurrent = max(1, int(cfg["capital_inr"] // (2 * size)))
    order = sorted(size_rows)
    idx = {c: i for i, c in enumerate(order)}

    # for transfer mode: per venue time series of sell-side fills
    series = defaultdict(list)
    for c in order:
        for r in size_rows[c]:
            series[(r["exchange"], r["quote"], r["base"])].append((r["ts"], r))

    def lookup(c, ex, q, base):
        for r in size_rows.get(c, []):
            if r["exchange"] == ex and r["quote"] == q and r["base"] == base:
                return r

    seen = defaultdict(float)          # cost waterfall over every positive-gross observation
    n_seen = n_profitable = 0
    trades, open_ep = [], set()
    for c in order:
        fx = cycles[c]
        rows = size_rows[c]
        ref = ref_mids(rows, fx["usdtinr"])
        signals = []
        for key, ev in evaluate_cycle(rows, cost, fx["usdtinr"], fx["spread"], ref,
                                      max_skew_ms=an["max_book_skew_ms"], rebalance_every=rb_every, mode=mode):
            n_seen += 1
            for k in ("notional", "gross", "fees", "gst", "fx_leg", "transfer", "tax", "net", "tds_locked"):
                seen[k] += ev[k]
            if ev["net"] > min_net:
                n_profitable += 1
                signals.append((key, ev))
        live = {k for k, _ in signals}
        open_ep &= live                                    # episodes that ended
        signals = [s for s in signals if s[0] not in open_ep]
        signals.sort(key=lambda s: s[1]["net"], reverse=True)

        for key, sig in signals[:max_concurrent]:
            open_ep.add(key)
            base, ea, qa, eb, qb = key
            ei = idx[c] + lag
            if ei >= len(order):
                continue
            ce = order[ei]
            fxe = cycles[ce]
            ra = lookup(ce, ea, qa, base)
            if not ra or not ra["full_buy"]:
                continue
            refe = ref_mids(size_rows[ce], fxe["usdtinr"])
            if mode == "prefunded":
                rb = lookup(ce, eb, qb, base)
            else:  # coin lands on B after confirmations + withdrawal processing
                arrive = ra["ts"] + cost.transfer_minutes(ea, base) * 60_000 if ea != eb else ra["ts"]
                s = series[(eb, qb, base)]
                j = bisect.bisect_left([t for t, _ in s], arrive)
                rb = s[j][1] if j < len(s) else None
            if not rb or not rb["full_sell"]:
                continue
            ev = eval_route(cost, key, ra, rb, fxe, refe, mode, rb_every, qty=ra["qty"])
            trades.append(dict(mode=mode, size=size, route=route_name(key), signal_cycle=c, exec_cycle=ce,
                               signal_ts=fx["ts"], signal_net=sig["net"], **ev))
    return dict(n_seen=n_seen, n_profitable=n_profitable, seen=seen, trades=trades)


def fmt(x):
    return f"₹{x:,.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default="reports")
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = yaml.safe_load(f)
    cost = CostModel(cfg)
    cycles, rows = load(a.db or cfg["db_path"])
    if not cycles:
        raise SystemExit("no data logged yet — run logger.py first")
    os.makedirs(a.out, exist_ok=True)

    t0, t1 = min(c["ts"] for c in cycles.values()), max(c["ts"] for c in cycles.values())
    hours = max((t1 - t0) / 3.6e6, 1e-9)
    venues = sorted({(r["exchange"], r["quote"]) for s in rows.values() for cr in s.values() for r in cr})
    cap = cfg["capital_inr"]

    md = ["# Cross-exchange spread report", "",
          f"Logged {datetime.fromtimestamp(t0/1000):%d %b %H:%M} → {datetime.fromtimestamp(t1/1000):%d %b %H:%M} "
          f"({hours:.1f} h, {len(cycles)} polls). Paper capital {fmt(cap)}.", "",
          f"Venues: {', '.join(f'{e}({q})' for e, q in venues)}", "",
          f"Avg USDT/INR on Indian exchanges: {sum(c['usdtinr'] for c in cycles.values())/len(cycles):.2f}", "",
          "Costs included: taker fees both legs, 18% GST on fees, USDT↔INR conversion leg, withdrawal fees "
          "(pre-funded: amortised rebalancing; transfer: per trade), 31.2% tax on each profitable trade's gross "
          "gain (no loss set-off, fees not deductible). TDS (1% per VDA transfer) shown separately as capital "
          "locked until your ITR refund; *net if TDS stuck* is the worst case.", ""]
    all_trades, route_rows = [], []

    for size in sorted(rows):
        md += [f"## Trade size {fmt(size)} per leg", ""]
        for mode in ("prefunded", "transfer"):
            res = simulate(cfg, cost, cycles, rows[size], size, mode)
            tr = res["trades"]
            all_trades += tr
            net = sum(t["net"] for t in tr)
            stuck = sum(t["net_if_tds_stuck"] for t in tr)
            tds = sum(t["tds_locked"] for t in tr)
            wins = sum(1 for t in tr if t["net"] > 0)
            md += [f"### {mode}", "",
                   "| metric | value |", "|---|---|",
                   f"| positive gross spreads seen | {res['n_seen']:,} |",
                   f"| …still profitable after all costs | {res['n_profitable']:,} |",
                   f"| trades taken (one per episode, capital-limited) | {len(tr):,} |",
                   f"| trades that actually made money after execution lag | {wins:,} |",
                   f"| realised net P&L | {fmt(net)} |",
                   f"| net if TDS never recovered | {fmt(stuck)} |",
                   f"| TDS deducted (capital locked) | {fmt(tds)} |",
                   f"| return on {fmt(cap)} over the period | {net/cap*100:.3f}% |",
                   f"| naive annualised | "
                   f"{f'{net/cap*100*8760/hours:.1f}%' if hours >= 24 else 'n/a — log at least 24h'} |", ""]
            s = res["seen"]
            if res["n_seen"]:
                bp = lambda k: f"{s[k] / s['notional'] * 1e4:.1f}"
                md += ["Where the spread goes — average over every positive-gross observation, "
                       "in basis points of trade size (100 bps = 1%):", "",
                       "| gross | fees | GST | FX leg | transfers | tax | **net** | TDS locked |",
                       "|---|---|---|---|---|---|---|---|",
                       f"| {bp('gross')} | {bp('fees')} | {bp('gst')} | {bp('fx_leg')} | {bp('transfer')} | "
                       f"{bp('tax')} | **{bp('net')}** | {bp('tds_locked')} |", ""]
            agg = defaultdict(lambda: dict(n=0, net=0.0, gross_bps=0.0, stuck=0.0))
            for t in tr:
                g = agg[t["route"]]
                g["n"] += 1; g["net"] += t["net"]; g["gross_bps"] += t["gross_bps"]; g["stuck"] += t["net_if_tds_stuck"]
            top = sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True)
            for r, g in top:
                route_rows.append(dict(size=size, mode=mode, route=r, trades=g["n"], net_inr=round(g["net"], 2),
                                       avg_gross_bps=round(g["gross_bps"] / g["n"], 2),
                                       net_if_tds_stuck=round(g["stuck"], 2)))
            if top:
                md += ["| best routes | trades | avg gross bps | net |", "|---|---|---|---|"]
                md += [f"| {r} | {g['n']} | {g['gross_bps']/g['n']:.1f} | {fmt(g['net'])} |" for r, g in top[:10]]
                md += [""]

    with open(os.path.join(a.out, "report.md"), "w") as f:
        f.write("\n".join(md))
    for name, data in (("trades.csv", all_trades), ("routes.csv", route_rows)):
        with open(os.path.join(a.out, name), "w", newline="") as f:
            if data:
                w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
                w.writeheader(); w.writerows(data)
    print("\n".join(md))
    print(f"\nwritten to {a.out}/report.md, routes.csv, trades.csv")


if __name__ == "__main__":
    main()
