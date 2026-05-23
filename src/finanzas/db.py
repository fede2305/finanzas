"""PostgreSQL (Supabase): schema, migraciones, seed.

Usa psycopg2 con un wrapper thin sobre la conexión para mantener
la API compatible con el código existente (conn.execute(), row["col"], etc.)
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import yaml

SEEDS_DIR = Path(__file__).resolve().parent.parent.parent / "seeds"

# Schema PostgreSQL completo. Colapsa las 4 migraciones SQLite en un schema inicial.
_V1_SQL = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS accounts (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        bank TEXT NOT NULL,
        card_last4 TEXT NOT NULL,
        holder_name TEXT,
        currency_default TEXT DEFAULT 'ARS',
        color TEXT DEFAULT '#64748b',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (user_id, bank, card_last4)
    );

    CREATE TABLE IF NOT EXISTS statements (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        account_id BIGINT NOT NULL REFERENCES accounts(id),
        period_start TEXT,
        period_end TEXT,
        due_date TEXT,
        source_filename TEXT,
        file_sha256 TEXT,
        parsed_at TIMESTAMPTZ DEFAULT NOW(),
        raw_total_ars REAL DEFAULT 0,
        raw_total_usd REAL DEFAULT 0,
        UNIQUE (user_id, file_sha256)
    );

    CREATE TABLE IF NOT EXISTS categories (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        parent_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        name TEXT NOT NULL,
        color TEXT,
        icon TEXT,
        is_user_created INTEGER DEFAULT 0,
        sort_order INTEGER DEFAULT 0
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_uid_parent_name
        ON categories (user_id, COALESCE(parent_id, 0), name);

    CREATE TABLE IF NOT EXISTS transactions (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        statement_id BIGINT REFERENCES statements(id) ON DELETE CASCADE,
        account_id BIGINT NOT NULL REFERENCES accounts(id),
        posted_at TEXT NOT NULL,
        description_raw TEXT NOT NULL,
        description_normalized TEXT NOT NULL,
        comprobante TEXT,
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        is_manually_categorized INTEGER DEFAULT 0,
        installment_current INTEGER,
        installment_total INTEGER,
        recurring_group_id BIGINT,
        notes TEXT,
        hash TEXT NOT NULL,
        my_share_pct REAL NOT NULL DEFAULT 1.0,
        share_with TEXT,
        UNIQUE (user_id, hash)
    );

    CREATE INDEX IF NOT EXISTS idx_tx_posted_at ON transactions(posted_at);
    CREATE INDEX IF NOT EXISTS idx_tx_category  ON transactions(category_id);
    CREATE INDEX IF NOT EXISTS idx_tx_account   ON transactions(account_id);
    CREATE INDEX IF NOT EXISTS idx_tx_user      ON transactions(user_id);

    CREATE TABLE IF NOT EXISTS manual_expenses (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        posted_at TEXT NOT NULL,
        description TEXT NOT NULL,
        amount REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'ARS',
        category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        subcategory_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        is_fixed INTEGER DEFAULT 0,
        recurrence_rule TEXT,
        notes TEXT,
        my_share_pct REAL NOT NULL DEFAULT 1.0,
        share_with TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_manual_posted ON manual_expenses(posted_at);

    CREATE TABLE IF NOT EXISTS rules (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        match_type TEXT NOT NULL,
        pattern TEXT NOT NULL,
        category_id BIGINT REFERENCES categories(id) ON DELETE CASCADE,
        subcategory_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        priority INTEGER DEFAULT 100,
        source TEXT DEFAULT 'seed',
        hit_count INTEGER DEFAULT 0,
        last_used_at TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_rules_priority ON rules(priority DESC);

    CREATE TABLE IF NOT EXISTS recurring_groups (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        normalized_merchant TEXT NOT NULL,
        suggested_category_id BIGINT REFERENCES categories(id) ON DELETE SET NULL,
        expected_amount_min REAL,
        expected_amount_max REAL,
        cadence_days INTEGER,
        occurrences INTEGER DEFAULT 0,
        last_seen_at TEXT,
        status TEXT DEFAULT 'suggested',
        UNIQUE (user_id, normalized_merchant)
    );

    CREATE TABLE IF NOT EXISTS tx_participants (
        id BIGSERIAL PRIMARY KEY,
        transaction_id BIGINT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
        person_name TEXT NOT NULL,
        amount_owed REAL NOT NULL,
        paid_back INTEGER NOT NULL DEFAULT 0,
        paid_back_at TEXT,
        sort_order INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_tx_part_tx ON tx_participants(transaction_id);
"""

_V2_SQL = """
    CREATE TABLE IF NOT EXISTS upload_jobs (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        stage TEXT DEFAULT 'uploading',
        progress_current INTEGER DEFAULT 0,
        progress_total INTEGER DEFAULT 0,
        results TEXT,
        error TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ
    );

    CREATE INDEX IF NOT EXISTS idx_upload_user_status ON upload_jobs(user_id, status);
"""

_V3_SQL = """
    ALTER TABLE upload_jobs ADD COLUMN IF NOT EXISTS stage TEXT DEFAULT 'uploading';
"""

