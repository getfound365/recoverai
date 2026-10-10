"""Store: one connection, one transaction API, two backends.

Default is SQLite (stdlib, zero setup) at data/recoverai.db. If DATABASE_URL is set
AND a Postgres driver is importable, Postgres is used instead - the SQL shape is
deliberately identical, which is why every timestamp is TEXT, every boolean is
INTEGER and every list column is a JSON array in TEXT.

The one place the backend matters is the send claim: SQLite uses BEGIN IMMEDIATE,
Postgres uses BEGIN + SELECT ... FOR UPDATE on the client_autonomy_settings row.
Either way the claim and a mode flip contend for the same row.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import config


def _postgres_driver():
    for name in ("psycopg", "psycopg2"):
        try:
            return __import__(name)
        except Exception:  # pragma: no cover - depends on the host
            continue
    return None


def resolve_backend(explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    forced = os.environ.get("RECOVERAI_BACKEND")
    if forced:
        return forced
    if os.environ.get("DATABASE_URL"):
        if _postgres_driver() is not None:
            return "postgres"
        print(
            "[recoverai] DATABASE_URL is set but no Postgres driver is importable; "
            "using SQLite at data/recoverai.db (the same SQL shape runs on Postgres).",
            file=sys.stderr,
        )
    return "sqlite"


def default_db_path() -> Path:
    env = os.environ.get("RECOVERAI_DB")
    return Path(env).resolve() if env else (config.data_dir() / "recoverai.db")


def _split_sqlite_only(src: str) -> Tuple[str, str]:
    """Return (sqlite_sql, postgres_sql) after stripping the other dialect's blocks."""
    keep_in, keep_pg = [], []
    mode = None
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("-- :sqlite-only"):
            mode = "sqlite"
            continue
        if stripped.startswith("-- :postgres-only"):
            mode = "pg"
            continue
        if stripped.startswith("-- :end"):
            mode = None
            continue
        if mode in (None, "sqlite"):
            keep_in.append(line)
        if mode in (None, "pg"):
            keep_pg.append(line)
    return "\n".join(keep_in), "\n".join(keep_pg)


