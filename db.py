"""SQLite persistence: users, encrypted API keys, durable job queue, clips."""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from .config import get_settings

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys(
  user_id INTEGER NOT NULL,
  provider TEXT NOT NULL,
  secret_enc TEXT NOT NULL,
  hint TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(user_id, provider)
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  progress INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  source_path TEXT,
  source_name TEXT,
  options_json TEXT NOT NULL,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS clips(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  category TEXT NOT NULL,
  title TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0,
  start_s REAL NOT NULL,
  end_s REAL NOT NULL,
  filename TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS clips_job ON clips(job_id);
"""

ACTIVE = ("queued", "downloading", "transcribing", "analyzing", "selecting", "rendering")


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    path = str(get_settings().db_path)
    if c is None or getattr(_local, "path", None) != path:
        c = sqlite3.connect(path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.c, _local.path = c, path
    return c


def init_db() -> None:
    conn().executescript(SCHEMA)


@contextmanager
def write():
    with _write_lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


# ---------- users ----------
def create_user(email: str, password_hash: str):
    try:
        with write() as c:
            cur = c.execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)",
                            (email.lower().strip(), password_hash, time.time()))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_user_by_email(email: str):
    return conn().execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),)).fetchone()


def get_user(uid: int):
    return conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def save_user_settings(uid: int, settings: dict) -> None:
    with write() as c:
        c.execute("UPDATE users SET settings_json=? WHERE id=?", (json.dumps(settings), uid))


def user_settings(row) -> dict:
    try:
        return json.loads(row["settings_json"] or "{}")
    except Exception:
        return {}


# ---------- API keys ----------
def set_api_key(uid: int, provider: str, secret_enc: str, hint: str) -> None:
    with write() as c:
        c.execute("INSERT INTO api_keys(user_id,provider,secret_enc,hint,updated_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(user_id,provider) DO UPDATE SET secret_enc=excluded.secret_enc,hint=excluded.hint,updated_at=excluded.updated_at",
                  (uid, provider, secret_enc, hint, time.time()))


def get_api_key(uid: int, provider: str):
    return conn().execute("SELECT * FROM api_keys WHERE user_id=? AND provider=?", (uid, provider)).fetchone()


def list_api_keys(uid: int):
    return conn().execute("SELECT provider,hint,updated_at FROM api_keys WHERE user_id=?", (uid,)).fetchall()


def delete_api_key(uid: int, provider: str) -> None:
    with write() as c:
        c.execute("DELETE FROM api_keys WHERE user_id=? AND provider=?", (uid, provider))


# ---------- jobs / queue ----------
def create_job(uid: int, source_path: str, source_name: str, options: dict, status="queued", message="Queued…") -> str:
    jid = uuid.uuid4().hex
    now = time.time()
    with write() as c:
        c.execute("INSERT INTO jobs(id,user_id,status,message,source_path,source_name,options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (jid, uid, status, message, source_path, source_name, json.dumps(options), now, now))
    return jid


def count_active_jobs(uid: int) -> int:
    q = ",".join("?" * len(ACTIVE))
    return conn().execute(f"SELECT COUNT(*) FROM jobs WHERE user_id=? AND status IN ({q})", (uid, *ACTIVE)).fetchone()[0]


def count_jobs_since(uid: int, since: float) -> int:
    return conn().execute("SELECT COUNT(*) FROM jobs WHERE user_id=? AND created_at>=?", (uid, since)).fetchone()[0]


def claim_next_job():
    """Atomically move the oldest queued job to 'downloading' and return it."""
    with write() as c:
        row = c.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("UPDATE jobs SET status='transcribing',message='Starting…',attempts=attempts+1,updated_at=? WHERE id=?", (time.time(), row["id"]))
        return c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def update_job(jid: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ",".join(f"{k}=?" for k in fields)
    with write() as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))


def get_job(jid: str, uid: int = None):
    if uid is None:
        return conn().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return conn().execute("SELECT * FROM jobs WHERE id=? AND user_id=?", (jid, uid)).fetchone()


def list_jobs(uid: int, limit=30):
    return conn().execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (uid, limit)).fetchall()


def request_cancel(jid: str, uid: int) -> bool:
    with write() as c:
        cur = c.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=? AND user_id=? AND status IN (%s)" % ",".join("?" * len(ACTIVE)),
                        (time.time(), jid, uid, *ACTIVE))
        if cur.rowcount:
            c.execute("UPDATE jobs SET status='cancelled',message='Cancelled.' WHERE id=? AND status='queued'", (jid,))
        return cur.rowcount > 0


def is_cancelled(jid: str) -> bool:
    r = conn().execute("SELECT cancel_requested FROM jobs WHERE id=?", (jid,)).fetchone()
    return bool(r and r["cancel_requested"])


def requeue_interrupted() -> int:
    """After a crash/restart, jobs stuck mid-pipeline go back to the queue (max 2 attempts)."""
    q = ",".join("?" * (len(ACTIVE) - 1))
    with write() as c:
        c.execute(f"UPDATE jobs SET status='failed',error='Interrupted by server restart too many times.',message='Failed.' WHERE status IN ({q}) AND attempts>=2",
                  ACTIVE[1:])
        cur = c.execute(f"UPDATE jobs SET status='queued',message='Re-queued after restart…' WHERE status IN ({q})", ACTIVE[1:])
        return cur.rowcount


def add_clip(jid, idx, category, title, reason, score, start_s, end_s, filename, meta) -> None:
    with write() as c:
        c.execute("INSERT INTO clips(job_id,idx,category,title,reason,score,start_s,end_s,filename,meta_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (jid, idx, category, title, reason, score, start_s, end_s, filename, json.dumps(meta)))


def job_clips(jid: str):
    return conn().execute("SELECT * FROM clips WHERE job_id=? ORDER BY idx", (jid,)).fetchall()


def clip_owner(filename: str):
    return conn().execute("SELECT j.user_id FROM clips c JOIN jobs j ON j.id=c.job_id WHERE c.filename=?", (filename,)).fetchone()


def expired_jobs(cutoff: float):
    return conn().execute("SELECT * FROM jobs WHERE created_at<? AND status NOT IN (%s)" % ",".join("?" * len(ACTIVE)), (cutoff, *ACTIVE)).fetchall()


def delete_job(jid: str) -> None:
    with write() as c:
        c.execute("DELETE FROM clips WHERE job_id=?", (jid,))
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
