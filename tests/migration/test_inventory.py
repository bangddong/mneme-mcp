from __future__ import annotations

import os
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """A mixed legacy database that mirrors Mneme's known application tables."""
    db_path = tmp_path / "legacy state #1.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE wiki_fts USING fts5(path, content, tokenize='unicode61');
            CREATE TABLE wiki_index (
                path TEXT PRIMARY KEY,
                summary TEXT,
                tags TEXT,
                content_hash TEXT,
                updated_at TEXT,
                updated_by TEXT
            );
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                source_agent TEXT,
                trust_score REAL DEFAULT 0.5,
                wiki_path TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                agent TEXT,
                tool TEXT,
                query TEXT,
                result_summary TEXT,
                success INTEGER,
                score REAL,
                what_worked TEXT,
                what_failed TEXT,
                next_hint TEXT,
                skills_used TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE skills (
                name TEXT PRIMARY KEY,
                description TEXT,
                wiki_path TEXT,
                base REAL DEFAULT 0.5,
                delta REAL DEFAULT 0.0,
                propensity REAL DEFAULT 0.5,
                state TEXT DEFAULT 'seeding',
                success_count INTEGER DEFAULT 0,
                use_count INTEGER DEFAULT 0,
                seed_protected INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE working (
                session_id TEXT,
                key TEXT,
                value TEXT,
                expires_at TEXT,
                PRIMARY KEY (session_id, key)
            );
            CREATE TABLE loop_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT DEFAULT (datetime('now')),
                episodes_processed INTEGER,
                ci REAL,
                bc REAL,
                transitions TEXT,
                propensities TEXT
            );
            CREATE TABLE self_model (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessed_at TEXT DEFAULT (datetime('now')),
                episodes_seen INTEGER,
                success_rate REAL,
                growth_rate REAL,
                calibration_error REAL,
                difficulty REAL,
                regulation TEXT,
                curriculum TEXT,
                notes TEXT
            );
            CREATE TABLE growth_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                last_seen_at TEXT DEFAULT (datetime('now')),
                seen_count INTEGER DEFAULT 1,
                kind TEXT,
                severity TEXT,
                subject TEXT,
                detail TEXT,
                dedup_key TEXT,
                status TEXT DEFAULT 'open',
                resolved_at TEXT,
                resolution_note TEXT
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE future_durable_data (entry_id TEXT PRIMARY KEY, payload BLOB);
            INSERT INTO wiki_fts(path, content) VALUES ('wiki/example.md', 'indexed text');
            INSERT INTO facts(content) VALUES ('A potentially durable fact');
            INSERT INTO episodes(session_id, success) VALUES ('session-1', 1);
            INSERT INTO skills(name, description) VALUES ('careful-migration', 'preserve data');
            INSERT INTO working(session_id, key, value) VALUES ('session-1', 'focus', 'inventory');
            INSERT INTO loop_cycles(episodes_processed) VALUES (1);
            INSERT INTO self_model(episodes_seen) VALUES (1);
            INSERT INTO growth_actions(kind, severity) VALUES ('calibration', 'warn');
            INSERT INTO meta(key, value) VALUES ('schema_version', '2');
            INSERT INTO future_durable_data(entry_id, payload) VALUES ('unknown-1', X'CAFE');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_inventory_is_read_only_and_classifies_mixed_state(legacy_db: Path):
    """Changing a known table's classification or mutating the DB must fail here."""
    from mneme.migration.legacy import inspect_legacy_db

    before_bytes = legacy_db.read_bytes()
    before_mtime = legacy_db.stat().st_mtime_ns

    report = inspect_legacy_db(legacy_db)

    assert legacy_db.read_bytes() == before_bytes
    assert legacy_db.stat().st_mtime_ns == before_mtime
    assert report.db_path == legacy_db.resolve()
    assert report.sha256 == sha256(before_bytes).hexdigest()

    assert report.tables["wiki_fts"].classification == "generated"
    assert report.tables["wiki_index"].classification == "generated"
    assert report.tables["facts"].classification == "candidate-durable-local"
    assert report.tables["episodes"].classification == "candidate-durable-local"
    assert report.tables["skills"].classification == "growth-local"
    assert report.tables["working"].classification == "local-transient"
    assert report.tables["meta"].classification == "local-transient"
    assert report.tables["loop_cycles"].classification == "growth-local"
    assert report.tables["self_model"].classification == "growth-local"
    assert report.tables["growth_actions"].classification == "growth-local"

    fts_internal_tables = {
        "wiki_fts_data",
        "wiki_fts_idx",
        "wiki_fts_content",
        "wiki_fts_docsize",
        "wiki_fts_config",
    }
    assert fts_internal_tables <= report.tables.keys()
    assert {
        report.tables[name].classification for name in fts_internal_tables
    } == {"generated"}

    unknown = report.tables["future_durable_data"]
    assert unknown.classification == "needs-review"
    assert unknown.classification not in {"generated", "local-transient"}
    assert unknown.row_count == 1
    assert unknown.schema == "CREATE TABLE future_durable_data (entry_id TEXT PRIMARY KEY, payload BLOB)"
    assert [column.name for column in unknown.columns] == ["entry_id", "payload"]
    assert "not a recognized Mneme legacy table" in unknown.reason

    facts = report.tables["facts"]
    assert facts.row_count == 1
    assert facts.schema.startswith("CREATE TABLE facts")
    assert [column.name for column in facts.columns] == [
        "id",
        "content",
        "source_agent",
        "trust_score",
        "wiki_path",
        "created_at",
    ]
    assert "potentially durable" in facts.reason


