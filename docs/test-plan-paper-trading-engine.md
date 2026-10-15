# Test plan: paper-trading engine (from /autoplan eng review)
Order: T1-T4 first, before any engine code.
T1 ledger-vs-evaluate reconcile (INR/INR, USDT/USDT, cross-quote, same-exchange cross-quote; transfer excluded)
T2 what-if identity (factor 1, no toggles == baseline); per-toggle effect
T3 determinism: same log + seed = same ledger; different PYTHONHASHSEED; restart mid-log == straight run
T4 conservation each step incl. in-flight rebalance
T5 TDS decay: capital shrinks 1% per sell, cap denominator
Paths: pricing tie-break; guard (NaN/0/outlier, fx fallback, ts gap); decision (min_net_bps, cooldown vs episode, parity with analyze.py on --simulate fixture); cap/balance/5L reject/size fallback; pending reserve + cancel (gap/outage/end of data); fills seeded; ledger tax winner/loser; rebalancer (trigger <20%, unavailable window, ETA arrival, in-flight at end, FX path, no double count vs evaluate.move); kill switch (stale, outage, loss limit, sticky, IST day, restart); persist (BR-9 row completeness, snapshot round-trip + cycle continuity); feed replay (ordering, down record, None at end); feed live with mocked adapters (one market fails, exchange timeout, next-poll book walk); store migrate (old DB opens, levels table round-trips); ticker closest-to-profitable with negative gross.
