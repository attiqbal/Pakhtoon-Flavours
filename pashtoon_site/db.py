"""SQLite storage. One small file holds the whole business."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS menu_items (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', image TEXT, notice_hours INTEGER NOT NULL DEFAULT 24,
  available INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS menu_options (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES menu_items(id), label TEXT NOT NULL,
  price REAL NOT NULL, sort INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, customer_name TEXT NOT NULL, phone TEXT NOT NULL,
  email TEXT, fulfilment TEXT NOT NULL, address TEXT, postcode TEXT, miles REAL,
  delivery_fee REAL NOT NULL DEFAULT 0, food_total REAL NOT NULL, total REAL NOT NULL,
  date TEXT NOT NULL, time TEXT NOT NULL, payment_method TEXT NOT NULL,
  payment_status TEXT NOT NULL DEFAULT 'unpaid', status TEXT NOT NULL DEFAULT 'confirmed',
  notes TEXT, source TEXT NOT NULL DEFAULT 'web', stock_used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), option_id INTEGER,
  dish TEXT NOT NULL, portion TEXT NOT NULL, qty INTEGER NOT NULL, unit_price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY, token TEXT UNIQUE NOT NULL, channel TEXT NOT NULL DEFAULT 'web',
  contact TEXT, name TEXT, messages TEXT NOT NULL DEFAULT '[]', pending TEXT NOT NULL DEFAULT '[]',
  ai_paused INTEGER NOT NULL DEFAULT 0, needs_owner INTEGER NOT NULL DEFAULT 0, unread INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_log (
  id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL REFERENCES conversations(id),
  sender TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS owner_chat (id INTEGER PRIMARY KEY, messages TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ingredients (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL COLLATE NOCASE, unit TEXT NOT NULL,
  stock REAL NOT NULL DEFAULT 0, low_at REAL NOT NULL DEFAULT 0, cost_per_unit REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS recipe_lines (
  id INTEGER PRIMARY KEY, option_id INTEGER NOT NULL REFERENCES menu_options(id),
  ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), qty REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stock_moves (
  id INTEGER PRIMARY KEY, ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), kind TEXT NOT NULL,
  qty REAL NOT NULL, cost REAL NOT NULL DEFAULT 0, reason TEXT, order_id INTEGER, date TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, category TEXT NOT NULL, description TEXT NOT NULL,
  amount REAL NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, link TEXT, seen INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS orders_date ON orders(date);
CREATE INDEX IF NOT EXISTS chat_conv ON chat_log(conversation_id);
"""

_local = threading.local()
_path = config.DB_PATH


def use(path: str) -> None:
    """Point at another database file (tests use a temporary one)."""
    global _path
    _path = path
    _local.__dict__.clear()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None or getattr(_local, "path", None) != _path:
        if _path != ":memory:":
            os.makedirs(os.path.dirname(_path) or ".", exist_ok=True)
        c = sqlite3.connect(_path, check_same_thread=False, isolation_level=None, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA journal_mode=WAL")
        _local.c, _local.path = c, _path
    return c


def init() -> None:
    conn().executescript(SCHEMA)


def q(sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql: str, args: tuple = ()) -> dict | None:
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def execute(sql: str, args: tuple = ()) -> int:
    cur = conn().execute(sql, args)
    return cur.lastrowid


@contextmanager
def tx():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def get_setting(key: str, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
