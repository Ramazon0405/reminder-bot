"""SQLite storage layer for the reminder bot.

Simple synchronous sqlite3 (fast enough for a personal bot - all calls run on
one event loop thread). opportunistically enables WAL so concurrent reads are safe.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "data/bot.db")

_conn: sqlite3.Connection | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init() -> None:
    """Create the database and tables (idempotent)."""
    global _conn
    if DB_PATH != ":memory:":
        directory = os.path.dirname(DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    if DB_PATH != ":memory:":
        _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            chat_id        INTEGER NOT NULL,
            name           TEXT NOT NULL DEFAULT '',
            charge_amount  INTEGER NOT NULL DEFAULT 0,
            balance        INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            chat_id   INTEGER NOT NULL,
            fire_at   TEXT NOT NULL,                       -- UTC ISO-8601
            label     TEXT NOT NULL DEFAULT '',
            status    TEXT NOT NULL DEFAULT 'pending',     -- pending|fired|done|skipped|compensated|cancelled
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_reminders_status ON reminders(status);

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            reminder_id INTEGER,
            kind        TEXT NOT NULL,                     -- done|skip|compensate
            delta       INTEGER NOT NULL,                  -- balance change in so'm
            at          TEXT NOT NULL                      -- UTC ISO-8601
        );
        CREATE INDEX IF NOT EXISTS ix_events_user_at ON events(user_id, at);

        CREATE TABLE IF NOT EXISTS kv (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        """
    )
    _conn.commit()


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- users

def get_user(user_id: int) -> dict | None:
    cur = _conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return _row_to_dict(cur.fetchone())


def upsert_user(user_id: int, chat_id: int, name: str, charge_amount: int) -> None:
    existing = get_user(user_id)
    if existing is None:
        _conn.execute(
            "INSERT INTO users (user_id, chat_id, name, charge_amount, balance, created_at)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (user_id, chat_id, name, charge_amount, _now_iso()),
        )
    else:
        _conn.execute(
            "UPDATE users SET chat_id = ?, name = ?, charge_amount = ? WHERE user_id = ?",
            (chat_id, name, charge_amount, user_id),
        )
    _conn.commit()


def add_balance(user_id: int, delta: int) -> int:
    """Apply delta to the user's outstanding balance, return the new balance."""
    row = _conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return 0
    new_balance = row["balance"] + delta
    _conn.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
    _conn.commit()
    return new_balance


def all_users() -> list[dict]:
    cur = _conn.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE")
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------- reminders

def create_reminder(user_id: int, chat_id: int, fire_at_utc_iso: str, label: str) -> int:
    cur = _conn.execute(
        "INSERT INTO reminders (user_id, chat_id, fire_at, label, status, created_at)"
        " VALUES (?, ?, ?, ?, 'pending', ?)",
        (user_id, chat_id, fire_at_utc_iso, label, _now_iso()),
    )
    _conn.commit()
    return cur.lastrowid


def get_reminder(reminder_id: int) -> dict | None:
    cur = _conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
    return _row_to_dict(cur.fetchone())


def set_reminder_status(reminder_id: int, status: str) -> None:
    _conn.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))
    _conn.commit()


def user_reminders(user_id: int, statuses: tuple[str, ...]) -> list[dict]:
    placeholders = ",".join("?" for _ in statuses)
    cur = _conn.execute(
        f"SELECT * FROM reminders WHERE user_id = ? AND status IN ({placeholders})"
        " ORDER BY fire_at",
        (user_id, *statuses),
    )
    return [dict(r) for r in cur.fetchall()]


def all_pending() -> list[dict]:
    """Every pending reminder - used to re-schedule jobs after a restart."""
    cur = _conn.execute("SELECT * FROM reminders WHERE status = 'pending' ORDER BY fire_at")
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------- events

def add_event(user_id: int, reminder_id: int | None, kind: str, delta: int) -> None:
    _conn.execute(
        "INSERT INTO events (user_id, reminder_id, kind, delta, at) VALUES (?, ?, ?, ?, ?)",
        (user_id, reminder_id, kind, delta, _now_iso()),
    )
    _conn.commit()


def events_since(since_utc_iso: str) -> list[dict]:
    cur = _conn.execute(
        "SELECT * FROM events WHERE at >= ? ORDER BY at", (since_utc_iso,)
    )
    return [dict(r) for r in cur.fetchall()]


# ------------------------------------------------------------------- kv

def get_kv(key: str) -> str | None:
    cur = _conn.execute("SELECT v FROM kv WHERE k = ?", (key,))
    row = cur.fetchone()
    return row["v"] if row is not None else None


def set_kv(key: str, value: str) -> None:
    _conn.execute(
        "INSERT INTO kv (k, v) VALUES (?, ?)"
        " ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, value),
    )
    _conn.commit()
