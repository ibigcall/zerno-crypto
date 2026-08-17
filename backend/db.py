"""SQLite-слой: списки наблюдения, кэш ответов API, кэш разборов LLM, TG-подписчики."""
import json
import os
import sqlite3
import threading
import time

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    scope      TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    name       TEXT,
    position   INTEGER NOT NULL DEFAULT 0,
    added_at   REAL NOT NULL,
    PRIMARY KEY (scope, symbol)
);

CREATE TABLE IF NOT EXISTS api_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

-- Разбор кэшируется на область данных: у веба и у каждого чата свой рыночный
-- контекст, а значит и свой текст. Без scope в ключе разбор из Telegram
-- перезатирал бы веб-разбор и наоборот.
CREATE TABLE IF NOT EXISTS analysis (
    scope       TEXT NOT NULL,
    symbol      TEXT NOT NULL,          -- тикер либо 'portfolio'
    kind        TEXT NOT NULL,          -- token | portfolio
    fingerprint TEXT NOT NULL,          -- отпечаток входных данных
    payload     TEXT NOT NULL,
    model       TEXT,
    source      TEXT,                   -- llm | rules
    created_at  REAL NOT NULL,
    PRIMARY KEY (scope, symbol, kind)
);

CREATE TABLE IF NOT EXISTS tg_users (
    chat_id        TEXT PRIMARY KEY,
    username       TEXT,
    created_at     REAL NOT NULL,
    digest_enabled INTEGER NOT NULL DEFAULT 0,
    digest_hour    INTEGER NOT NULL DEFAULT 10,
    last_digest    TEXT                 -- YYYY-MM-DD последней отправки
);

