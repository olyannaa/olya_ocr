"""Состояние обработки в SQLite: пакеты, файлы, ошибки/предупреждения, исправления.
Все записи сразу на диск -> после сбоя/перезапуска работа продолжается с места остановки."""
import json
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_PATH, MAX_ATTEMPTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches(
  id INTEGER PRIMARY KEY, name TEXT, source TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER,
  rel_path TEXT,          -- путь как в исходной папке (сохраняет структуру)
  stored_path TEXT,       -- где файл лежит внутри контейнера
  size INTEGER,
  sha256 TEXT,
  ext TEXT,
  status TEXT,            -- uploading | queued | processing | done | error | skipped
  attempts INTEGER DEFAULT 0,
  pages_total INTEGER, pages_done INTEGER DEFAULT 0, ocr_pages INTEGER DEFAULT 0,
  mean_conf REAL,
  chars INTEGER,
  error TEXT,
  worker INTEGER, heartbeat REAL,
  started REAL, finished REAL, created REAL
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE INDEX IF NOT EXISTS files_path ON files(rel_path, size);
CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY,
  file_id INTEGER,
  page INTEGER,
  severity TEXT,          -- error | warning | word
  kind TEXT,              -- corrupt | encrypted | unsupported | empty | page_error | timeout | crash | low_quality | word ...
  message TEXT,
  word TEXT, conf REAL, line INTEGER, word_idx INTEGER, context TEXT, crop TEXT,
  correction TEXT,
  created REAL
);
CREATE INDEX IF NOT EXISTS issues_file ON issues(file_id);
CREATE INDEX IF NOT EXISTS issues_sev ON issues(severity);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=60, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=60000")
    return con


@contextmanager
def tx(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def init():
    con = connect()
    con.executescript(SCHEMA)
    return con


def kv_get(con, key, default=None):
    r = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def kv_set(con, key, value):
    con.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))


def add_issue(con, file_id, severity, kind, message, page=None, **kw):
    con.execute(
        "INSERT INTO issues(file_id,page,severity,kind,message,word,conf,line,word_idx,context,crop,created)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (file_id, page, severity, kind, message, kw.get("word"), kw.get("conf"), kw.get("line"),
         kw.get("word_idx"), kw.get("context"), kw.get("crop"), time.time()))


def claim_next(con, worker_pid):
    """Атомарно взять следующий файл из очереди."""
    with tx(con):
        r = con.execute("SELECT id FROM files WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        con.execute("UPDATE files SET status='processing', worker=?, heartbeat=?, started=?, attempts=attempts+1"
                    " WHERE id=?", (worker_pid, time.time(), time.time(), r["id"]))
    return con.execute("SELECT * FROM files WHERE id=?", (r["id"],)).fetchone()


def requeue_interrupted(con, file_id=None, reason="процесс обработки прервался"):
    """Файлы в статусе processing (после падения/перезапуска) -> снова в очередь, если не исчерпаны попытки."""
    q = "SELECT id, attempts FROM files WHERE status='processing'" + (" AND id=?" if file_id else "")
    rows = con.execute(q, (file_id,) if file_id else ()).fetchall()
    for r in rows:
        if r["attempts"] >= MAX_ATTEMPTS:
            con.execute("UPDATE files SET status='error', error=?, finished=? WHERE id=?",
                        (f"{reason} {r['attempts']} раза подряд — файл пропущен", time.time(), r["id"]))
            add_issue(con, r["id"], "error", "crash",
                      f"{reason} {r['attempts']} раза подряд (возможно, файл повреждён или слишком велик) — пропущен")
        else:
            con.execute("UPDATE files SET status='queued', worker=NULL WHERE id=?", (r["id"],))
    return len(rows)
