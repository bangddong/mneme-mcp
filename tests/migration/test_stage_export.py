from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_path: Path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


@pytest.fixture
def legacy_sources(tmp_path: Path) -> tuple[Path, Path]:
    """A closed mixed-state database and external Wiki with durable payloads."""
    db_path = tmp_path / "legacy" / "state.db"
    db_path.parent.mkdir()
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
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
            CREATE TABLE sqlitex_private (entry_id TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE generated_values (
                base INTEGER,
                virtual_value INTEGER GENERATED ALWAYS AS (base * 2) VIRTUAL,
                stored_value TEXT GENERATED ALWAYS AS ('stored-' || base) STORED
            );
            CREATE TABLE nullable_primary_key (
                external_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            );
            INSERT INTO wiki_fts(path, content) VALUES ('wiki/example.md', 'indexed text');
            INSERT INTO wiki_index VALUES (
                'wiki/example.md', 'summary', '["migration"]', 'abc',
                '2026-08-26T00:00:00Z', 'legacy-agent'
            );
            INSERT INTO facts(
                content, source_agent, trust_score, wiki_path, created_at
            ) VALUES (
                'A potentially durable fact', 'legacy-agent', 0.75,
                'wiki/example.md', '2026-08-26T00:00:00Z'
            );
            INSERT INTO episodes(
                session_id, agent, tool, query, result_summary, success, score,
                what_worked, what_failed, next_hint, skills_used, created_at
            ) VALUES (
                'session-1', 'legacy-agent', 'search', 'typed payload',
                'complete result', 1, 3.25, 'kept all fields', X'00FF10', NULL,
                '["migration"]', '2026-08-26T00:00:01Z'
            );
            INSERT INTO episodes(
                session_id, agent, query, result_summary, success, created_at
            ) VALUES (
                'session-2', 'legacy-agent', CAST(X'80FF' AS TEXT),
                'invalid UTF-8 remains lossless', 0, '2026-08-26T00:00:02Z'
            );
            INSERT INTO skills(
                name, description, wiki_path, base, delta, propensity, state,
                success_count, use_count, seed_protected, created_at, updated_at
            ) VALUES (
                'careful-migration', 'preserve data', 'wiki/example.md', 0.8,
                0.1, 0.9, 'active', 2, 3, 1,
                '2026-08-26T00:00:02Z', '2026-08-26T00:00:03Z'
            );
            INSERT INTO working VALUES (
                'session-1', 'focus', 'inventory', '2026-08-27T00:00:00Z'
            );
            INSERT INTO loop_cycles(
                ran_at, episodes_processed, ci, bc, transitions, propensities
            ) VALUES (
                '2026-08-26T00:00:04Z', 1, 0.2, 0.3, '{}', '{}'
            );
            INSERT INTO self_model(
                assessed_at, episodes_seen, success_rate, growth_rate,
                calibration_error, difficulty, regulation, curriculum, notes
            ) VALUES (
                '2026-08-26T00:00:05Z', 1, 0.5, 0.1, 0.0, 0.4,
                'steady', 'next', 'retain locally'
            );
            INSERT INTO growth_actions(
                created_at, last_seen_at, seen_count, kind, severity, subject,
                detail, dedup_key, status, resolved_at, resolution_note
            ) VALUES (
                '2026-08-26T00:00:06Z', '2026-08-26T00:00:07Z', 2,
                'calibration', 'warn', 'migration', 'review', 'dedup-1',
                'open', NULL, NULL
            );
            INSERT INTO meta VALUES ('schema_version', '2');
            INSERT INTO future_durable_data VALUES ('unknown-1', X'CAFE');
            INSERT INTO sqlitex_private VALUES ('private-1', 'must not be omitted');
            INSERT INTO generated_values(base) VALUES (7);
            INSERT INTO nullable_primary_key VALUES (NULL, 'zeta');
            INSERT INTO nullable_primary_key VALUES (NULL, 'alpha');
            INSERT INTO nullable_primary_key VALUES ('known', 'middle');
            ANALYZE;
            """
        )
        connection.commit()
    finally:
        connection.close()

    wiki_path = tmp_path / "external-wiki"
    (wiki_path / "nested").mkdir(parents=True)
    (wiki_path / "Home.md").write_text("# Legacy Wiki\n", encoding="utf-8")
    (wiki_path / "nested" / "raw.bin").write_bytes(b"\x00wiki\xff")
    return db_path, wiki_path


def _file_state(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _tree_state(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            "directory" if path.is_dir() else "file",
            None if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    )


def _sqlite_master_tables(db_path: Path) -> dict[str, str | None]:
    connection = sqlite3.connect(db_path)
    try:
        return dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        )
    finally:
        connection.close()


def _generated_kind(hidden: int) -> str | None:
    return {2: "virtual", 3: "stored"}.get(hidden)


def _load_table(bundle: Path, manifest: dict, table_name: str) -> tuple[dict, dict]:
    table_manifest = next(item for item in manifest["tables"] if item["name"] == table_name)
    export_path = bundle / table_manifest["export_path"]
    assert sha256(export_path.read_bytes()).hexdigest() == table_manifest["sha256"]
    return table_manifest, json.loads(export_path.read_text(encoding="utf-8"))


def _is_utf8(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _decode_typed_value(item: dict):
    storage_class = item["storage_class"]
    value = item["value"]
    if storage_class == "NULL":
        return None
    if storage_class == "INTEGER":
        return int(value)
    if storage_class == "REAL":
        return float.fromhex(value)
    if storage_class == "TEXT":
        if item.get("encoding") == "base64":
            return base64.b64decode(value)
        return value
    if storage_class == "BLOB":
        return base64.b64decode(value)
    raise AssertionError(f"unexpected SQLite storage class: {storage_class}")


def _sortable_row(row: tuple[object, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            "NULL" if value is None else type(value).__name__,
            base64.b64encode(value).decode("ascii")
            if isinstance(value, bytes)
            else float(value).hex()
            if isinstance(value, float)
            else repr(value),
        )
        for value in row
    )


def _directory_link(link: Path, target: Path, *, junction: bool = False) -> None:
    if junction:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"junction unavailable: {result.stderr or result.stdout}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def _remove_directory_link(link: Path, *, junction: bool = False) -> None:
    if junction:
        link.rmdir()
    else:
        link.unlink()


def test_stage_preserves_sources_and_exports_every_sqlite_value_losslessly(
    vault, legacy_sources: tuple[Path, Path], tmp_path: Path
):
    """Removing byte/type/schema preservation or writing outside local pending fails."""
    from mneme.migration.legacy import inspect_legacy_db, stage_legacy

    db_path, wiki_path = legacy_sources
    before_db = db_path.read_bytes()
    before_db_mtime = db_path.stat().st_mtime_ns
    before_wiki = _file_state(wiki_path)
    source_tables = _sqlite_master_tables(db_path)
    inventory = inspect_legacy_db(db_path)

    report = stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    assert db_path.read_bytes() == before_db
    assert db_path.stat().st_mtime_ns == before_db_mtime
    assert _file_state(wiki_path) == before_wiki
    assert db_path.exists() and wiki_path.exists()
    assert set(inspect_legacy_db(db_path).tables) == set(source_tables)

    db_digest = sha256(before_db).hexdigest()
    bundle = vault.local_root / "pending" / "legacy" / db_digest
    assert report.bundle_path == bundle
    assert bundle.is_dir()
    assert bundle.resolve().is_relative_to(vault.state_home.resolve())
    assert not bundle.resolve().is_relative_to(vault.root.resolve())
    assert (bundle / "source" / "state.db").read_bytes() == before_db
    assert not list(bundle.rglob("*.md"))

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["format"] == "madi.legacy-staging-manifest.v1"
    assert manifest["storage_class"] == "local-only"
    assert manifest["confidential"] is True
    assert manifest["source"]["database"]["sha256"] == db_digest
    assert manifest["source"]["database"]["snapshot_sha256"] == db_digest
    assert manifest["source"]["database"]["authority"] == "legacy-runtime"
    assert manifest["source"]["wiki"]["copied"] is False
    assert manifest["source"]["wiki"]["authority"] == "external-source"
    assert manifest["source"]["wiki"]["registration"] == {
        "binding": "machine-local",
        "read_only": True,
        "registry_storage_class": "local-only",
        "status": "requires-authorized-core-api",
    }
    wiki_files = {item["path"]: item for item in manifest["source"]["wiki"]["files"]}
    assert wiki_files["Home.md"]["sha256"] == sha256(before_wiki["Home.md"][0]).hexdigest()
    assert wiki_files["nested/raw.bin"]["sha256"] == sha256(
        before_wiki["nested/raw.bin"][0]
    ).hexdigest()
    assert report.wiki_sha256 == manifest["source"]["wiki"]["sha256"]

    assert {item["name"] for item in manifest["tables"]} == set(source_tables)
    connection = sqlite3.connect(db_path)
    connection.text_factory = lambda raw: (
        raw.decode("utf-8")
        if _is_utf8(raw)
        else raw
    )
    try:
        for table_name in source_tables:
            inspected = inventory.tables[table_name]
            table_manifest, exported = _load_table(bundle, manifest, table_name)
            assert table_manifest["row_count"] == inspected.row_count
            assert exported["format"] == "madi.legacy-sqlite-table.v1"
            assert exported["table"]["name"] == table_name
            assert exported["table"]["schema_sql"] == inspected.schema
            assert exported["table"]["row_count"] == inspected.row_count

            source_columns = connection.execute(
                'SELECT cid, name, type, "notnull", dflt_value, pk, hidden '
                "FROM pragma_table_xinfo(?) ORDER BY cid",
                (table_name,),
            ).fetchall()
            assert exported["table"]["columns"] == [
                {
                    "cid": cid,
                    "name": name,
                    "declared_type": declared_type,
                    "not_null": bool(not_null),
                    "default": default,
                    "primary_key_position": primary_key_position,
                    "hidden": hidden,
                    "generated": _generated_kind(hidden),
                }
                for (
                    cid,
                    name,
                    declared_type,
                    not_null,
                    default,
                    primary_key_position,
                    hidden,
                ) in source_columns
            ]
            assert len(exported["rows"]) == inspected.row_count
            assert [row["ordinal"] for row in exported["rows"]] == list(
                range(1, inspected.row_count + 1)
            )
            assert all(
                row["locator"]["kind"] in {"primary-key", "rowid", "ordinal"}
                for row in exported["rows"]
            )

            escaped_name = table_name.replace('"', '""')
            column_selectors = ", ".join(
                f'"{name.replace(chr(34), chr(34) * 2)}"'
                for _cid, name, *_rest in source_columns
            )
            source_rows = connection.execute(
                f'SELECT {column_selectors} FROM "{escaped_name}"'
            ).fetchall()
            exported_rows = [
                tuple(_decode_typed_value(item) for item in row["values"])
                for row in exported["rows"]
            ]
            # FTS5's table-named hidden control column returns a per-query
            # cursor token (1, 2, ...), not stored row data.  Its presence and
            # storage class are asserted independently below; compare every
            # stable accessible value here.
            stable_indexes = [
                index
                for index, (_cid, name, *_metadata) in enumerate(source_columns)
                if not (table_name == "wiki_fts" and name == table_name)
            ]
            stable_exported_rows = [
                tuple(row[index] for index in stable_indexes) for row in exported_rows
            ]
            stable_source_rows = [
                tuple(row[index] for index in stable_indexes) for row in source_rows
            ]
            assert sorted(stable_exported_rows, key=_sortable_row) == sorted(
                stable_source_rows, key=_sortable_row
            )
    finally:
        connection.close()

    episodes_manifest, episodes = _load_table(bundle, manifest, "episodes")
    assert episodes_manifest["classification"] == "candidate-durable-local"
    assert episodes_manifest["review_status"] == "needs-review"
    values = {item["column"]: item for item in episodes["rows"][0]["values"]}
    assert values["next_hint"] == {"column": "next_hint", "storage_class": "NULL", "value": None}
    assert values["success"] == {
        "column": "success",
        "encoding": "decimal",
        "storage_class": "INTEGER",
        "value": "1",
    }
    assert values["score"] == {
        "column": "score",
        "encoding": "float.hex",
        "storage_class": "REAL",
        "value": float(3.25).hex(),
    }
    assert values["query"] == {
        "column": "query",
        "storage_class": "TEXT",
        "value": "typed payload",
    }
    assert values["what_failed"] == {
        "column": "what_failed",
        "encoding": "base64",
        "storage_class": "BLOB",
        "value": base64.b64encode(b"\x00\xff\x10").decode("ascii"),
    }
    assert episodes["rows"][0]["locator"]["kind"] == "rowid"
    invalid_text_row = next(
        row
        for row in episodes["rows"]
        if {item["column"]: item for item in row["values"]}["session_id"]["value"]
        == "session-2"
    )
    invalid_text_values = {
        item["column"]: item for item in invalid_text_row["values"]
    }
    assert invalid_text_values["query"] == {
        "column": "query",
        "encoding": "base64",
        "storage_class": "TEXT",
        "value": base64.b64encode(b"\x80\xff").decode("ascii"),
    }

    _generated_manifest, generated = _load_table(
        bundle, manifest, "generated_values"
    )
    generated_columns = {
        column["name"]: column for column in generated["table"]["columns"]
    }
    assert generated_columns["virtual_value"]["hidden"] == 2
    assert generated_columns["virtual_value"]["generated"] == "virtual"
    assert generated_columns["stored_value"]["hidden"] == 3
    assert generated_columns["stored_value"]["generated"] == "stored"
    generated_values = {
        item["column"]: _decode_typed_value(item)
        for item in generated["rows"][0]["values"]
    }
    assert generated_values == {
        "base": 7,
        "virtual_value": 14,
        "stored_value": "stored-7",
    }

    _fts_manifest, fts = _load_table(bundle, manifest, "wiki_fts")
    fts_columns = {column["name"]: column for column in fts["table"]["columns"]}
    assert fts_columns["wiki_fts"]["hidden"] == 1
    assert fts_columns["wiki_fts"]["generated"] is None
    assert fts_columns["rank"]["hidden"] == 1
    fts_typed_values = {
        item["column"]: item for item in fts["rows"][0]["values"]
    }
    assert _decode_typed_value(fts_typed_values["path"]) == "wiki/example.md"
    assert _decode_typed_value(fts_typed_values["content"]) == "indexed text"
    assert fts_typed_values["wiki_fts"]["storage_class"] == "INTEGER"
    assert isinstance(_decode_typed_value(fts_typed_values["wiki_fts"]), int)
    assert _decode_typed_value(fts_typed_values["rank"]) is None

    from mneme.core.vault import Vault

    second_vault = Vault.initialize(
        tmp_path / "second-vault", tmp_path / "second-state", "person-01"
    )
    second_report = stage_legacy(
        db_path, wiki_path, second_vault, second_vault.local_root / "pending"
    )
    second_manifest = json.loads(
        (second_report.bundle_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert {item["name"]: item["sha256"] for item in second_manifest["tables"]} == {
        item["name"]: item["sha256"] for item in manifest["tables"]
    }


def test_stage_classifies_every_destination_and_blocks_unknown_portable_action(
    vault, legacy_sources: tuple[Path, Path]
):
    """Changing a review destination or allowing an unknown table to promote fails."""
    from mneme.migration.legacy import stage_legacy

    db_path, wiki_path = legacy_sources
    portable_before = _file_state(vault.root)

    report = stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    manifest = json.loads(
        (report.bundle_path / "manifest.json").read_text(encoding="utf-8")
    )
    tables = {item["name"]: item for item in manifest["tables"]}
    expected_blockers = (
        "future_durable_data",
        "generated_values",
        "nullable_primary_key",
        "sqlitex_private",
    )

    assert report.portable_action == "blocked"
    assert report.blocking_tables == expected_blockers
    assert manifest["portable_action"] == {
        "blocking_tables": list(expected_blockers),
        "reason": "unknown legacy tables require explicit human classification",
        "status": "blocked",
    }

    for table_name in ("facts", "episodes"):
        assert tables[table_name]["classification"] == "candidate-durable-local"
        assert tables[table_name]["destination"] == "local-review"
        assert tables[table_name]["review_status"] == "needs-review"
        assert tables[table_name]["accepted"] is False
        assert tables[table_name]["portable"] is False

    for table_name in ("skills", "loop_cycles", "self_model", "growth_actions"):
        assert tables[table_name]["classification"] == "growth-local"
        assert tables[table_name]["destination"] == "growth-lab-local"
        assert tables[table_name]["portable"] is False

    generated = [item for item in tables.values() if item["classification"] == "generated"]
    assert {item["name"] for item in generated} >= {"wiki_fts", "wiki_index"}
    assert all(item["destination"] == "generated-rebuildable" for item in generated)
    assert all(item["rebuildable"] is True for item in generated)
    assert all(item["durable_markdown_exported"] is False for item in generated)
    assert tables["sqlite_stat1"]["classification"] == "generated"
    assert tables["sqlite_sequence"]["classification"] == "local-transient"
    assert tables["sqlitex_private"]["classification"] == "needs-review"

    unknown = tables["future_durable_data"]
    assert unknown["classification"] == "needs-review"
    assert unknown["destination"] == "local-review"
    assert unknown["blocks_portable_action"] is True
    _unknown_manifest, unknown_export = _load_table(
        report.bundle_path, manifest, "future_durable_data"
    )
    unknown_values = {
        item["column"]: item for item in unknown_export["rows"][0]["values"]
    }
    assert unknown_values["payload"] == {
        "column": "payload",
        "encoding": "base64",
        "storage_class": "BLOB",
        "value": base64.b64encode(b"\xca\xfe").decode("ascii"),
    }

    assert _file_state(vault.root) == portable_before
    assert not (vault.local_root / "bindings" / "sources.yaml").exists()
    assert not list((vault.root / "memory").glob("*.md"))
    assert not list((vault.root / "workstreams").rglob("*.md"))
    assert not list(report.bundle_path.rglob("*.md"))


def test_stage_detects_database_mutation_and_publishes_no_partial_bundle(
    vault,
    legacy_sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """Removing the post-copy source identity check could publish mixed revisions."""
    import mneme.migration.legacy as legacy

    db_path, wiki_path = legacy_sources
    before_digest = sha256(db_path.read_bytes()).hexdigest()
    portable_before = _file_state(vault.root)
    original_copy = legacy._copy_database_snapshot

    def copy_then_mutate(source: Path, target: Path) -> None:
        original_copy(source, target)
        connection = sqlite3.connect(source)
        try:
            connection.execute(
                "INSERT INTO facts(content) VALUES ('concurrent mutation')"
            )
            connection.commit()
        finally:
            connection.close()

    monkeypatch.setattr(legacy, "_copy_database_snapshot", copy_then_mutate)

    with pytest.raises(legacy.LegacyDatabaseChangedError, match="changed"):
        legacy.stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    legacy_root = vault.local_root / "pending" / "legacy"
    assert not (legacy_root / before_digest).exists()
    assert not legacy_root.exists() or list(legacy_root.iterdir()) == []
    assert _file_state(vault.root) == portable_before


def test_typed_rows_use_hidden_rowid_and_ignore_unordered_scan_direction(
    legacy_sources: tuple[Path, Path],
):
    """Nullable declared PKs must not collide or inherit SQLite scan order."""
    import mneme.migration.legacy as legacy

    db_path, _wiki_path = legacy_sources
    table = legacy.inspect_legacy_db(db_path).tables["nullable_primary_key"]
    forward = sqlite3.connect(db_path)
    reverse = sqlite3.connect(db_path)
    try:
        reverse.execute("PRAGMA reverse_unordered_selects = ON")
        forward_rows = legacy._read_typed_rows(forward, table)
        reverse_rows = legacy._read_typed_rows(reverse, table)
    finally:
        forward.close()
        reverse.close()

    assert reverse_rows == forward_rows
    assert [row["ordinal"] for row in forward_rows] == [1, 2, 3]
    assert {row["locator"]["kind"] for row in forward_rows} == {"rowid"}
    rowids = [
        _decode_typed_value(row["locator"]["value"]) for row in forward_rows
    ]
    assert rowids == [1, 2, 3]
    assert len(set(rowids)) == len(forward_rows)


def test_stage_revalidates_database_after_manifest_before_publication(
    vault,
    legacy_sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """A writer racing the last JSON write must invalidate the whole bundle."""
    import mneme.migration.legacy as legacy

    db_path, wiki_path = legacy_sources
    original_digest = sha256(db_path.read_bytes()).hexdigest()
    original_write = legacy._write_json_document

    def write_manifest_then_mutate(path: Path, value: dict) -> None:
        original_write(path, value)
        if path.name == "manifest.json":
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    "INSERT INTO facts(content) VALUES ('late concurrent mutation')"
                )
                connection.commit()
            finally:
                connection.close()

    monkeypatch.setattr(legacy, "_write_json_document", write_manifest_then_mutate)

    with pytest.raises(legacy.LegacyDatabaseChangedError, match="changed"):
        legacy.stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    legacy_root = vault.local_root / "pending" / "legacy"
    assert not (legacy_root / original_digest).exists()
    assert not legacy_root.exists() or list(legacy_root.iterdir()) == []


def test_stage_revalidates_wiki_after_manifest_before_publication(
    vault,
    legacy_sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """A late Wiki edit must not leave a stale manifest or published bundle."""
    import mneme.migration.legacy as legacy

    db_path, wiki_path = legacy_sources
    original_digest = sha256(db_path.read_bytes()).hexdigest()
    original_write = legacy._write_json_document

    def write_manifest_then_mutate(path: Path, value: dict) -> None:
        original_write(path, value)
        if path.name == "manifest.json":
            (wiki_path / "Home.md").write_text(
                "# Changed after manifest\n", encoding="utf-8"
            )

    monkeypatch.setattr(legacy, "_write_json_document", write_manifest_then_mutate)

    with pytest.raises(legacy.LegacyStagingError, match="Wiki changed"):
        legacy.stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    legacy_root = vault.local_root / "pending" / "legacy"
    assert not (legacy_root / original_digest).exists()
    assert not legacy_root.exists() or list(legacy_root.iterdir()) == []


def test_stage_cleans_temporary_bundle_when_export_fails(
    vault,
    legacy_sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """An exception after snapshot creation must not leave a discoverable bundle."""
    import mneme.migration.legacy as legacy

    db_path, wiki_path = legacy_sources
    original_write = legacy._write_json_document

    def fail_table_export(path: Path, value: dict) -> None:
        if path.parent.name == "tables":
            raise OSError("injected table export failure")
        original_write(path, value)

    monkeypatch.setattr(legacy, "_write_json_document", fail_table_export)

    with pytest.raises(legacy.LegacyStagingError, match="staging failed"):
        legacy.stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    legacy_root = vault.local_root / "pending" / "legacy"
    assert not legacy_root.exists() or list(legacy_root.iterdir()) == []


def test_stage_refuses_existing_bundle_without_overwriting_any_byte(
    vault, legacy_sources: tuple[Path, Path]
):
    """Content-address collision handling must be create-only and fail closed."""
    from mneme.core.errors import ArtifactExists
    from mneme.migration.legacy import stage_legacy

    db_path, wiki_path = legacy_sources
    first = stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")
    bundle_before = _tree_state(first.bundle_path)

    with pytest.raises(ArtifactExists):
        stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    assert _tree_state(first.bundle_path) == bundle_before
    assert list(first.bundle_path.parent.iterdir()) == [first.bundle_path]


@pytest.mark.parametrize(
    "overlap",
    [
        "wiki-contains-output-root",
        "same-output-root",
        "same-bundle",
        "bundle-contains-wiki",
    ],
)
def test_stage_rejects_wiki_overlapping_owned_output_before_lock_or_output_creation(
    vault, legacy_sources: tuple[Path, Path], overlap: str
):
    """Owned output paths must not equal, contain, or be contained by the Wiki."""
    from mneme.core.errors import UnsafePath
    from mneme.migration.legacy import stage_legacy

    db_path, _external_wiki = legacy_sources
    pending = vault.local_root / "pending"
    legacy_root = pending / "legacy"
    bundle = legacy_root / sha256(db_path.read_bytes()).hexdigest()
    if overlap == "wiki-contains-output-root":
        wiki_path = pending
    elif overlap == "same-output-root":
        wiki_path = legacy_root
    elif overlap == "same-bundle":
        wiki_path = bundle
    else:
        wiki_path = bundle / "source-wiki"
    wiki_path.mkdir(parents=True, exist_ok=True)
    (wiki_path / "Home.md").write_text("# Still external\n", encoding="utf-8")
    lock_path = vault.local_root / "locks" / "legacy-migration.lock"
    database_before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
    wiki_before = _tree_state(wiki_path)
    wiki_mtime_before = wiki_path.stat().st_mtime_ns

    with pytest.raises(UnsafePath, match="overlap"):
        stage_legacy(db_path, wiki_path, vault, pending)

    assert _tree_state(wiki_path) == wiki_before
    assert wiki_path.stat().st_mtime_ns == wiki_mtime_before
    assert (db_path.read_bytes(), db_path.stat().st_mtime_ns) == database_before
    assert not lock_path.exists()


@pytest.mark.parametrize(
    "overlap", ["lock-parent", "same-lock-path", "lock-path-contains-wiki"]
)
def test_stage_rejects_wiki_overlapping_lock_before_creating_lock_or_output(
    vault, legacy_sources: tuple[Path, Path], overlap: str
):
    """The migration lock must never equal, contain, or be inside the Wiki."""
    from mneme.core.errors import UnsafePath
    from mneme.migration.legacy import stage_legacy

    db_path, _external_wiki = legacy_sources
    lock_parent = vault.local_root / "locks"
    lock_path = lock_parent / "legacy-migration.lock"
    if overlap == "lock-parent":
        wiki_path = lock_parent
    elif overlap == "same-lock-path":
        wiki_path = lock_path
    else:
        wiki_path = lock_path / "source-wiki"
    wiki_path.mkdir(parents=True, exist_ok=True)
    (wiki_path / "Home.md").write_text("# Lock-root Wiki\n", encoding="utf-8")
    pending = vault.local_root / "pending"
    database_before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
    wiki_tree_before = _tree_state(wiki_path)
    wiki_files_before = _file_state(wiki_path)
    wiki_mtime_before = wiki_path.stat().st_mtime_ns

    caught: Exception | None = None
    try:
        stage_legacy(db_path, wiki_path, vault, pending)
    except Exception as error:
        caught = error

    assert _tree_state(wiki_path) == wiki_tree_before
    assert _file_state(wiki_path) == wiki_files_before
    assert wiki_path.stat().st_mtime_ns == wiki_mtime_before
    assert (db_path.read_bytes(), db_path.stat().st_mtime_ns) == database_before
    assert not lock_path.is_file()
    assert not (pending / "legacy").exists()
    assert isinstance(caught, UnsafePath)
    assert "overlap" in str(caught)


@pytest.mark.parametrize("sibling", ["lock", "output"])
def test_stage_allows_wiki_in_unrelated_local_output_siblings(
    vault, legacy_sources: tuple[Path, Path], sibling: str
):
    """Broad parent guards must not reject siblings the migration never writes."""
    from mneme.migration.legacy import stage_legacy

    db_path, _external_wiki = legacy_sources
    if sibling == "lock":
        wiki_path = vault.local_root / "locks" / "source-wiki"
    else:
        wiki_path = vault.local_root / "pending" / "legacy" / "source-wiki"
        wiki_path.parent.mkdir()
    wiki_path.mkdir()
    (wiki_path / "Home.md").write_text("# Safe sibling Wiki\n", encoding="utf-8")
    wiki_before = _tree_state(wiki_path)
    wiki_mtime_before = wiki_path.stat().st_mtime_ns

    report = stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")

    assert report.bundle_path.is_dir()
    assert _tree_state(wiki_path) == wiki_before
    assert wiki_path.stat().st_mtime_ns == wiki_mtime_before


@pytest.mark.parametrize("overlap", ["same-temp", "temp-contains-wiki"])
def test_stage_rejects_wiki_overlapping_generated_temporary_bundle_before_writes(
    vault,
    legacy_sources: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    overlap: str,
):
    """The generated temporary tree is an owned write target, not Wiki space."""
    from mneme.core.errors import UnsafePath
    import mneme.migration.legacy as legacy

    class FixedUuid:
        hex = "1" * 32

    monkeypatch.setattr(legacy, "uuid4", lambda: FixedUuid())
    db_path, _external_wiki = legacy_sources
    pending = vault.local_root / "pending"
    staging = pending / "legacy" / f".{FixedUuid.hex}.staging"
    wiki_path = staging if overlap == "same-temp" else staging / "source-wiki"
    wiki_path.mkdir(parents=True)
    (wiki_path / "Home.md").write_text("# Temporary-path Wiki\n", encoding="utf-8")
    lock_path = vault.local_root / "locks" / "legacy-migration.lock"
    database_before = (db_path.read_bytes(), db_path.stat().st_mtime_ns)
    wiki_before = _tree_state(wiki_path)
    wiki_mtime_before = wiki_path.stat().st_mtime_ns

    with pytest.raises(UnsafePath, match="overlap"):
        legacy.stage_legacy(db_path, wiki_path, vault, pending)

    assert _tree_state(wiki_path) == wiki_before
    assert wiki_path.stat().st_mtime_ns == wiki_mtime_before
    assert (db_path.read_bytes(), db_path.stat().st_mtime_ns) == database_before
    assert not lock_path.exists()


@pytest.mark.parametrize("unsafe_location", ["outside", "portable", "cache"])
def test_stage_rejects_pending_paths_outside_the_exact_local_boundary(
    vault,
    legacy_sources: tuple[Path, Path],
    tmp_path: Path,
    unsafe_location: str,
):
    """Relaxing the explicit pending boundary could put confidential data in Git."""
    from mneme.core.errors import UnsafePath
    from mneme.migration.legacy import stage_legacy

    db_path, wiki_path = legacy_sources
    locations = {
        "outside": tmp_path / "outside-pending",
        "portable": vault.root / "pending",
        "cache": vault.local_root / "cache",
    }
    local_pending = locations[unsafe_location]
    existed_before = local_pending.exists()
    files_before = _file_state(local_pending) if existed_before else {}

    with pytest.raises(UnsafePath):
        stage_legacy(db_path, wiki_path, vault, local_pending)

    assert local_pending.exists() is existed_before
    if existed_before:
        assert _file_state(local_pending) == files_before
    assert not list((vault.root / "memory").glob("*.md"))


def test_stage_rejects_symlinked_pending_ancestor_without_writing_through_it(
    vault, legacy_sources: tuple[Path, Path], tmp_path: Path
):
    """Leaf-only validation would follow pending/legacy into an attacker path."""
    from mneme.core.errors import UnsafePath
    from mneme.migration.legacy import stage_legacy

    db_path, wiki_path = legacy_sources
    target = tmp_path / "outside-target"
    target.mkdir()
    marker = target / "marker.txt"
    marker.write_text("untouched", encoding="utf-8")
    link = vault.local_root / "pending" / "legacy"
    _directory_link(link, target)
    try:
        with pytest.raises(UnsafePath):
            stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")
    finally:
        _remove_directory_link(link)

    assert _file_state(target) == {"marker.txt": (b"untouched", marker.stat().st_mtime_ns)}


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_stage_rejects_junctioned_pending_ancestor_without_writing_through_it(
    vault, legacy_sources: tuple[Path, Path], tmp_path: Path
):
    """Windows junctions are reparse points even when Path.is_symlink is false."""
    from mneme.core.errors import UnsafePath
    from mneme.migration.legacy import stage_legacy

    db_path, wiki_path = legacy_sources
    target = tmp_path / "junction-target"
    target.mkdir()
    link = vault.local_root / "pending" / "legacy"
    _directory_link(link, target, junction=True)
    try:
        with pytest.raises(UnsafePath):
            stage_legacy(db_path, wiki_path, vault, vault.local_root / "pending")
    finally:
        _remove_directory_link(link, junction=True)

    assert list(target.iterdir()) == []


def test_migrate_cli_inspects_then_stages_without_promoting_source_payloads(
    vault,
    legacy_sources: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
):
    """The migrate commands must remain read-only/isolated from remember/checkpoint."""
    from mneme.cli import main

    db_path, wiki_path = legacy_sources
    common = [
        "--vault-root",
        str(vault.root),
        "--state-home",
        str(vault.state_home),
        "migrate",
    ]
    source_before = (db_path.read_bytes(), _file_state(wiki_path))
    portable_before = _file_state(vault.root)

    inspect_exit = main([*common, "inspect", "--db-path", str(db_path)])
    inspect_output = capsys.readouterr()
    inspected = json.loads(inspect_output.out)

    assert inspect_exit == 0
    assert inspect_output.err == ""
    assert inspected["command"] == "migrate"
    assert inspected["status"] == "needs-review"
    assert inspected["result"]["database_sha256"] == sha256(source_before[0]).hexdigest()
    assert {item["name"] for item in inspected["result"]["tables"]} >= {
        "facts",
        "episodes",
        "future_durable_data",
        "sqlite_sequence",
        "sqlitex_private",
    }
    assert not list((vault.local_root / "pending").iterdir())
    assert _file_state(vault.root) == portable_before

    stage_exit = main(
        [
            *common,
            "stage",
            "--db-path",
            str(db_path),
            "--wiki-path",
            str(wiki_path),
        ]
    )
    stage_output = capsys.readouterr()
    staged = json.loads(stage_output.out)

    assert stage_exit == 0
    assert stage_output.err == ""
    assert staged["command"] == "migrate"
    assert staged["status"] == "needs-review"
    assert staged["result"]["portable_action"] == "blocked"
    assert staged["result"]["blocking_tables"] == [
        "future_durable_data",
        "generated_values",
        "nullable_primary_key",
        "sqlitex_private",
    ]
    assert Path(staged["result"]["bundle_path"]).is_relative_to(vault.local_root)
    assert (db_path.read_bytes(), _file_state(wiki_path)) == source_before
    assert _file_state(vault.root) == portable_before
    assert not (vault.local_root / "bindings" / "sources.yaml").exists()

    remember_exit = main(
        [
            "--vault-root",
            str(vault.root),
            "--state-home",
            str(vault.state_home),
            "remember",
            "--kind",
            "lesson",
            "--scope",
            "personal-global",
            "--authority",
            "personal",
            "--portability",
            "personal-vault",
            "--body",
            "Reviewed migration lesson without raw legacy payloads.",
            "--memory-id",
            "reviewed-migration-lesson",
        ]
    )
    remember_output = capsys.readouterr()
    remembered = json.loads(remember_output.out)

    assert remember_exit == 0
    assert remember_output.err == ""
    assert remembered["result"]["status"] == "candidate"
    memory_text = (vault.root / "memory" / "reviewed-migration-lesson.md").read_text(
        encoding="utf-8"
    )
    assert "Reviewed migration lesson" in memory_text
    assert "A potentially durable fact" not in memory_text
    assert "complete result" not in memory_text
    assert not list((vault.root / "workstreams").rglob("*.md"))
