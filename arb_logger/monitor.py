"""Trader view of the spread logger (read-only, stdlib + the project's own modules).

    python monitor.py                 # http://127.0.0.1:8765
    python monitor.py --port 9000 --db data/spreads.sqlite

Opens the logger's SQLite in read-only mode, so it cannot disturb the logger. Health is judged
from the data itself (age of the last cycle), so it works the same on a remote machine.
"""
import argparse
import copy
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import median
from urllib.parse import parse_qs, urlparse

import yaml

from arb.costs import CostModel
from arb.routes import evaluate_cycle, reference_mids, route_name

SERIES_N = 90          # polls shown in the chart (~15 min at 10 s)
_best_cache = {}       # (cycle, size) -> summary; finished cycles never change

# What-if scenarios: (key, label, transfer multiplier, fee+GST multiplier). Tax and FX stay as configured.
SCENARIOS = [
    ("real", "Your assumptions", 1.0, 1.0),
    ("xfer", "Transfers amortised (rebalance rarely)", 0.0, 1.0),
    ("best", "…and VIP fees (half)", 0.0, 0.5),
]


def connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _num(v, lo, hi):
    v = float(v)
    if not lo <= v <= hi:          # also rejects NaN
        raise ValueError(f"{v} outside [{lo}, {hi}]")
    return v


def apply_overrides(cfg, o):
    """Copy of cfg with the UI's what-if edits applied. Whitelisted keys only, bounds-checked."""
    c = copy.deepcopy(cfg)
    for ex, v in (o.get("taker") or {}).items():
        if ex in c["exchanges"]:
            c["exchanges"][ex]["taker_fee"] = _num(v, 0, 0.05)
    for k, v in (o.get("tax") or {}).items():
        if k in ("income_tax_rate", "cess", "surcharge", "tds_rate", "gst_on_fees"):
            c["tax"][k] = _num(v, 0, 1)
    if "fx_fee" in o:
        c["fx"]["fx_leg_taker_fee"] = _num(o["fx_fee"], 0, 0.05)
    if "rebalance" in o:
        c["analysis"]["rebalance_every_n_trades"] = int(_num(o["rebalance"], 1, 10000))
    for coin, v in (o.get("wfee") or {}).items():
        if coin in c["withdraw_fee"]:
            c["withdraw_fee"][coin] = _num(v, 0, 1e6)
    return c


def cfg_view(c):
    """The editable numbers, as the UI shows them."""
    return dict(taker={e: x.get("taker_fee") for e, x in c["exchanges"].items() if x.get("enabled", True)},
                tax={k: c["tax"][k] for k in ("income_tax_rate", "cess", "surcharge", "tds_rate", "gst_on_fees")},
                fx_fee=c["fx"]["fx_leg_taker_fee"], rebalance=c["analysis"]["rebalance_every_n_trades"],
                wfee=dict(c["withdraw_fee"]), wfee_overrides=c.get("withdraw_fee_overrides") or {})


def evaluate(db, cost, an, cyc, size):
    """Same pipeline as logger.py: rows for one cycle & size -> ([(route, ev)], {market: qty})."""
    cy = db.execute("SELECT usdtinr, usdtinr_spread FROM cycles WHERE cycle=?", (cyc,)).fetchone()
    if not cy:
        return [], {}
    usdtinr, spread = cy
    q = db.execute("SELECT ts, exchange, quote, base, bid, ask, vwap_buy, vwap_sell, qty, full_buy, full_sell "
                   "FROM quotes WHERE cycle=? AND size_inr=?", (cyc, size)).fetchall()
    rows, by_base, qtys = [], {}, {}
    for ts, e, qt, b, bid, ask, vb, vs, qty, fb, fs in q:
        if bid and ask:
            by_base.setdefault(b, []).append((qt, bid, ask))
        rows.append(dict(ts=ts, exchange=e, quote=qt, base=b, vwap_buy=vb, vwap_sell=vs, qty=qty,
                         full_buy=fb, full_sell=fs))
        qtys[(e, qt, b)] = qty
    ref = reference_mids(by_base, usdtinr)
    rows = [r for r in rows if r["base"] in ref]
    evs = list(evaluate_cycle(rows, cost, usdtinr, spread, ref, max_skew_ms=an["max_book_skew_ms"],
                              rebalance_every=an["rebalance_every_n_trades"]))
    return evs, qtys


