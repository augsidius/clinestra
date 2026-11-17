"""
Storage: a tiny document store with three backends.

Every record is stored as JSON under a *kind* (``patient``, ``order`` ...),
with its ``site`` and ``patient_id`` lifted out for filtering. Site scoping is
enforced here: every read takes a site, and only library content (cohorts,
protocols and instruments loaded from packs) lives under the shared site
``"*"``.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional, Protocol
from urllib.parse import urlsplit

__all__ = ["Store", "MemoryStore", "SQLiteStore", "PostgresStore", "store_from_url", "redact_url", "LIBRARY_SITE"]

LIBRARY_SITE = "*"


class Store(Protocol):
    def put(self, kind: str, doc: dict) -> None: ...
    def get(self, kind: str, id: str, site: str) -> Optional[dict]: ...
    def list(self, kind: str, site: str, patient_id: Optional[str] = None, /, **where) -> List[dict]: ...
    # Optional: ``delete(kind, id, site)`` hard-deletes a private (non-clinical) document, e.g. an expired portal
    # session. Clinical records are never hard-deleted; they are soft-deleted through the harness.


def _match(doc: dict, where: dict) -> bool:
    return all(doc.get(k) == v for k, v in where.items() if v is not None)


class MemoryStore:
    """In-process store. Good for tests, notebooks and demos."""

    def __init__(self):
        self._d: Dict[str, Dict[str, dict]] = {}
        self._lock = threading.RLock()

    def put(self, kind, doc):
        with self._lock:
            self._d.setdefault(kind, {})[doc["id"]] = json.loads(json.dumps(doc, default=str))

    def get(self, kind, id, site):
        doc = self._d.get(kind, {}).get(id)
        if doc and doc.get("site") in (site, LIBRARY_SITE):
            return json.loads(json.dumps(doc))
        return None

    def site_of(self, kind, id):
        doc = self._d.get(kind, {}).get(id)
        return doc.get("site") if doc else None

    def list(self, kind, site, patient_id=None, /, **where):
        out = []
        for doc in self._d.get(kind, {}).values():
            if doc.get("site") not in (site, LIBRARY_SITE):
                continue
            if patient_id is not None and doc.get("patient_id") != patient_id:
                continue
            if _match(doc, where):
                out.append(json.loads(json.dumps(doc)))
        return sorted(out, key=lambda d: str(d.get("created_at", "")))

    def delete(self, kind, id, site):
        with self._lock:
            doc = self._d.get(kind, {}).get(id)
            if doc and doc.get("site") == site:
                del self._d[kind][id]


class SQLiteStore:
    """Single-file persistent store using only the standard library."""

    def __init__(self, path: str = "clinestra.db"):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self.migrate()

    def ping(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1").fetchone()[0] == 1

    def migrate(self) -> None:
        """Create the schema if it is missing. Safe to run repeatedly."""
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS docs (kind TEXT NOT NULL, id TEXT NOT NULL, site TEXT NOT NULL,"
                " patient_id TEXT, created_at TEXT, data TEXT NOT NULL, PRIMARY KEY (kind, id))")
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_docs_scope ON docs(kind, site, patient_id)")
            self._conn.commit()

    def put(self, kind, doc):
        data = json.dumps(doc, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO docs(kind,id,site,patient_id,created_at,data) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(kind,id) DO UPDATE SET site=excluded.site, patient_id=excluded.patient_id, data=excluded.data",
                (kind, doc["id"], doc["site"], doc.get("patient_id"), str(doc.get("created_at", "")), data))
            self._conn.commit()

    def get(self, kind, id, site):
        with self._lock:
            row = self._conn.execute("SELECT data FROM docs WHERE kind=? AND id=? AND site IN (?,?)",
                                     (kind, id, site, LIBRARY_SITE)).fetchone()
        return json.loads(row[0]) if row else None

    def site_of(self, kind, id):
        with self._lock:
            row = self._conn.execute("SELECT site FROM docs WHERE kind=? AND id=?", (kind, id)).fetchone()
        return row[0] if row else None

    def list(self, kind, site, patient_id=None, /, **where):
        sql, args = "SELECT data FROM docs WHERE kind=? AND site IN (?,?)", [kind, site, LIBRARY_SITE]
        if patient_id is not None:
            sql += " AND patient_id=?"
            args.append(patient_id)
        sql += " ORDER BY created_at"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        docs: Iterable[dict] = (json.loads(r[0]) for r in rows)
        return [d for d in docs if _match(d, where)]

    def delete(self, kind, id, site):
        with self._lock:
            self._conn.execute("DELETE FROM docs WHERE kind=? AND id=? AND site=?", (kind, id, site))
            self._conn.commit()


def redact_url(url: str) -> str:
    """The URL with any password removed, safe for logs and error messages."""
    return re.sub(r"(://[^:/@]*):[^@/]*@", r"\1:***@", url)


_NUL = re.compile(r"(?<!\\)(?:\\\\)*\\u0000")
_SCHEMA_LOCK = 72815340  # advisory lock id, so concurrent starts do not race to create the schema


class PostgresStore:
    """PostgreSQL store for multi-process deployments. Needs ``pip install "clinestra[postgres]"``.

    Holds a small thread-safe pool of connections. Every write commits on its own, as in :class:`SQLiteStore`.
    """

    def __init__(self, url: str, pool_size: int = 10):
        try:
            import psycopg
        except ImportError:
            raise ImportError('PostgreSQL needs psycopg. Install it with: pip install "clinestra[postgres]"') from None
        self._psycopg = psycopg
        self._url = url
        self._password = urlsplit(url).password
        self._idle: List = []
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(pool_size)
        self.migrate()

    def __repr__(self):
        return f"PostgresStore({redact_url(self._url)!r})"

    def _clean(self, exc: Exception) -> str:
        text = str(exc)
        if self._password:
            text = text.replace(self._password, "***")
        return redact_url(text.replace(self._url, redact_url(self._url)))

    def _connect(self):
        try:
            return self._psycopg.connect(self._url, autocommit=True, connect_timeout=10)
        except self._psycopg.Error as exc:
            message = f"Could not connect to PostgreSQL at {redact_url(self._url)}: {self._clean(exc)}"
        raise RuntimeError(message)

    @contextmanager
    def _conn(self):
        with self._slots:
            with self._lock:
                conn = self._idle.pop() if self._idle else None
            if conn is None or conn.closed:
                conn = self._connect()
            try:
                yield conn
            finally:
                if conn.closed or conn.broken:
                    pass
                else:
                    with self._lock:
                        self._idle.append(conn)

    def _run(self, sql: str, args=(), fetch: Optional[str] = None):
        """Run one statement. A connection lost while idle (a server restart, say) is replaced once."""
        for attempt in (1, 2):
            try:
                with self._conn() as conn:
                    cur = conn.execute(sql, args)
                    if fetch == "one":
                        return cur.fetchone()
                    return cur.fetchall() if fetch == "all" else None
            except self._psycopg.OperationalError as exc:
                if attempt == 2:
                    raise RuntimeError(f"PostgreSQL error: {self._clean(exc)}") from None
                self._discard_idle()

    def _discard_idle(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    def ping(self) -> bool:
        return self._run("SELECT 1", fetch="one")[0] == 1

    def migrate(self) -> None:
        """Create the schema if it is missing. Safe to run repeatedly, including from several processes at once."""
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK,))
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS docs (kind TEXT NOT NULL, id TEXT NOT NULL, site TEXT NOT NULL,"
                    " patient_id TEXT, created_at TEXT, data JSONB NOT NULL, seq BIGINT GENERATED ALWAYS AS IDENTITY,"
                    " PRIMARY KEY (kind, id))")
                conn.execute("CREATE INDEX IF NOT EXISTS ix_docs_scope ON docs(kind, site, patient_id)")

    def put(self, kind, doc):
        data = json.dumps(doc, default=str)
        if _NUL.search(data):
            raise ValueError("PostgreSQL cannot store NUL characters in text; remove them from this record")
        self._run("INSERT INTO docs(kind,id,site,patient_id,created_at,data) VALUES(%s,%s,%s,%s,%s,%s::jsonb) "
                  "ON CONFLICT(kind,id) DO UPDATE SET site=excluded.site, patient_id=excluded.patient_id, data=excluded.data",
                  (kind, doc["id"], doc["site"], doc.get("patient_id"), str(doc.get("created_at", "")), data))

    def get(self, kind, id, site):
        row = self._run("SELECT data FROM docs WHERE kind=%s AND id=%s AND site IN (%s,%s)",
                        (kind, id, site, LIBRARY_SITE), "one")
        return row[0] if row else None

    def site_of(self, kind, id):
        row = self._run("SELECT site FROM docs WHERE kind=%s AND id=%s", (kind, id), "one")
        return row[0] if row else None

    def list(self, kind, site, patient_id=None, /, **where):
        sql, args = "SELECT data FROM docs WHERE kind=%s AND site IN (%s,%s)", [kind, site, LIBRARY_SITE]
        if patient_id is not None:
            sql += " AND patient_id=%s"
            args.append(patient_id)
        sql += ' ORDER BY created_at COLLATE "C", seq'  # byte order, as in SQLite; seq keeps insertion order for ties
        return [r[0] for r in self._run(sql, args, "all") if _match(r[0], where)]

    def delete(self, kind, id, site):
        self._run("DELETE FROM docs WHERE kind=%s AND id=%s AND site=%s", (kind, id, site))

    def close(self) -> None:
        self._discard_idle()


def store_from_url(url: Optional[str]) -> "Store":
    """``postgresql://`` or ``postgres://`` gives PostgreSQL, ``sqlite:///path`` or a plain path gives SQLite, ``None`` gives memory."""
    if not url:
        return MemoryStore()
    if url.startswith(("postgresql://", "postgres://")):
        return PostgresStore(url)
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        return SQLiteStore(path or ":memory:")
    if "://" in url:
        raise ValueError(f"Unsupported database URL {redact_url(url)}. Use postgresql://... or sqlite:///path")
    return SQLiteStore(url)