def _to_postgres(sql: str) -> str:
    out = re.sub(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", "BIGSERIAL PRIMARY KEY", sql, flags=re.I)
    return out.replace("?", "%s")


def _split_statements(sql: str) -> List[str]:
    """Split a script on ';' while respecting Postgres dollar-quoted bodies."""
    stmts: List[str] = []
    buf: List[str] = []
    dollar: Optional[str] = None
    i = 0
    while i < len(sql):
        ch = sql[i]
        if dollar:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = re.match(r"\$[A-Za-z_]*\$", sql[i:])
            if m:
                dollar = m.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


class Store:
    """Thin store over SQLite/Postgres. Create one per thread that needs to write."""

    def __init__(self, db_path: Optional[os.PathLike | str] = None, backend: Optional[str] = None,
                 url: Optional[str] = None):
        self.backend = resolve_backend(backend)
        self._tx_depth = 0
        if self.backend == "sqlite":
            self.path: Path = Path(db_path) if db_path else default_db_path()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None,
                                        check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=30000")
            self.conn.execute("PRAGMA foreign_keys=ON")
        else:  # pragma: no cover - exercised only where a Postgres driver exists
            driver = _postgres_driver()
            if driver is None:
                raise RuntimeError("postgres backend requested but no driver is installed")
            dsn = url or os.environ["DATABASE_URL"]
            self.path = dsn  # type: ignore[assignment]
            self.conn = driver.connect(dsn)
            try:
                self.conn.autocommit = True  # psycopg3
            except Exception:
                pass

    # --- low level ---------------------------------------------------------
    @property
    def placeholder(self) -> str:
        return "?" if self.backend == "sqlite" else "%s"

    def _adapt(self, sql: str) -> str:
        return sql if self.backend == "sqlite" else _to_postgres(sql)

    def execute(self, sql: str, params: Sequence[Any] = ()):
        return self.conn.execute(self._adapt(sql), tuple(params))

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            if isinstance(row, sqlite3.Row):
                out.append(dict(row))
            elif isinstance(row, dict):
                out.append(dict(row))
            else:  # pragma: no cover - driver dependent
                cols = [d[0] for d in cur.description]
                out.append(dict(zip(cols, row)))
        return out

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def insert(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self.backend == "sqlite":
            cur = self.execute(sql, params)
            return cur.lastrowid
        # pragma: no cover - driver dependent
        cur = self.execute(sql.rstrip().rstrip(";") + " RETURNING 1", params)
        row = cur.fetchone()
        return row[0] if row else None

    # --- schema ------------------------------------------------------------
    def schema_sql(self) -> str:
        src = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
        sqlite_sql, pg_sql = _split_sqlite_only(src)
        return sqlite_sql if self.backend == "sqlite" else _to_postgres(pg_sql)

    def init_schema(self) -> None:
        sql = self.schema_sql()
        if self.backend == "sqlite":
            self.conn.executescript(sql)
            self._ensure_columns()
        else:  # pragma: no cover - driver dependent
            for stmt in _split_statements(sql):
                self.conn.execute(stmt)

    #: Columns added after a database file already existed. `CREATE TABLE IF NOT EXISTS`
    #: does nothing to a table that is already there, so an older data/recoverai.db would
    #: otherwise be missing every column added since it was created. Each entry is a
    #: plain, additive, defaulted column - nothing is dropped and no row is rewritten.
    #: Postgres gets the same statements from the :postgres-only block in schema.sql.
    SQLITE_ADDED_COLUMNS: Tuple[Tuple[str, str, str], ...] = (
        ("client_autonomy_settings", "lifecycle_status",
         "TEXT NOT NULL DEFAULT 'STATUS_BETA_TESTER'"),
        ("client_autonomy_settings", "manual_review_threshold_passed",
         "INTEGER NOT NULL DEFAULT 0"),
        ("client_autonomy_settings", "live_billing_verified", "INTEGER NOT NULL DEFAULT 0"),
        ("client_autonomy_settings", "commercial_msa_executed", "INTEGER NOT NULL DEFAULT 0"),
        ("client_autonomy_settings", "billing_route_demonstrated_in_sandbox",
         "INTEGER NOT NULL DEFAULT 0"),
        ("client_autonomy_settings", "live_billing_receipt_metadata", "TEXT"),
        ("outbound_contact_logs", "approved_by", "TEXT"),
        ("outbound_contact_logs", "approved_at", "TEXT"),
    )

    def _ensure_columns(self) -> None:
        for table, column, ddl in self.SQLITE_ADDED_COLUMNS:
            if not self.query_one("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                                  (table,)):
                continue
            existing = {row["name"] for row in self.query(f"PRAGMA table_info({table})")}
            if column in existing:
                continue
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # --- transactions ------------------------------------------------------
    @contextlib.contextmanager
    def tx(self, immediate: bool = False) -> Iterator["Store"]:
        """A real transaction. immediate=True takes the write lock up front (SQLite).

        Nested use is refused on purpose: the claim must own its own transaction.
        """
        if self._tx_depth:
            raise RuntimeError("nested transaction refused: the claim owns its transaction")
        self._tx_depth += 1
        try:
            if self.backend == "sqlite":
                self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            else:  # pragma: no cover - driver dependent
                self.conn.execute("BEGIN")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")
        finally:
            self._tx_depth -= 1

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def ensure_dirs() -> Dict[str, Path]:
    """Create (idempotently) the working directories the engine writes to."""
    paths = {
        "root": config.repo_root(),
        "data": config.data_dir(),
        "out": config.out_dir(),
        "drafts": config.out_dir() / "drafts",
        "payloads": config.out_dir() / "vapi_payloads",
        "email": config.out_dir() / "email_outbox",
    }
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    return paths


def fresh_store(db_path: Optional[os.PathLike | str] = None, backend: Optional[str] = None) -> Store:
    """A store with the schema applied. Deletes an existing SQLite file first."""
    if backend in (None, "sqlite") and (backend == "sqlite" or resolve_backend() == "sqlite"):
        path = Path(db_path) if db_path else default_db_path()
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(path) + suffix)
            if f.exists():
                f.unlink()
    store = Store(db_path=db_path, backend=backend)
    store.init_schema()
    return store