def scenario_net(ev, tf, ff):
    """Net ₹ under a what-if: scale transfer cost and fee+GST. Tax stays on the same gross gain."""
    return ev["gross"] - (ev["fees"] + ev["gst"]) * ff - ev["fx_leg"] - ev["transfer"] * tf - ev["tax"]


def summarize(evs):
    """Per-poll summary used for the chart and the window tallies."""
    out = dict(n_gap=len(evs), gross_bps=max((ev["gross_bps"] for _, ev in evs), default=0.0))
    for key, _label, tf, ff in SCENARIOS:
        best_net, best_route, n_pos = None, None, 0
        for k, ev in evs:
            n = scenario_net(ev, tf, ff)
            n_pos += n > 0
            if best_net is None or n > best_net:
                best_net, best_route = n, route_name(k)
                best_bps = n / ev["notional"] * 1e4 if ev["notional"] else 0.0
        out[key] = dict(net=best_net, bps=best_bps if evs else None, route=best_route, n=n_pos)
    return out


def state(base_cfg, db_path, size, overrides=None):
    overrides = overrides or {}
    cfg = apply_overrides(base_cfg, overrides)         # raises ValueError on bad input
    cost = CostModel(cfg)
    sig = json.dumps(overrides, sort_keys=True)
    if sig not in _best_cache:
        if len(_best_cache) >= 8:                      # keep memory bounded while sliders move
            _best_cache.clear()
        _best_cache[sig] = {}
    cache = _best_cache[sig]
    db = connect(db_path)
    try:
        poll = cfg["poll_interval_sec"]
        n, last = db.execute("SELECT COUNT(*), MAX(cycle) FROM cycles").fetchone()
        if not last:
            return dict(status="NO DATA", poll=poll)
        sizes = cfg["trade_sizes_inr"]
        size = size if size in sizes else sizes[len(sizes) // 2]
        recent = db.execute("SELECT cycle, ts, usdtinr, usdtinr_spread, fx_source FROM cycles "
                            "ORDER BY cycle DESC LIMIT ?", (SERIES_N,)).fetchall()[::-1]
        ts = [r[1] for r in recent]
        gaps = [(b - a) / 1000 for a, b in zip(ts, ts[1:])]
        age = time.time() - ts[-1] / 1000
        status = "LIVE" if age < 3 * poll else ("STALE" if age < 30 * poll else "DOWN")

        first = recent[0][0]
        ex = {}
        for cyc, e, tot, ok in db.execute("SELECT cycle, exchange, markets_total, markets_ok FROM cycle_status "
                                          "WHERE cycle>=?", (first,)):
            d = ex.setdefault(e, dict(ok_sum=0, tot_sum=0, last_ok=0, last_tot=0, last_cycle=0))
            d["ok_sum"] += ok
            d["tot_sum"] += tot
            if cyc >= d["last_cycle"]:
                d.update(last_cycle=cyc, last_ok=ok, last_tot=tot)
        exchanges = [dict(name=e, ok=d["last_ok"], total=d["last_tot"],
                          uptime=round(100 * d["ok_sum"] / d["tot_sum"], 1) if d["tot_sum"] else 0.0,
                          seen_last=d["last_cycle"] == last)
                     for e, d in sorted(ex.items())]

        evs, qtys = evaluate(db, cost, cfg["analysis"], last, size)
        tax_keep = 1 - cost.tax_rate

        # every route with a gap right now, closest-to-profitable first
        routes = []
        for k, ev in evs:
            base, ea, qa, eb, qb = k
            notional = ev["notional"]
            qty = qtys.get((ea, qa, base)) or 0
            costs = ev["fees"] + ev["gst"] + ev["fx_leg"] + ev["transfer"]
            cost_bps = costs / notional * 1e4
            need_bps = cost_bps / tax_keep                 # gross gap at which net = 0 (tax is on the gap)
            routes.append(dict(
                base=base, buy=f"{ea} ({qa})", sell=f"{eb} ({qb})",
                buy_px=notional / qty if qty else None, sell_px=(notional + ev["gross"]) / qty if qty else None,
                gap_bps=ev["gross_bps"], need_bps=need_bps, short_bps=need_bps - ev["gross_bps"],
                gross=ev["gross"], fees=ev["fees"] + ev["gst"], fx=ev["fx_leg"], transfer=ev["transfer"],
                tax=ev["tax"], net=ev["net"], tds=ev["tds_locked"], notional=notional,
                net_xfer=scenario_net(ev, 0.0, 1.0), net_best=scenario_net(ev, 0.0, 0.5)))
        routes.sort(key=lambda r: r["short_bps"])

        now = summarize(evs)
        series = []
        for cyc, t, *_ in recent:
            key = (cyc, size)
            if key not in cache or cyc == last:
                cache[key] = now if cyc == last else summarize(evaluate(db, cost, cfg["analysis"], cyc, size)[0])
            series.append(dict(cycle=cyc, ts=t, **cache[key]))
        window = {k: sum(1 for s in series if s[k]["n"] > 0) for k, *_ in SCENARIOS}
        best_in_window = {k: max(((s[k]["net"], s[k]["route"]) for s in series if s[k]["net"] is not None),
                                 default=(None, None)) for k, *_ in SCENARIOS}

        return dict(
            status=status, age=round(age, 1), poll=poll, cycles=n, last_cycle=last, size=size, sizes=sizes,
            window_min=round(len(series) * poll / 60, 1), gap_median=round(median(gaps), 1) if gaps else None,
            gap_max=round(max(gaps), 1) if gaps else None, usdtinr=recent[-1][2], fx_source=recent[-1][4],
            fx_fallbacks=sum(1 for r in recent if r[4] == "fallback"),
            db_mb=round(os.path.getsize(db_path) / 1e6, 1), exchanges=exchanges, routes=routes[:12],
            now=now, scenarios=[dict(key=k, label=lbl) for k, lbl, *_ in SCENARIOS], series=series,
            window=len(series), window_pos=window, window_best=best_in_window,
            tax_pct=round(cost.tax_rate * 100, 1), cfg=cfg_view(cfg), cfg_default=cfg_view(base_cfg),
            modified=bool(overrides))
    finally:
        db.close()


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Arbitrage Monitor</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mut:#6a7280;--line:#e3e6ea;--ok:#12805c;--okbg:#e3f5ee;--bad:#c53030;--badbg:#fbeaea;--warn:#b7791f;--warnbg:#fbf1dc;--a:#2b6cb0;--b:#c05621;--c:#12805c}
@media (prefers-color-scheme:dark){:root{--bg:#0e1013;--card:#171a1f;--ink:#e8eaed;--mut:#8a93a1;--line:#272b33;--ok:#38b28a;--okbg:#12302a;--bad:#f06565;--badbg:#3a1d1d;--warn:#e0a63a;--warnbg:#3a2f14;--a:#63a3ec;--b:#f0a05a;--c:#38b28a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
main{max-width:1150px;margin:0 auto;padding:16px}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
h1{font-size:18px;margin:0;flex:1}.pill{padding:3px 10px;border-radius:99px;font-weight:600;font-size:12px;color:#fff}
.LIVE{background:var(--ok)}.STALE{background:var(--warn)}.DOWN,.NO{background:var(--bad)}
.verdict{border-radius:12px;padding:16px 18px;margin-bottom:12px;border:1px solid var(--line)}
.verdict.yes{background:var(--okbg);border-color:var(--ok)}.verdict.no{background:var(--badbg);border-color:var(--bad)}.verdict.maybe{background:var(--warnbg);border-color:var(--warn)}
.verdict h2{margin:0 0 4px;font-size:22px;text-transform:none;letter-spacing:0;color:var(--ink)}.verdict p{margin:2px 0;color:var(--ink)}.verdict .mut{font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:19px}.card span{color:var(--mut);font-size:12px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:12px;overflow-x:auto}
h2{font-size:13px;margin:0 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--line);white-space:nowrap}th:first-child,td:first-child,th.l,td.l{text-align:left}
th{color:var(--mut);font-weight:600;font-size:12px}.pos{color:var(--ok);font-weight:600}.neg{color:var(--bad)}.mut{color:var(--mut)}
svg{width:100%;height:190px;display:block}select{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:3px 6px}
.leg{font-size:12px;color:var(--mut);margin-top:4px}.leg i{display:inline-block;width:10px;height:3px;margin:0 4px 2px 10px}
.note{font-size:12px;color:var(--mut);margin:6px 0 0}
details>summary{cursor:pointer;font-weight:600}details[open]>summary{margin-bottom:10px}
.cfgs{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
.cfgs h3{margin:0 0 6px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.f{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:3px 0}
.f input{width:92px;text-align:right;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:3px 6px;font:inherit}
.f input.mod{border-color:var(--warn);background:var(--warnbg)}
button{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:4px 10px;font:inherit;cursor:pointer}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}.err{color:var(--bad);font-size:12px}
textarea{width:100%;height:150px;font:12px/1.4 ui-monospace,monospace;background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:6px;margin-top:8px}
</style></head><body><main>
<header><h1>Arbitrage Monitor <span class="mut" id="sub"></span></h1>
<label class="mut">trade size per leg <select id="size"></select></label><span class="pill" id="status">…</span></header>
<div class="verdict no" id="verdict"><h2>Loading…</h2></div>
<div class="grid" id="cards"></div>
<section><h2>Is there a trade? Same market data, three cost assumptions</h2>
<table id="scen"><thead><tr><th class="l">scenario</th><th>profitable routes now</th><th>best route net now</th><th>polls with a profitable route</th><th>best net seen in window</th></tr></thead><tbody></tbody></table>
<p class="note">"Your assumptions" start as config.yaml (a coin transfer on every trade, conservative); edit them below and 1% TDS is excluded from net but shown as locked cash. The other two rows ask: would anything work if transfers were rare, or fees were at a VIP tier?</p></section>
<section><details id="cfgbox"><summary>Assumptions: change fees, tax, TDS, transfers and see the result instantly <span class="mut" id="modtag"></span></summary>
<div class="cfgs" id="cfg"></div>
<div class="bar"><button id="reset">Reset to config.yaml</button><button id="yaml">Show as YAML for config.yaml</button><span class="err" id="cfgerr"></span></div>
<p class="note" id="impact" style="color:var(--ink);font-weight:600"></p>
<p class="note">Edits change only this page's calculation (the logger stores prices, not costs) and are remembered in this browser. Orange = differs from config.yaml.</p>
<textarea id="yamlout" hidden readonly></textarea></details></section>
<section><h2>Best route per poll, in bps of trade size (last <span id="win"></span> polls)</h2>
<svg id="chart" viewBox="0 0 1000 190" preserveAspectRatio="none"></svg>
<div class="leg"><i style="background:var(--a)"></i>gap (gross)<i style="background:var(--b)"></i>net with your assumptions<i style="background:var(--c)"></i>net with rare transfers + VIP fees<i style="background:var(--mut)"></i>break-even</div></section>
<section><h2>Routes with a price gap now, closest to profitable first</h2>
<table id="routes"><thead><tr><th class="l">asset</th><th class="l">buy on</th><th>buy price</th><th class="l">sell on</th><th>sell price</th><th>gap bps</th><th>needs bps</th><th>short by</th><th>costs ₹</th><th>net ₹</th><th>net, rare transfers</th><th>net, + VIP fees</th></tr></thead><tbody></tbody></table>
<p class="note">gap = what you'd capture before costs. needs = gap required to break even after fees, GST, FX, transfer and <span id="tax"></span>% tax on the gap. Prices are average fills for the full trade size, in ₹. Hover a "costs" cell for the split.</p></section>
<section><h2>Exchanges (latest poll; uptime over the window)</h2>
<table id="ex"><thead><tr><th class="l">exchange</th><th>markets ok</th><th>in latest poll</th><th>uptime %</th></tr></thead><tbody></tbody></table></section>
</main><script>
const $=id=>document.getElementById(id),inr=v=>v==null?"–":(v<0?"-":"")+"₹"+Math.abs(Math.round(v)).toLocaleString("en-IN"),
 px=v=>v==null?"–":"₹"+(v>=100?Math.round(v).toLocaleString("en-IN"):v.toFixed(v>=1?2:4)),bp=v=>v==null?"–":v.toFixed(1);
let size=null,last=null,timer=null;
// what-if edits: flat {"taker.binance":0.001,"tax.cess":0.04,"fx_fee":..,"rebalance":..,"wfee.USDT":..}, remembered in this browser
let OV={};try{OV=JSON.parse(localStorage.getItem("arb_ov")||"{}")||{}}catch(e){OV={}}
const saveOV=()=>{try{localStorage.setItem("arb_ov",JSON.stringify(OV))}catch(e){}};
function nested(){const o={};for(const [k,v] of Object.entries(OV)){const [a,b]=k.split(".");if(b===undefined)o[a]=v;else(o[a]??={})[b]=v}return o}
const FIELDS=[ // [group, key, label, unit, scale]
 ["Taker fee per exchange (%)",d=>Object.keys(d.taker).map(e=>["taker."+e,e,d.taker[e],100,0.01])],
 ["Tax and levies (%)",d=>[["tax.income_tax_rate","Income tax",d.tax.income_tax_rate,100,0.5],["tax.cess","Cess",d.tax.cess,100,0.5],["tax.surcharge","Surcharge",d.tax.surcharge,100,0.5],["tax.tds_rate","TDS (shown as locked cash)",d.tax.tds_rate,100,0.1],["tax.gst_on_fees","GST on fees",d.tax.gst_on_fees,100,1],["fx_fee","USDT↔INR leg fee",d.fx_fee,100,0.01]]],
 ["Transfers",d=>[["rebalance","Rebalance every N trades",d.rebalance,1,1],...Object.keys(d.wfee).map(c=>["wfee."+c,"Withdraw fee "+c+" (coins)",d.wfee[c],1,null])]]];
function buildPanel(d){const box=$("cfg");box.replaceChildren();
 FIELDS.forEach(([title,items])=>{const col=document.createElement("div");const h=document.createElement("h3");h.textContent=title;col.appendChild(h);
  items(d.cfg_default).forEach(([key,label,def,scale])=>{
   const eff=key in OV?OV[key]:def,f=document.createElement("label");f.className="f";const s=document.createElement("span");s.textContent=label;
   const inp=document.createElement("input");inp.type="number";inp.step="any";inp.min="0";inp.dataset.key=key;inp.value=+(eff*scale).toPrecision(6);inp.className=key in OV?"mod":"";
   inp.oninput=()=>{const v=parseFloat(inp.value);if(!isFinite(v)||v<0)return;const frac=v/scale;
    if(Math.abs(frac-def)<1e-12){delete OV[key];inp.className=""}else{OV[key]=frac;inp.className="mod"}
    saveOV();$("modtag").textContent="(recalculating…)";clearTimeout(timer);timer=setTimeout(tick,400)};
   f.append(s,inp);col.appendChild(f)});box.appendChild(col)})}
function yamlOf(c){const L=["# paste over the matching sections of config.yaml","tax:"];
 for(const k of ["income_tax_rate","cess","surcharge","tds_rate","gst_on_fees"])L.push(`  ${k}: ${c.tax[k]}`);
 L.push("fx:",`  fx_leg_taker_fee: ${c.fx_fee}`,"analysis:",`  rebalance_every_n_trades: ${c.rebalance}`,"withdraw_fee:");
 for(const [k,v] of Object.entries(c.wfee))L.push(`  ${k}: ${v}`);
 L.push("# in exchanges: set taker_fee per exchange");for(const [k,v] of Object.entries(c.taker))L.push(`#   ${k}: taker_fee: ${v}`);return L.join("\n")}
function td(tr,txt,cls,title){const e=document.createElement("td");e.textContent=txt;if(cls)e.className=cls;if(title)e.title=title;tr.appendChild(e);return e}
function row(cells){const tr=document.createElement("tr");cells.forEach(c=>td(tr,...c));return tr}
function chart(s){const svg=$("chart");const ks=[["gross_bps",null,"var(--a)"],["real","bps","var(--b)"],["best","bps","var(--c)"]];
 const val=(x,k,f)=>f?x[k][f]:x[k];const vals=s.flatMap(x=>ks.map(([k,f])=>val(x,k,f))).filter(v=>v!=null);
 if(!vals.length){svg.replaceChildren();return}
 let lo=Math.min(0,...vals),hi=Math.max(0,...vals);if(hi-lo<1){hi+=1;lo-=1}
 const X=i=>s.length<2?500:i*1000/(s.length-1),Y=v=>180-(v-lo)/(hi-lo)*170;
 const line=([k,f,c])=>{const pts=s.map((x,i)=>val(x,k,f)==null?null:X(i)+","+Y(val(x,k,f))).filter(Boolean).join(" ");return `<polyline fill="none" stroke="${c}" stroke-width="2" vector-effect="non-scaling-stroke" points="${pts}"/>`};
 svg.innerHTML=`<line x1="0" x2="1000" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--mut)" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>`+ks.map(line).join("")+
 `<text x="4" y="14" fill="var(--mut)" font-size="12">${hi.toFixed(0)} bps</text><text x="4" y="186" fill="var(--mut)" font-size="12">${lo.toFixed(0)} bps</text>`}
function verdict(d){const v=$("verdict"),n=d.now,w=d.window_pos,r=d.routes[0];
 const rn=n.real,xn=n.xfer,bn=n.best;
 if(rn.n>0){v.className="verdict yes";v.innerHTML=`<h2>YES: ${rn.n} route${rn.n>1?"s":""} profitable after all costs right now</h2><p></p><p class="mut"></p>`;
  v.children[1].textContent=`Best: ${rn.route} nets ${inr(rn.net)} on a ${inr(d.size*1)} leg (${bp(rn.bps)} bps). TDS cash locked is extra.`;
  v.children[2].textContent=`Seen in ${w.real} of the last ${d.window} polls. Check the buy and sell prices below are real depth, and that withdrawals are open.`;return}
 const alt=bn.n>0?"maybe":"no";v.className="verdict "+alt;
 const gap=r?`Closest route: ${r.base} buy ${r.buy} → sell ${r.sell}. Gap ${bp(r.gap_bps)} bps, needs ${bp(r.need_bps)} bps, short by ${bp(r.short_bps)} bps (${inr(r.net)}).`:"No venue pair has sell above buy at this size right now.";
 v.innerHTML=`<h2>${alt=="maybe"?"NOT with your assumptions, but possible with better terms":"NO: no profitable trade right now"}</h2><p></p><p class="mut"></p>`;
 v.children[1].textContent=gap;
 v.children[2].textContent=`Last ${d.window_min} min: ${w.real}/${d.window} polls profitable with your assumptions, ${w.xfer}/${d.window} with rare transfers, ${w.best}/${d.window} with rare transfers and VIP fees.`+(bn.n>0?` Now, with those terms: ${bn.n} route(s), best ${bn.route} nets ${inr(bn.net)}.`:"")}
async function impact(d){const el=$("impact");
 if(!d.modified){el.textContent="";return}
 try{const b=await(await fetch(`/api/state?size=${size||""}&o=%7B%7D`)).json();const r=d.routes[0],r0=b.routes[0];
  const mine=[d.now.real.net,r&&r.short_bps,d.window_pos.real,d.now.real.n],base=[b.now.real.net,r0&&r0.short_bps,b.window_pos.real,b.now.real.n];
  el.textContent=JSON.stringify(mine)===JSON.stringify(base)
   ?"No effect yet: the fields you changed don't touch any route with a gap right now (those exchanges or coins aren't in the routes below). Try the buy/sell exchanges listed in the table."
   :`Effect of your changes: best route net now ${inr(d.now.real.net)} (config.yaml: ${inr(b.now.real.net)}) · closest route short by ${bp(r&&r.short_bps)} bps (was ${bp(r0&&r0.short_bps)}) · profitable polls ${d.window_pos.real}/${d.window} (was ${b.window_pos.real}/${b.window})`}catch(e){}}
async function tick(){
 try{const o=encodeURIComponent(JSON.stringify(nested()));
 const r=await fetch(`/api/state?size=${size||""}&o=${o}`);const d=await r.json();
 if(!r.ok){$("cfgerr").textContent="Invalid value: "+d.error;return}$("cfgerr").textContent="";
 last=d;if(!$("cfg").children.length)buildPanel(d);$("modtag").textContent=d.modified?"(custom assumptions active)":"";impact(d);
 $("status").textContent=d.status;$("status").className="pill "+d.status.split(" ")[0];
 if(!d.cycles){$("verdict").className="verdict no";$("verdict").innerHTML="<h2>No data yet. Is the logger running?</h2>";return}
 $("sub").textContent="· polls every "+d.poll+"s · updated "+new Date().toLocaleTimeString();
 if(!$("size").options.length)d.sizes.forEach(v=>$("size").add(new Option(inr(v),v)));
 $("size").value=d.size;size=d.size;$("tax").textContent=d.tax_pct;verdict(d);
 const cards=[["Last poll",d.age+"s ago",`cycle ${d.last_cycle.toLocaleString()} · gap median ${d.gap_median}s`],["Cycles logged",d.cycles.toLocaleString(),`${d.db_mb} MB database`],
  ["USDT/INR",d.usdtinr.toFixed(2),d.fx_source+(d.fx_fallbacks?` · ${d.fx_fallbacks} fallback`:"")],
  ["Best gap now",bp(d.now.gross_bps)+" bps",`${d.now.n_gap} routes with sell > buy`]];
 $("cards").replaceChildren(...cards.map(([a,b,c])=>{const e=document.createElement("div");e.className="card";e.innerHTML="<span></span><b></b><span></span>";
  e.children[0].textContent=a;e.children[1].textContent=b;e.children[2].textContent=c;return e}));
 $("scen").tBodies[0].replaceChildren(...d.scenarios.map(s=>{const n=d.now[s.key],wb=d.window_best[s.key];
  return row([[s.label,"l"],[n.n,n.n?"pos":"neg"],[inr(n.net)+(n.route?"  "+n.route.split(":")[0]:""),n.net>0?"pos":"neg"],[`${d.window_pos[s.key]} / ${d.window}`,d.window_pos[s.key]?"pos":"mut"],[inr(wb[0]),wb[0]>0?"pos":"mut"]])}));
 $("win").textContent=d.window;chart(d.series);
 $("routes").tBodies[0].replaceChildren(...(d.routes.length?d.routes.map(t=>{const tr=row([[t.base,"l"],[t.buy,"l"],[px(t.buy_px)],[t.sell,"l"],[px(t.sell_px)],[bp(t.gap_bps)],[bp(t.need_bps),"mut"],[bp(t.short_bps),t.short_bps<=0?"pos":"neg"]]);
  td(tr,inr(t.fees+t.fx+t.transfer+t.tax),"mut",`fees+GST ${inr(t.fees)} · FX ${inr(t.fx)} · transfer ${inr(t.transfer)} · tax ${inr(t.tax)} · TDS locked ${inr(t.tds)} (not in net)`);
  td(tr,inr(t.net),t.net>0?"pos":"neg");td(tr,inr(t.net_xfer),t.net_xfer>0?"pos":"mut");td(tr,inr(t.net_best),t.net_best>0?"pos":"mut");return tr}):[row([["no route has sell above buy at this size","l"]])]));
 $("ex").tBodies[0].replaceChildren(...d.exchanges.map(e=>row([[e.name,"l"],[`${e.ok}/${e.total}`,e.ok<e.total?"neg":"pos"],[e.seen_last?"yes":"missing",e.seen_last?"pos":"neg"],[e.uptime.toFixed(1)]])));
 }catch(e){$("status").textContent="MONITOR ERROR";$("status").className="pill DOWN"}}
$("size").onchange=e=>{size=+e.target.value;tick()};
$("reset").onclick=()=>{OV={};saveOV();if(last)buildPanel(last);tick()};
$("yaml").onclick=()=>{if(!last)return;const t=$("yamlout");t.value=yamlOf(last.cfg);t.hidden=false;t.select();try{navigator.clipboard.writeText(t.value)}catch(e){}};
tick();setInterval(tick,5000);
</script></body></html>"""


def make_handler(cfg, db_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self.send(200, "text/html; charset=utf-8", PAGE.encode())
            if u.path == "/favicon.ico":
                return self.send(204, "image/x-icon", b"")
            if u.path == "/api/state":
                try:
                    qs = parse_qs(u.query)
                    q = qs.get("size", [""])[0]
                    o = json.loads(qs.get("o", ["{}"])[0])
                    if not isinstance(o, dict):
                        raise ValueError("overrides must be an object")
                    body = json.dumps(state(cfg, db_path, int(q) if q.isdigit() else 0, o)).encode()
                    return self.send(200, "application/json", body)
                except (ValueError, TypeError, KeyError) as e:   # bad overrides from the UI
                    return self.send(400, "application/json", json.dumps(dict(error=str(e))).encode())
                except sqlite3.OperationalError as e:   # db missing / not created yet
                    return self.send(200, "application/json", json.dumps(dict(status="NO DATA", error=str(e))).encode())
            self.send(404, "text/plain", b"not found")
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default=None)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="keep 127.0.0.1 unless you add auth")
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = yaml.safe_load(f)
    db_path = a.db or cfg["db_path"]
    server = ThreadingHTTPServer((a.host, a.port), make_handler(cfg, db_path))
    print(f"monitor on http://{a.host}:{a.port}  (db: {db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
