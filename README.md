# vitalik: cross-exchange crypto arbitrage research (India, paper only)

A read-only research project: do price gaps between crypto exchanges survive every cost an Indian
resident pays (taker fees, 18% GST, USDT↔INR conversion, withdrawal fees, 30% + 4% cess tax, 1% TDS,
transfer delays)? It logs live order books, prices each route after all costs, and shows the result
in a local trader dashboard. **It places no orders, needs no API keys, and holds no money.**

First finding (about 27 minutes of live data, so anecdotal): best gaps were 0 to 15 bps while a route
needs roughly 45 to 75 bps to break even, so no route was profitable at default costs.

## Quick start

```bash
cd arb_logger
pip install -r requirements.txt
python logger.py --quiet --full-books --depth 10   # live logging -> data/spreads.sqlite
python monitor.py                                  # dashboard: http://127.0.0.1:8765
python analyze.py                                  # paper-trade the log -> reports/report.md
```

Add `--simulate` to `logger.py` to run offline against synthetic markets.

## What's here

| Path | What |
|---|---|
| [`arb_logger/`](arb_logger/README.md) | the logger, cost engine, analyzer and monitor (full usage in its README) |
| [`docs/design-paper-trading-engine.md`](docs/design-paper-trading-engine.md) | approved design for the next build: a paper-trading engine |
| [`docs/test-plan-paper-trading-engine.md`](docs/test-plan-paper-trading-engine.md) | tests to write first |
| [`docs/TODOS.md`](docs/TODOS.md) | deferred work |
| [`CHANGELOG.md`](CHANGELOG.md) | what changed and when |
| [`CLAUDE.md`](CLAUDE.md) | guidance for AI coding agents working in this repo |

## Status

- Logger and monitor: working on 8 of 9 configured exchanges (OKX is unreachable from some networks).
- Fee defaults: Binance and ZebPay verified against official pages; the rest are estimates, marked
  `VERIFY` in `arb_logger/config.yaml`.
- Next: paper-trading engine (ledger, simulated fills, kill switch). No tests exist yet.

Not financial or tax advice. Tax treatment of crypto in India is contestable; confirm with a CA.