def test_inventory_fails_closed_for_missing_and_non_sqlite_files(tmp_path: Path):
    """Missing or non-SQLite inputs must never be initialized or treated as empty DBs."""
    from mneme.migration.legacy import (
        LegacyDatabaseNotFoundError,
        LegacyDatabaseNotSQLiteError,
        inspect_legacy_db,
    )

    missing = tmp_path / "missing state.db"
    with pytest.raises(LegacyDatabaseNotFoundError):
        inspect_legacy_db(missing)

    not_sqlite = tmp_path / "state.db"
    not_sqlite.write_text("this is not a SQLite database", encoding="utf-8")
    before_bytes = not_sqlite.read_bytes()
    before_mtime = not_sqlite.stat().st_mtime_ns

    with pytest.raises(LegacyDatabaseNotSQLiteError):
        inspect_legacy_db(not_sqlite)

    assert not_sqlite.read_bytes() == before_bytes
    assert not_sqlite.stat().st_mtime_ns == before_mtime


def test_inventory_fails_closed_when_file_changes_during_inspection(
    legacy_db: Path, monkeypatch: pytest.MonkeyPatch
):
    """Disabling the post-inspection identity check would miss a concurrent writer."""
    import mneme.migration.legacy as legacy

    original_connect = legacy.sqlite3.connect

    def connect_then_change_file(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        stat = legacy_db.stat()
        os.utime(legacy_db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000_000))
        return connection

    monkeypatch.setattr(legacy.sqlite3, "connect", connect_then_change_file)

    with pytest.raises(legacy.LegacyDatabaseChangedError):
        legacy.inspect_legacy_db(legacy_db)


def test_inventory_requires_recognized_runtime_table_semantics(tmp_path: Path):
    """A similarly named table without Mneme's key semantics must be reviewed."""
    from mneme.migration.legacy import inspect_legacy_db

    db_path = tmp_path / "lookalike.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE working (
                session_id TEXT,
                key TEXT,
                value TEXT,
                expires_at TEXT
            );
            CREATE TABLE meta (key TEXT, value TEXT);
            """
        )
        conn.commit()
    finally:
        conn.close()

    report = inspect_legacy_db(db_path)

    assert report.tables["working"].classification == "needs-review"
    assert report.tables["meta"].classification == "needs-review"
