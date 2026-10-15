# Cross-exchange spread logger and monitor (read-only, paper ₹10L)

Watches live order books for BTC, ETH, BNB, XRP, SOL, TRX, ZEC, USDC (and USDT/INR) across
global and Indian exchanges, and works out what each price gap would actually net an
Indian resident after **every** cost:

- taker fees on both legs, +18% GST on fees
- the USDT↔INR conversion trade when the two venues quote in different currencies
- blockchain withdrawal fees (+GST) to move coins/cash between exchanges
- **blockchain transfer time**: in transfer mode the coin is sold on the second exchange only
  after it has confirmed, at whatever the price is by then
- **execution lag**: you see a gap on one poll, your orders fill at the next poll's prices
- order-book depth: prices are the average fill for the full trade size, not the top quote
- 30% tax + 4% cess on each profitable trade's gross gain (no loss set-off, fees not deductible)
- 1% TDS on every crypto transfer, including buying a coin with USDT. Reported separately,
  because you can claim it back in your ITR, but it locks capital until then.

It places **no orders** and needs **no API keys**. Order books come from each exchange's public
API (ccxt for most, a small custom adapter for CoinDCX).

## Run it

Python 3.10+ and an internet connection. A laptop works while it stays awake; a small VM in
Mumbai is better for multi-day runs.

```bash
pip install -r requirements.txt

python logger.py --simulate --cycles 300      # 1. check everything works offline
python logger.py --quiet --full-books --depth 10   # 2. live; leave running for days
python monitor.py                             # 3. trader view at http://127.0.0.1:8765
python analyze.py                             # 4. any time: writes reports/report.md
```

`logger.py` flags: `--simulate`, `--cycles N`, `--quiet`, `-v` (verbose; very noisy),
`--full-books` (also store the top `--depth` order-book levels per side, ~0.85 GB/day at depth 10),
`--config PATH`.

If an exchange is unreachable or a market is not listed, it is skipped (`-v` shows why).
OKX is unreachable from some networks and is skipped automatically.

## Monitor (`monitor.py`)

A local web page (stdlib only) that reads the logger's SQLite **read-only**, so it cannot
disturb the logger. It shows:

- a verdict banner: is any route profitable after all costs right now, and how far the closest
  route is from break-even
- three what-if scenarios on the same data: your assumptions, transfers amortised, and VIP fees
- a chart of the best route per poll (gap, net, net under optimistic terms)
- every route with a price gap: real buy/sell prices, gap, gap needed to break even, costs, net
- per-exchange health (markets answering, uptime)
- an **Assumptions** panel: edit taker fees per exchange, tax/TDS/GST, FX leg fee, rebalance
  frequency and per-coin withdrawal fees; results recalculate within seconds. Edits live in your
  browser only (`localStorage`); the monitor never writes `config.yaml`. "Show as YAML" gives a
  block to paste into `config.yaml`. An "Effect of your changes" line says when an edit has no
  effect on any route currently showing.

It listens on `127.0.0.1` only. Do not expose it without adding authentication.

## Reading the report (`analyze.py`)

For each trade size (₹50k / ₹2L / ₹5L per leg) and each model:

- **prefunded**: money already sits on both exchanges, and both legs fire together.
  Inventory is moved back with a withdrawal every N trades (`rebalance_every_n_trades`).
- **transfer**: buy on A, withdraw the coin to B, sell after it confirms.

The "where the spread goes" table is the most useful part. It shows in basis points how much
of the average gap is consumed by fees, GST, the FX leg, transfers and tax. If net is negative
there for the routes you care about, the strategy doesn't work on those venues at that size.

Capital model: ₹10L limits you to `capital / (2 × trade size)` simultaneous trades. Only one
trade is counted per continuous "episode" of a gap, so a gap that stays open for 20 polls
counts once.

## What the data says so far

About 27 minutes of live data (163 polls, 15 Oct 2026), so anecdotal. Best gaps were 0 to 15 bps
(one spike near 88 bps); a route needs roughly 45 to 75 bps to break even. Zero routes were
profitable with the default costs. With market-maker-level fees (0.02%) and rare transfers, about
1 in 9 polls had a profitable route. Collect days of data before drawing conclusions.

## Cost defaults: what is verified

`config.yaml` costs are best-effort estimates except where noted (checked 2026-10-15):

| Item | Status |
|---|---|
| Binance spot taker 0.1% | verified (binance.com/en/fee/schedule) |
| ZebPay spot taker 0.45%, USDT withdrawal 8 (TRC-20) | verified (zebpay.com/in/fees) |
| CoinDCX fees, other exchanges, transfer minutes, FX leg fee | **not verified** (`VERIFY`) |

Check the rest against each exchange's fee page before trusting rupee figures. Your tax position
may differ (surcharge, business income treatment), so confirm with a CA.

## APIs: what you need, and when

| Stage | Keys needed | Notes |
|---|---|---|
| **Now: logging and monitoring** | **None** | Order books are public endpoints. |
| Exact withdrawal fees and deposit/withdraw status | Read-only key per exchange | Suspended withdrawals are the main reason big gaps exist. |
| Live trading (much later, only if numbers justify it) | Key with **Read + Spot trade** | Keep **withdrawals disabled**, bind to your server's IP, one sub-account per exchange. |

Store keys in environment variables, never in code. Before opening accounts on offshore
exchanges, check they are registered with **FIU-IND**.

## Files

```
config.yaml     all assumptions: capital, sizes, fees, tax, transfer times
logger.py       polls books -> data/spreads.sqlite (cycles, quotes, cycle_status, levels)
monitor.py      read-only trader dashboard (stdlib http.server)
analyze.py      paper-trades the log -> reports/
arb/costs.py    the cost engine (one pure function, easy to audit)
arb/routes.py   route enumeration, INR conversion, reference mids
arb/venues.py   exchange adapters + simulator
arb/store.py    SQLite schema
```

Project docs live in `../docs/` (approved design for the paper-trading engine, test plan).
