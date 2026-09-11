"""
Database adapter — Postgres in production, SQLite locally.

The app was written against sqlite3's API (conn.execute(...) with ? placeholders
and dict-like rows). This module keeps that exact surface so the rest of the app
is unchanged, while talking to Postgres when DATABASE_URL is present.

Postgres is preferred because it lives in its own Railway service: deploys cannot
overwrite it, and it is backed up. A SQLite file inside the deploy image is not.
"""
import os
import re
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras

# Railway sometimes provides the legacy postgres:// scheme
if USE_PG and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_PLACEHOLDER = re.compile(r"\?")


def _to_pg(sql: str) -> str:
    """Translate the SQLite dialect the app is written in into Postgres."""
    sql = _PLACEHOLDER.sub("%s", sql)
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("AUTOINCREMENT", "")
    return sql


class _Result:
    """Wraps a cursor so callers can use .fetchone()/.fetchall()/.rowcount
    exactly as they do with sqlite3."""

    __slots__ = ("_cur", "rowcount")

    def __init__(self, cur):
        self._cur = cur
        self.rowcount = cur.rowcount

    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if (USE_PG and row is not None) else row

    def fetchall(self):
        rows = self._cur.fetchall()
        return [dict(r) for r in rows] if USE_PG else rows

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """sqlite3-shaped connection that may be backed by Postgres."""

    def __init__(self):
        if USE_PG:
            self._c = psycopg2.connect(DATABASE_URL)
        else:
            self._c = sqlite3.connect(DB_FILE)
            self._c.row_factory = sqlite3.Row

    # -- sqlite3 compatibility ------------------------------------------------
    def execute(self, sql, params=()):
        if USE_PG:
            cur = self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(_to_pg(sql), params)
            return _Result(cur)
        return self._c.execute(sql, params)

    def executescript(self, script):
        """SQLite runs multi-statement scripts directly; Postgres needs them split
        and each failure isolated so an existing index doesn't abort the batch."""
        if not USE_PG:
            return self._c.executescript(script)
        for stmt in [s.strip() for s in script.split(";") if s.strip()]:
            try:
                cur = self._c.cursor()
                cur.execute(_to_pg(stmt))
                self._c.commit()
            except Exception:
                self._c.rollback()
        return None

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        self._c.close()

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, _value):
        # Rows are already dict-like under both backends.
        pass


DB_FILE = os.environ.get("DB_PATH", "vi_triage.db")


def connect():
    return Connection()


def add_column(db, table, column, coltype):
    """Idempotent ALTER TABLE that works on both backends. Postgres aborts the
    whole transaction on a duplicate-column error, so each attempt is isolated."""
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        db.commit()
    except Exception:
        db.rollback()


def backend():
    return "postgres" if USE_PG else "sqlite"


def location():
    if USE_PG:
        # never expose credentials
        return re.sub(r"://[^@]*@", "://***@", DATABASE_URL)
    return DB_FILE


def is_ephemeral():
    """True when the data lives inside the deploy image and will be lost on the
    next push. Postgres is a separate service, so it is never ephemeral."""
    if USE_PG:
        return False
    return not DB_FILE.startswith("/data")