-- Собственная история цен: на бесплатном тарифе FreeCryptoAPI исторические
-- эндпоинты закрыты, поэтому накапливаем котировки сами и строим по ним график
-- и недельное изменение.
CREATE TABLE IF NOT EXISTS price_points (
    symbol TEXT NOT NULL,
    ts     REAL NOT NULL,
    price  REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_price_symbol ON price_points (symbol, ts);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL,
    text       TEXT NOT NULL,
    source     TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_scope ON notes (scope, created_at DESC);
"""


def conn():
    """Соединение на поток (Flask отдаёт запросы из пула потоков)."""
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=15000")
        _local.conn = c
    return c


def init():
    c = conn()
    _migrate(c)
    c.executescript(SCHEMA)
    c.commit()
    seed_scope("web", config.DEFAULT_WATCHLIST)


def _migrate(c):
    """Схема кэша разборов менялась: в ключ добавилась область данных.

    Таблица целиком производная — проще пересобрать её, чем переливать строки.
    """
    exists = c.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'analysis'"
    ).fetchone()
    if not exists:
        return
    columns = {row[1] for row in c.execute("PRAGMA table_info(analysis)")}
    if "scope" not in columns:
        c.execute("DROP TABLE analysis")
        c.commit()


# ── списки наблюдения ─────────────────────────────────────────────────────────

def seed_scope(scope, symbols):
    """Наполнить пустой список дефолтным набором. Уже непустой не трогаем."""
    c = conn()
    row = c.execute("SELECT COUNT(*) n FROM watchlist WHERE scope = ?", (scope,)).fetchone()
    if row["n"]:
        return
    now = time.time()
    c.executemany(
        "INSERT OR IGNORE INTO watchlist (scope, symbol, name, position, added_at)"
        " VALUES (?, ?, NULL, ?, ?)",
        [(scope, s.upper(), i, now) for i, s in enumerate(symbols)],
    )
    c.commit()


def watchlist(scope):
    rows = conn().execute(
        "SELECT symbol, name FROM watchlist WHERE scope = ? ORDER BY position, added_at",
        (scope,),
    ).fetchall()
    return [dict(r) for r in rows]


def watchlist_add(scope, symbol, name=None):
    c = conn()
    pos = c.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 p FROM watchlist WHERE scope = ?", (scope,)
    ).fetchone()["p"]
    c.execute(
        "INSERT OR IGNORE INTO watchlist (scope, symbol, name, position, added_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (scope, symbol.upper(), name, pos, time.time()),
    )
    c.commit()


def watchlist_remove(scope, symbol):
    c = conn()
    c.execute("DELETE FROM watchlist WHERE scope = ? AND symbol = ?", (scope, symbol.upper()))
    c.commit()


# ── кэш ответов API ──────────────────────────────────────────────────────────

def cache_get(key, ttl):
    if ttl <= 0:
        return None
    row = conn().execute(
        "SELECT payload, fetched_at FROM api_cache WHERE key = ?", (key,)
    ).fetchone()
    if not row or time.time() - row["fetched_at"] > ttl:
        return None
    try:
        return json.loads(row["payload"])
    except json.JSONDecodeError:
        return None


def cache_put(key, value):
    c = conn()
    c.execute(
        "INSERT INTO api_cache (key, payload, fetched_at) VALUES (?, ?, ?)"
        " ON CONFLICT(key) DO UPDATE SET payload = excluded.payload,"
        " fetched_at = excluded.fetched_at",
        (key, json.dumps(value, ensure_ascii=False), time.time()),
    )
    c.commit()


def cache_age(key):
    row = conn().execute("SELECT fetched_at FROM api_cache WHERE key = ?", (key,)).fetchone()
    return time.time() - row["fetched_at"] if row else None


# ── собственная история цен ──────────────────────────────────────────────────

def price_add(symbol, price, ts=None):
    """Записать котировку. Чаще одной точки в две минуты не храним — этого хватает
    и графику, и оценке волатильности, а база не растёт."""
    if price is None:
        return
    ts = ts or time.time()
    c = conn()
    row = c.execute(
        "SELECT MAX(ts) t FROM price_points WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row and row["t"] and ts - row["t"] < 120:
        return
    c.execute("INSERT OR REPLACE INTO price_points (symbol, ts, price) VALUES (?, ?, ?)",
              (symbol, ts, float(price)))
    c.execute("DELETE FROM price_points WHERE symbol = ? AND ts < ?",
              (symbol, ts - 400 * 86400))
    c.commit()


def price_daily(symbol, days):
    """Последняя цена каждого календарного дня, от старых к новым."""
    rows = conn().execute(
        "SELECT date(ts, 'unixepoch', 'localtime') AS day, price, ts"
        " FROM price_points WHERE symbol = ? AND ts > ?"
        " ORDER BY ts",
        (symbol, time.time() - (days + 1) * 86400),
    ).fetchall()
    per_day = {}
    for row in rows:
        per_day[row["day"]] = row["price"]
    return [per_day[day] for day in sorted(per_day)][-days:]


def price_recent(symbol, limit=48):
    """Последние точки внутри дня — на них рисуется график, пока нет истории дней."""
    rows = conn().execute(
        "SELECT price FROM price_points WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    return [r["price"] for r in reversed(rows)]


# ── кэш разборов LLM ─────────────────────────────────────────────────────────

def analysis_get(scope, symbol, kind, fingerprint, ttl):
    row = conn().execute(
        "SELECT payload, model, source, created_at, fingerprint FROM analysis"
        " WHERE scope = ? AND symbol = ? AND kind = ?",
        (scope, symbol, kind),
    ).fetchone()
    if not row:
        return None
    if row["fingerprint"] != fingerprint:
        return None
    if ttl > 0 and time.time() - row["created_at"] > ttl:
        return None
    try:
        data = json.loads(row["payload"])
    except json.JSONDecodeError:
        return None
    data["_model"] = row["model"]
    data["_source"] = row["source"]
    data["_created_at"] = row["created_at"]
    return data


def analysis_put(scope, symbol, kind, fingerprint, payload, model, source):
    c = conn()
    c.execute(
        "INSERT INTO analysis"
        " (scope, symbol, kind, fingerprint, payload, model, source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(scope, symbol, kind) DO UPDATE SET fingerprint = excluded.fingerprint,"
        " payload = excluded.payload, model = excluded.model, source = excluded.source,"
        " created_at = excluded.created_at",
        (scope, symbol, kind, fingerprint, json.dumps(payload, ensure_ascii=False), model,
         source, time.time()),
    )
    c.commit()


# ── TG-подписчики ────────────────────────────────────────────────────────────

def tg_upsert(chat_id, username=None):
    c = conn()
    c.execute(
        "INSERT INTO tg_users (chat_id, username, created_at) VALUES (?, ?, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET username = COALESCE(excluded.username, username)",
        (str(chat_id), username, time.time()),
    )
    c.commit()


def tg_get(chat_id):
    row = conn().execute("SELECT * FROM tg_users WHERE chat_id = ?", (str(chat_id),)).fetchone()
    return dict(row) if row else None


def tg_set_digest(chat_id, enabled, hour=None):
    c = conn()
    if hour is None:
        c.execute("UPDATE tg_users SET digest_enabled = ? WHERE chat_id = ?",
                  (1 if enabled else 0, str(chat_id)))
    else:
        c.execute("UPDATE tg_users SET digest_enabled = ?, digest_hour = ? WHERE chat_id = ?",
                  (1 if enabled else 0, int(hour), str(chat_id)))
    c.commit()


def tg_digest_subscribers():
    rows = conn().execute(
        "SELECT * FROM tg_users WHERE digest_enabled = 1"
    ).fetchall()
    return [dict(r) for r in rows]


def tg_mark_digest(chat_id, day):
    c = conn()
    c.execute("UPDATE tg_users SET last_digest = ? WHERE chat_id = ?", (day, str(chat_id)))
    c.commit()


# ── заметки (рыночный контекст для LLM) ──────────────────────────────────────

def notes_add(scope, text, source=None):
    c = conn()
    cur = c.execute(
        "INSERT INTO notes (scope, text, source, created_at) VALUES (?, ?, ?, ?)",
        (scope, text.strip(), source, time.time()),
    )
    c.commit()
    return cur.lastrowid


def notes_list(scope, limit=10, max_age=None):
    sql = "SELECT id, text, source, created_at FROM notes WHERE scope = ?"
    args = [scope]
    if max_age:
        sql += " AND created_at > ?"
        args.append(time.time() - max_age)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def notes_delete(scope, note_id):
    c = conn()
    c.execute("DELETE FROM notes WHERE scope = ? AND id = ?", (scope, note_id))
    c.commit()
