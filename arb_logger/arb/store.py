import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles(
  cycle INTEGER PRIMARY KEY, ts INTEGER, usdtinr REAL, usdtinr_spread REAL, fx_source TEXT);
CREATE TABLE IF NOT EXISTS quotes(
  cycle INTEGER, ts INTEGER, exchange TEXT, quote TEXT, base TEXT, size_inr INTEGER,
  bid REAL, ask REAL,              -- top of book, native quote currency
  vwap_buy REAL, vwap_sell REAL,   -- avg fill for size_inr, native quote currency
  qty REAL, full_buy INTEGER, full_sell INTEGER);
-- per poll and exchange: how many of its markets returned a usable book (outage vs empty market)
CREATE TABLE IF NOT EXISTS cycle_status(
  cycle INTEGER, exchange TEXT, markets_total INTEGER, markets_ok INTEGER,
  PRIMARY KEY(cycle, exchange));
-- optional (--full-books): top N levels per side as a JSON list of [price, size]
CREATE TABLE IF NOT EXISTS levels(
  cycle INTEGER, exchange TEXT, quote TEXT, base TEXT, side TEXT, levels_json TEXT,
  PRIMARY KEY(cycle, exchange, quote, base, side));
CREATE INDEX IF NOT EXISTS q_cycle ON quotes(cycle);
CREATE INDEX IF NOT EXISTS q_lookup ON quotes(exchange, quote, base, size_inr, ts);
"""


def connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def next_cycle(db):
    r = db.execute("SELECT COALESCE(MAX(cycle), 0) FROM cycles").fetchone()
    return r[0] + 1
