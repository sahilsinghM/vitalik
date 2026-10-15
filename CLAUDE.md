# CLAUDE.md

Cross-exchange crypto arbitrage research for an Indian resident (₹10L paper capital, no real
money, no order placement). Code lives in `arb_logger/`. Goal is learning; a paper-trading engine
is the next build (see `docs/design-paper-trading-engine.md`, status APPROVED).

## Commands (run from `arb_logger/`)

```bash
pip install -r requirements.txt
python logger.py --simulate --cycles 300          # offline pipeline check (writes data/spreads_sim.sqlite)
python logger.py --quiet --full-books --depth 10  # live logging -> data/spreads.sqlite
python monitor.py                                 # dashboard at http://127.0.0.1:8765
python analyze.py                                 # paper-trade the log -> reports/report.md
```

There is no test suite yet. The first tests to write are T1 to T4 in `docs/test-plan-paper-trading-engine.md`
(ledger vs `CostModel.evaluate`, what-if identity, determinism, conservation), before any engine code.

## Architecture

- `logger.py` polls public order books every 10 s, stores VWAP fills at 3 trade sizes, plus
  `cycle_status` (outage vs empty market) and optional `levels` (top N book levels, `--full-books`).
- `arb/costs.py` `CostModel.evaluate` is a pure function that prices one buy-on-A/sell-on-B route in INR.
  It returns each cost component separately. Reuse it; do not fork it. In prefunded mode it already
  amortises transfer cost over `rebalance_every`, so an engine that also charges real rebalances must
  not charge transfers twice.
- `arb/routes.py` builds routes and INR conversion; `arb/store.py` is the SQLite schema (additive
  changes only: `CREATE TABLE IF NOT EXISTS`, no migrations).
- `monitor.py` is read-only (SQLite opened with `mode=ro`), stdlib only, bound to 127.0.0.1.
  Assumption edits are per-request overrides from the browser; it must never write `config.yaml`.

## Gotchas

- Stored data is top-of-book plus VWAP at 3 fixed sizes, not full books (unless `--full-books`). Replay
  fills are therefore the logger's own VWAP plus noise, not independent evidence.
- ZebPay USDT books are empty (8 of 14 markets answer); OKX is unreachable from this machine.
  Both are expected, not outages.
- Cost defaults in `config.yaml` are mostly unverified. Verified 2026-10-15: Binance taker 0.1%,
  ZebPay taker 0.45% and USDT TRC-20 withdrawal 8. CoinDCX returned 403 on its fee page.
- Tax model is a spread-gain approximation (31.2% of each trade's gross gain), not lot-based
  capital gains. TDS is reported as locked cash, not in net.
- Never run `pkill -f` or `pgrep -f` with a pattern that appears in your own command text: it matches
  the shell running it and kills it. Use the bracket trick (`pgrep -f '[l]ogger\.py'`) or find the
  process by port (`ss -ltnp 'sport = :8765'`). Stop the logger with `kill -INT` so it closes cleanly.
- `data/`, `reports*/`, `*.log` are gitignored. Live data is local only.

## Conventions

- Stdlib first; the only third-party deps are ccxt, aiohttp, pyyaml. Keep it that way unless justified.
- Money is INR floats; assert conservation with an epsilon, never exact equality.
- Anything random must be seeded from a stable key so same log + same seed gives the same result.
- Do not trade real money or add order-placement code without an explicit decision; BR/PRD rules
  (30% cap per exchange, kill switch, trade-only keys with withdrawals disabled) apply first.
- Git: single `master` branch, no remote configured yet.
