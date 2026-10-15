# Changelog

All notable changes to this project. Format follows Keep a Changelog; dates are 2026.

## [0.2.0] - 2026-10-15

### Added
- `monitor.py`: read-only trader dashboard (verdict banner, three what-if scenarios, per-poll chart,
  routes with real buy/sell prices and break-even gap, exchange health).
- Assumptions panel in the monitor: edit fees, tax, TDS, GST, FX leg fee, rebalance frequency and
  withdrawal fees live; "Effect of your changes" line; Reset; "Show as YAML". Edits stay in the browser.
- `logger.py --full-books [--depth N]`: store the top N order-book levels per side (`levels` table).
- `cycle_status` table: per poll and exchange, markets answering vs expected (distinguishes outage
  from an empty market).
- `docs/`: approved design for the paper-trading engine, test plan, deferred TODOs.
- `CLAUDE.md`, `CHANGELOG.md`, `.gitignore`.

### Changed
- `config.yaml`: ZebPay taker fee 0.5% to 0.45% and USDT withdrawal override 8 (verified against
  zebpay.com/in/fees); Binance taker 0.1% confirmed. Verification notes added.
- README rewritten: monitor, new flags, verified-fee table, first findings.

### Verified
- Live logging works on 8 of 9 configured exchanges (OKX unreachable from this machine); ~27 minutes /
  163 polls collected. No route profitable with default costs; best gaps 0 to 15 bps vs 45 to 75 bps
  needed to break even.
- Old databases open unchanged after the schema additions.

### Known issues
- No tests yet; no git remote; CoinDCX and most other fee defaults still unverified.
- ZebPay shows 8/14 markets in the monitor because its USDT books are empty (expected).

## [0.1.0] - 2026-10-14

### Added
- Initial read-only spread logger: `logger.py`, `analyze.py`, `arb/` (cost engine, routes, venues,
  store), `config.yaml`. Simulator mode for offline testing.