MIGRATIONS: list[str] = [_V1_SQL, _V2_SQL, _V3_SQL]


class _PgConn:
    """Wrapper thin sobre psycopg2 para que `conn.execute()` funcione como sqlite3."""

    def __init__(self, raw_conn: psycopg2.extensions.connection) -> None:
        self._conn = raw_conn
        self._cur: psycopg2.extensions.cursor = raw_conn.cursor()
        self._lastid: int | None = None
        self._lastid_fetched: bool = False

    def execute(self, sql: str, params: tuple | list = ()) -> "_PgConn":
        self._cur = self._conn.cursor()
        self._cur.execute(sql, params or ())
        self._lastid = None
        self._lastid_fetched = False
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self) -> int | None:
        """ID del último INSERT ... RETURNING id."""
        if not self._lastid_fetched:
            row = self._cur.fetchone()
            self._lastid = row["id"] if row else None
            self._lastid_fetched = True
        return self._lastid

    def savepoint(self, name: str) -> None:
        self._conn.cursor().execute(f"SAVEPOINT {name}")

    def rollback_to(self, name: str) -> None:
        self._conn.cursor().execute(f"ROLLBACK TO SAVEPOINT {name}")

    def release(self, name: str) -> None:
        self._conn.cursor().execute(f"RELEASE SAVEPOINT {name}")


@contextmanager
def connect(db_path=None):  # db_path ignorado — siempre usa DATABASE_URL
    """Yield un _PgConn. Commit on exit, rollback on exception."""
    raw = psycopg2.connect(
        os.environ["DATABASE_URL"],
        cursor_factory=psycopg2.extras.DictCursor,
    )
    conn = _PgConn(raw)
    try:
        yield conn
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _current_version(conn: _PgConn) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return row["v"] or 0


def migrate() -> None:
    """Ejecuta migraciones pendientes contra la DB configurada en DATABASE_URL."""
    with connect() as conn:
        current = _current_version(conn)
        for idx, m in enumerate(MIGRATIONS, start=1):
            if idx <= current:
                continue
            stmts = [s.strip() for s in m.split(";") if s.strip()]
            for stmt in stmts:
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (idx,))


def seed_for_user(conn: _PgConn, user_id: str) -> None:
    """Seedea categorías y reglas para un usuario nuevo. Idempotente."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM categories WHERE user_id = %s", (user_id,)
    ).fetchone()
    if row["n"] > 0:
        return
    seed_categories(conn, user_id)
    seed_rules(conn, user_id)


def seed_categories(conn: _PgConn, user_id: str) -> None:
    yaml_path = SEEDS_DIR / "categories.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    for sort_idx, cat in enumerate(data["categories"]):
        cur = conn.execute(
            """INSERT INTO categories (user_id, name, color, icon, is_user_created, sort_order)
               VALUES (%s, %s, %s, %s, 0, %s) RETURNING id""",
            (user_id, cat["name"], cat.get("color"), cat.get("icon"), sort_idx),
        )
        parent_id = cur.lastrowid
        for sub_idx, sub in enumerate(cat.get("subcategories") or []):
            conn.execute(
                """INSERT INTO categories
                   (user_id, parent_id, name, color, icon, is_user_created, sort_order)
                   VALUES (%s, %s, %s, %s, %s, 0, %s)""",
                (user_id, parent_id, sub, cat.get("color"), cat.get("icon"), sub_idx),
            )


def seed_rules(conn: _PgConn, user_id: str) -> None:
    yaml_path = SEEDS_DIR / "rules.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    for priority, rule in enumerate(data["rules"]):
        cat_id, sub_id = _resolve_category(conn, user_id, rule["category"], rule.get("subcategory"))
        conn.execute(
            """INSERT INTO rules
               (user_id, match_type, pattern, category_id, subcategory_id, priority, source)
               VALUES (%s, %s, %s, %s, %s, %s, 'seed')""",
            (user_id, rule["match_type"], rule["pattern"].lower(), cat_id, sub_id, 1000 - priority),
        )


def _resolve_category(
    conn: _PgConn, user_id: str, name: str, subname: str | None
) -> tuple[int, int | None]:
    cat = conn.execute(
        "SELECT id FROM categories WHERE user_id = %s AND parent_id IS NULL AND name = %s",
        (user_id, name),
    ).fetchone()
    if cat is None:
        raise ValueError(f"Categoría seed no encontrada: {name!r}")
    if not subname:
        return cat["id"], None
    sub = conn.execute(
        "SELECT id FROM categories WHERE user_id = %s AND parent_id = %s AND name = %s",
        (user_id, cat["id"], subname),
    ).fetchone()
    if sub is None:
        cur = conn.execute(
            """INSERT INTO categories (user_id, parent_id, name, is_user_created)
               VALUES (%s, %s, %s, 0) RETURNING id""",
            (user_id, cat["id"], subname),
        )
        return cat["id"], cur.lastrowid
    return cat["id"], sub["id"]


def init_db() -> None:
    """Alias legacy: solo migra (el seed es per-user ahora)."""
    migrate()
