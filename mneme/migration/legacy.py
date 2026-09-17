"""Read-only inventory of the mixed-state legacy Mneme SQLite database.

This module deliberately has no dependency on ``mneme.memory``.  In particular,
it never calls its initializer, which could create or migrate a legacy database.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any
from uuid import uuid4

from mneme.core.errors import ArtifactExists, InvalidArtifact, UnsafePath
from mneme.core.fs import (
    exclusive_file_lock,
    is_symlink_or_reparse,
    validate_path_chain,
)


class LegacyInspectionError(RuntimeError):
    """The legacy database could not be safely inventoried."""


class LegacyDatabaseNotFoundError(LegacyInspectionError):
    """The requested legacy database does not exist."""


class LegacyDatabaseNotSQLiteError(LegacyInspectionError):
    """The requested file is not a readable SQLite database."""


class LegacyDatabaseChangedError(LegacyInspectionError):
    """The database changed while its read-only inventory was being collected."""


class LegacyStagingError(LegacyInspectionError):
    """A lossless local staging bundle could not be published safely."""


@dataclass(frozen=True)
class LegacyColumn:
    """Column metadata preserved for later, offline migration review."""

    cid: int
    name: str
    declared_type: str
    not_null: bool
    default: str | None
    primary_key_position: int
    hidden: int
    generated: str | None


@dataclass(frozen=True)
class LegacyTable:
    """A table's immutable inspection result and conservative staging default."""

    name: str
    schema: str | None
    columns: tuple[LegacyColumn, ...]
    row_count: int
    classification: str
    reason: str


@dataclass(frozen=True)
class LegacyInventory:
    """Read-only inventory of one specific byte-stable legacy database file."""

    db_path: Path
    tables: dict[str, LegacyTable]
    sha256: str


@dataclass(frozen=True)
class MigrationReport:
    """Local-only result of staging one closed legacy database and external Wiki."""

    bundle_path: Path
    database_sha256: str
    wiki_sha256: str
    portable_action: str
    blocking_tables: tuple[str, ...]


@dataclass(frozen=True)
class _FileSnapshot:
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class _WikiFileSnapshot:
    path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class _WikiSnapshot:
    root: Path
    files: tuple[_WikiFileSnapshot, ...]
    sha256: str


_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_FTS5_INTERNAL_SUFFIXES = frozenset({"data", "idx", "content", "docsize", "config"})
_SQLITE_PLANNER_TABLES = frozenset(
    {"sqlite_stat1", "sqlite_stat2", "sqlite_stat3", "sqlite_stat4"}
)

_KNOWN_COLUMN_NAMES = {
    "wiki_index": (
        ("path", "summary", "tags", "content_hash", "updated_at", "updated_by"),
        ("path", "summary", "tags", "updated_at", "updated_by"),
    ),
    "working": (("session_id", "key", "value", "expires_at"),),
    "meta": (("key", "value"),),
    "skills": (
        (
            "name",
            "description",
            "wiki_path",
            "base",
            "delta",
            "propensity",
            "state",
            "success_count",
            "use_count",
            "seed_protected",
            "created_at",
            "updated_at",
        ),
    ),
    "loop_cycles": (("id", "ran_at", "episodes_processed", "ci", "bc", "transitions", "propensities"),),
    "self_model": (
        (
            "id",
            "assessed_at",
            "episodes_seen",
            "success_rate",
            "growth_rate",
            "calibration_error",
            "difficulty",
            "regulation",
            "curriculum",
            "notes",
        ),
    ),
    "growth_actions": (
        (
            "id",
            "created_at",
            "last_seen_at",
            "seen_count",
            "kind",
            "severity",
            "subject",
            "detail",
            "dedup_key",
            "status",
            "resolved_at",
            "resolution_note",
        ),
    ),
}

_KNOWN_PRIMARY_KEYS = {
    "wiki_index": (1, 0, 0, 0, 0, 0),
    "working": (1, 2, 0, 0),
    "meta": (1, 0),
    "skills": (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "loop_cycles": (1, 0, 0, 0, 0, 0, 0),
    "self_model": (1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "growth_actions": (1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
}


def stage_legacy(
    db_path: Path,
    wiki_path: Path,
    vault: object,
    local_pending: Path,
) -> MigrationReport:
    """Publish a lossless, confidential staging bundle below local ``pending``.

    The legacy database and Wiki remain external authorities.  This operation
    reads them without SQLite recovery/checkpoint behavior, copies only the
    database bytes into a temporary local directory, exports typed review JSON,
    and renames the completed directory into place as the final publication
    step.  It never writes a portable registry, memory, or session artifact.

    Source writers and other staging processes are expected to be quiescent or
    cooperative.  Repeated byte/directory snapshots and the local staging lock
    detect deterministic/cooperative changes, but portable Python filesystem
    primitives cannot provide handle-relative traversal or an atomic,
    cross-platform rename-if-absent against an adversarial local process.
    """
    pending = _validate_local_pending(vault, local_pending)
    database = _resolve_legacy_path(Path(db_path))
    wiki = _resolve_wiki_path(Path(wiki_path))
    _require_wiki_outside_pending(wiki, pending)
    database_before = _snapshot(database)
    inventory = inspect_legacy_db(database)
    database_after_inventory = _snapshot_after_inspection(database)
    if database_after_inventory != database_before or inventory.sha256 != database_before.sha256:
        raise LegacyDatabaseChangedError(
            f"legacy database changed before staging: {database}"
        )
    wiki_before = _snapshot_wiki(wiki)

    legacy_root = pending / "legacy"
    bundle = legacy_root / database_before.sha256
    lock_path = Path(vault.local_root) / "locks" / "legacy-migration.lock"
    staging: Path | None = None
    published = False

    try:
        with exclusive_file_lock(lock_path):
            pending = _validate_local_pending(vault, pending)
            validate_path_chain(legacy_root, allow_missing=True)
            if is_symlink_or_reparse(legacy_root):
                raise UnsafePath("legacy staging root cannot be a link or reparse point")
            legacy_root.mkdir(parents=False, exist_ok=True)
            validate_path_chain(legacy_root, allow_missing=False)
            if bundle.exists() or is_symlink_or_reparse(bundle):
                raise ArtifactExists("legacy staging bundle already exists")

            staging = legacy_root / f".{uuid4().hex}.staging"
            validate_path_chain(staging, allow_missing=True)
            staging.mkdir(exist_ok=False)
            validate_path_chain(staging, allow_missing=False)
            source_directory = staging / "source"
            table_directory = staging / "tables"
            source_directory.mkdir()
            table_directory.mkdir()
            validate_path_chain(source_directory, allow_missing=False)
            validate_path_chain(table_directory, allow_missing=False)

            database_snapshot = source_directory / "state.db"
            _copy_database_snapshot(database, database_snapshot)
            snapshot_digest = _sha256_file(database_snapshot)
            database_after_copy = _snapshot_after_inspection(database)
            _require_no_sidecars(database, phase="after snapshot copy")
            if (
                database_after_copy != database_before
                or snapshot_digest != database_before.sha256
            ):
                raise LegacyDatabaseChangedError(
                    f"legacy database changed during snapshot: {database}"
                )

            snapshot_inventory = inspect_legacy_db(database_snapshot)
            if snapshot_inventory.sha256 != database_before.sha256:
                raise LegacyStagingError("staged database snapshot is not byte-identical")
            table_manifests = _export_tables(
                database_snapshot, snapshot_inventory, table_directory
            )

            _require_sources_unchanged(
                database,
                database_before,
                wiki,
                wiki_before,
                phase="before manifest construction",
            )

            blocking_tables = tuple(
                item["name"]
                for item in table_manifests
                if item["classification"] == "needs-review"
            )
            portable_action = "blocked" if blocking_tables else "requires-review"
            manifest = _migration_manifest(
                database,
                database_before.sha256,
                snapshot_digest,
                wiki_before,
                table_manifests,
                portable_action,
                blocking_tables,
            )
            _write_json_document(staging / "manifest.json", manifest)

            # This is intentionally after the last bundle write and immediately
            # before publication.  A source change during manifest construction
            # must invalidate the temporary bundle rather than publish stale
            # review metadata.
            _require_sources_unchanged(
                database,
                database_before,
                wiki,
                wiki_before,
                phase="immediately before staging publication",
            )
            _validate_local_pending(vault, pending)
            validate_path_chain(staging, allow_missing=False)
            validate_path_chain(bundle, allow_missing=True)
            if bundle.exists() or is_symlink_or_reparse(bundle):
                raise ArtifactExists("legacy staging bundle appeared during publication")
            try:
                staging.rename(bundle)
            except FileExistsError as error:
                raise ArtifactExists(
                    "legacy staging bundle appeared during publication"
                ) from error
            published = True
    except (LegacyInspectionError, InvalidArtifact, ArtifactExists):
        raise
    except Exception as error:
        raise LegacyStagingError("legacy staging failed before publication") from error
    finally:
        if staging is not None and not published and staging.exists():
            _remove_owned_staging_tree(staging, legacy_root)

    return MigrationReport(
        bundle_path=bundle,
        database_sha256=database_before.sha256,
        wiki_sha256=wiki_before.sha256,
        portable_action=portable_action,
        blocking_tables=blocking_tables,
    )


def inspect_legacy_db(path: Path) -> LegacyInventory:
    """Return a fail-closed, read-only inventory of ``path``.

    The result only describes a database whose bytes and modification time remain
    unchanged throughout inspection.  A concurrent writer produces an explicit
    error instead of a report that could mix two database revisions.
    """
    db_path = _resolve_legacy_path(path)
    before = _snapshot(db_path)
    _require_sqlite_header(db_path)
    _require_no_sidecars(db_path, phase="before inspection")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_readonly_uri(db_path), uri=True)
        connection.execute("BEGIN")
        tables = _read_tables(connection)
    except sqlite3.DatabaseError as error:
        raise _safe_sqlite_error(db_path, error) from error
    except OSError as error:
        raise LegacyDatabaseChangedError(
            f"legacy database changed or became unavailable during inspection: {db_path}"
        ) from error
    finally:
        if connection is not None:
            connection.close()

    after = _snapshot_after_inspection(db_path)
    _require_no_sidecars(db_path, phase="after inspection")
    if after != before:
        raise LegacyDatabaseChangedError(
            f"legacy database changed during inspection; retry after writers stop: {db_path}"
        )

    return LegacyInventory(db_path=db_path, tables=tables, sha256=after.sha256)


def _validate_local_pending(vault: object, local_pending: Path) -> Path:
    try:
        local_root_value = Path(vault.local_root)
        state_home_value = Path(vault.state_home)
        vault_root_value = Path(vault.root)
    except (AttributeError, TypeError) as error:
        raise UnsafePath("legacy staging requires an initialized Vault") from error

    local_root = validate_path_chain(local_root_value, allow_missing=False).resolve(
        strict=True
    )
    state_home = validate_path_chain(state_home_value, allow_missing=False).resolve(
        strict=True
    )
    vault_root = validate_path_chain(vault_root_value, allow_missing=False).resolve(
        strict=True
    )
    expected = validate_path_chain(local_root / "pending", allow_missing=False).resolve(
        strict=True
    )
    supplied = validate_path_chain(Path(local_pending), allow_missing=True).resolve(
        strict=False
    )
    if supplied != expected:
        raise UnsafePath("legacy staging must use the Vault's exact local pending root")
    if not expected.is_relative_to(local_root) or not expected.is_relative_to(state_home):
        raise UnsafePath("legacy staging pending root is outside MADI_STATE_HOME")
    if expected.is_relative_to(vault_root) or vault_root.is_relative_to(expected):
        raise UnsafePath("legacy staging pending root overlaps the portable Vault")
    if not expected.is_dir() or is_symlink_or_reparse(expected):
        raise UnsafePath("legacy staging pending root is unsafe")
    validate_path_chain(expected / "legacy", allow_missing=True)
    return expected


def _resolve_wiki_path(path: Path) -> Path:
    try:
        requested = validate_path_chain(path, allow_missing=False)
        resolved = requested.resolve(strict=True)
    except (OSError, ValueError) as error:
        raise LegacyStagingError("legacy Wiki is unavailable") from error
    if is_symlink_or_reparse(requested) or not resolved.is_dir():
        raise LegacyStagingError("legacy Wiki must be a regular directory")
    return resolved


def _require_wiki_outside_pending(wiki: Path, pending: Path) -> None:
    """Reject either direction of canonical Wiki/output containment."""
    if (
        wiki == pending
        or wiki.is_relative_to(pending)
        or pending.is_relative_to(wiki)
    ):
        raise UnsafePath("legacy Wiki and local pending output must not overlap")


def _require_sources_unchanged(
    database: Path,
    database_before: _FileSnapshot,
    wiki: Path,
    wiki_before: _WikiSnapshot,
    *,
    phase: str,
) -> None:
    database_after = _snapshot_after_inspection(database)
    _require_no_sidecars(database, phase=phase)
    wiki_after = _snapshot_wiki(wiki)
    if database_after != database_before:
        raise LegacyDatabaseChangedError(
            f"legacy database changed during staging: {database}"
        )
    if wiki_after != wiki_before:
        raise LegacyStagingError("legacy Wiki changed during staging")


def _snapshot_wiki(root: Path) -> _WikiSnapshot:
    validate_path_chain(root, allow_missing=False)
    files: list[_WikiFileSnapshot] = []

    def visit(directory: Path) -> None:
        validate_path_chain(directory, allow_missing=False)
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise LegacyStagingError("legacy Wiki cannot be read consistently") from error
        for entry in entries:
            entry_path = Path(entry.path)
            if is_symlink_or_reparse(entry_path):
                raise LegacyStagingError("legacy Wiki contains a link or reparse point")
            validate_path_chain(entry_path, allow_missing=False)
            try:
                if entry.is_dir(follow_symlinks=False):
                    visit(entry_path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise LegacyStagingError(
                        "legacy Wiki contains an unsupported filesystem entry"
                    )
                before = entry.stat(follow_symlinks=False)
                digest = _sha256_file(entry_path)
                after = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise LegacyStagingError(
                    "legacy Wiki changed or became unavailable during hashing"
                ) from error
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise LegacyStagingError("legacy Wiki changed during hashing")
            files.append(
                _WikiFileSnapshot(
                    path=entry_path.relative_to(root).as_posix(),
                    size=after.st_size,
                    mtime_ns=after.st_mtime_ns,
                    sha256=digest,
                )
            )

    visit(root)
    ordered = tuple(sorted(files, key=lambda item: item.path))
    public_files = [
        {"path": item.path, "sha256": item.sha256, "size": item.size}
        for item in ordered
    ]
    return _WikiSnapshot(
        root=root,
        files=ordered,
        sha256=sha256(_json_bytes({"files": public_files})).hexdigest(),
    )


def _copy_database_snapshot(source: Path, target: Path) -> None:
    validate_path_chain(source, allow_missing=False)
    validate_path_chain(target, allow_missing=True)
    validate_path_chain(target.parent, allow_missing=False)
    try:
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as error:
        raise LegacyStagingError("legacy database snapshot copy failed") from error
    validate_path_chain(target, allow_missing=False)


def _export_tables(
    database_snapshot: Path,
    inventory: LegacyInventory,
    table_directory: Path,
) -> list[dict[str, Any]]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_readonly_uri(database_snapshot), uri=True)
        connection.text_factory = bytes
        connection.execute("BEGIN")
        manifests: list[dict[str, Any]] = []
        for ordinal, table_name in enumerate(sorted(inventory.tables), start=1):
            table = inventory.tables[table_name]
            rows = _read_typed_rows(connection, table)
            if len(rows) != table.row_count:
                raise LegacyStagingError(
                    "staged table row count differs from the inspected snapshot"
                )
            document = {
                "format": "madi.legacy-sqlite-table.v1",
                "rows": rows,
                "table": {
                    "classification": table.classification,
                    "columns": [
                        {
                            "cid": column.cid,
                            "declared_type": column.declared_type,
                            "default": column.default,
                            "generated": column.generated,
                            "hidden": column.hidden,
                            "name": column.name,
                            "not_null": column.not_null,
                            "primary_key_position": column.primary_key_position,
                        }
                        for column in table.columns
                    ],
                    "name": table.name,
                    "row_count": table.row_count,
                    "schema_sql": table.schema,
                },
            }
            export_name = f"{ordinal:06d}.json"
            export_path = table_directory / export_name
            _write_json_document(export_path, document)
            semantics = _staging_semantics(table.classification)
            manifests.append(
                {
                    "classification": table.classification,
                    "export_path": f"tables/{export_name}",
                    "name": table.name,
                    "reason": table.reason,
                    "row_count": table.row_count,
                    "sha256": _sha256_file(export_path),
                    **semantics,
                }
            )
        return manifests
    except sqlite3.DatabaseError as error:
        raise LegacyStagingError("staged database could not be exported losslessly") from error
    finally:
        if connection is not None:
            connection.close()


def _read_typed_rows(
    connection: sqlite3.Connection, table: LegacyTable
) -> list[dict[str, Any]]:
    escaped_table = table.name.replace('"', '""')
    table_identifier = f'"{escaped_table}"'
    rowid_alias = _accessible_rowid_alias(connection, table_identifier, table.columns)
    primary_key_indexes = _guaranteed_primary_key_indexes(table.columns)

    selectable_columns: list[tuple[int, LegacyColumn, str]] = []
    unavailable_hidden_columns: set[int] = set()
    for index, column in enumerate(table.columns):
        escaped_column = column.name.replace('"', '""')
        identifier = f'"{escaped_column}"'
        probe = (
            f"SELECT {identifier}, typeof({identifier}) "
            f"FROM {table_identifier} LIMIT 1"
        )
        try:
            connection.execute(probe).fetchall()
        except sqlite3.DatabaseError:
            # Some virtual-table modules expose xinfo-only hidden columns that
            # cannot be selected.  Preserve the metadata and an explicit marker
            # rather than silently dropping the column.  Ordinary and generated
            # columns must remain readable or the export fails closed.
            if column.hidden != 1:
                raise
            unavailable_hidden_columns.add(index)
        else:
            selectable_columns.append((index, column, identifier))

    selectors: list[str] = []
    if rowid_alias is not None:
        selectors.extend((rowid_alias, f"typeof({rowid_alias})"))
    for _index, _column, identifier in selectable_columns:
        selectors.extend((identifier, f"typeof({identifier})"))
    if not selectors:
        selectors.append("1")
    query = f'SELECT {", ".join(selectors)} FROM {table_identifier}'
    raw_rows = connection.execute(query).fetchall()

    prepared: list[tuple[bytes, dict[str, Any]]] = []
    for raw_row in raw_rows:
        offset = 0
        rowid_value: dict[str, Any] | None = None
        if rowid_alias is not None:
            rowid_value = _typed_value("rowid", raw_row[1], raw_row[0])
            offset = 2
        selected_values: dict[int, dict[str, Any]] = {}
        for selected_ordinal, (index, column, _identifier) in enumerate(
            selectable_columns
        ):
            value_offset = offset + selected_ordinal * 2
            selected_values[index] = _typed_value(
                column.name,
                raw_row[value_offset + 1],
                raw_row[value_offset],
            )
        values = [
            selected_values[index]
            if index not in unavailable_hidden_columns
            else _unavailable_hidden_value(column.name)
            for index, column in enumerate(table.columns)
        ]
        if rowid_value is not None:
            locator = {"kind": "rowid", "value": rowid_value}
        elif primary_key_indexes:
            locator = {
                "kind": "primary-key",
                "values": [values[index] for index in primary_key_indexes],
            }
        else:
            locator = {"kind": "ordinal"}
        # Include the complete typed row as a tie-breaker.  This makes output
        # independent of SQLite's unspecified scan order even when a declared
        # primary key permits duplicate NULL values or no unique locator exists.
        sort_key = _json_bytes({"locator": locator, "values": values})
        prepared.append((sort_key, {"locator": locator, "values": values}))

    prepared.sort(key=lambda item: item[0])
    rows: list[dict[str, Any]] = []
    for ordinal, (_sort_key, row) in enumerate(prepared, start=1):
        if row["locator"]["kind"] == "ordinal":
            row["locator"]["value"] = ordinal
        rows.append({"ordinal": ordinal, **row})
    return rows


def _accessible_rowid_alias(
    connection: sqlite3.Connection,
    table_identifier: str,
    columns: tuple[LegacyColumn, ...],
) -> str | None:
    declared_names = {column.name.casefold() for column in columns}
    for alias in ("_rowid_", "rowid", "oid"):
        if alias in declared_names:
            continue
        try:
            connection.execute(
                f"SELECT {alias}, typeof({alias}) FROM {table_identifier} LIMIT 1"
            ).fetchall()
        except sqlite3.DatabaseError:
            continue
        return alias
    return None


def _guaranteed_primary_key_indexes(
    columns: tuple[LegacyColumn, ...],
) -> tuple[int, ...]:
    indexes = tuple(
        index
        for index, column in sorted(
            enumerate(columns),
            key=lambda item: (
                item[1].primary_key_position or len(columns) + 1,
                item[0],
            ),
        )
        if column.primary_key_position > 0
    )
    if not indexes:
        return ()
    if all(columns[index].not_null for index in indexes):
        return indexes
    if (
        len(indexes) == 1
        and columns[indexes[0]].declared_type.strip().casefold() == "integer"
    ):
        return indexes
    return ()


def _unavailable_hidden_value(column: str) -> dict[str, Any]:
    return {
        "accessible": False,
        "column": column,
        "reason": "virtual-table hidden column is not directly selectable",
    }


def _typed_value(column: str, sqlite_type: object, value: object) -> dict[str, Any]:
    if isinstance(sqlite_type, bytes):
        try:
            sqlite_type = sqlite_type.decode("ascii")
        except UnicodeDecodeError as error:
            raise LegacyStagingError("SQLite returned an invalid storage class") from error
    if not isinstance(sqlite_type, str):
        raise LegacyStagingError("SQLite returned an invalid storage class")
    storage_class = sqlite_type.upper()
    encoded: dict[str, Any] = {"column": column, "storage_class": storage_class}
    if storage_class == "NULL":
        encoded["value"] = None
    elif storage_class == "INTEGER":
        encoded.update(encoding="decimal", value=str(value))
    elif storage_class == "REAL":
        encoded.update(encoding="float.hex", value=float(value).hex())
    elif storage_class == "TEXT":
        if isinstance(value, str):
            encoded["value"] = value
        elif isinstance(value, bytes):
            try:
                encoded["value"] = value.decode("utf-8")
            except UnicodeDecodeError:
                encoded.update(
                    encoding="base64",
                    value=base64.b64encode(value).decode("ascii"),
                )
        else:
            raise LegacyStagingError("SQLite TEXT value could not be decoded losslessly")
    elif storage_class == "BLOB":
        try:
            raw = bytes(value)
        except (TypeError, ValueError) as error:
            raise LegacyStagingError("SQLite BLOB value could not be encoded") from error
        encoded.update(encoding="base64", value=base64.b64encode(raw).decode("ascii"))
    else:
        raise LegacyStagingError("SQLite returned an unknown storage class")
    return encoded


def _staging_semantics(classification: str) -> dict[str, Any]:
    common = {
        "accepted": False,
        "blocks_portable_action": False,
        "durable_markdown_exported": False,
        "portable": False,
        "rebuildable": False,
    }
    if classification == "candidate-durable-local":
        return {**common, "destination": "local-review", "review_status": "needs-review"}
    if classification == "growth-local":
        return {**common, "destination": "growth-lab-local", "review_status": "local-only"}
    if classification == "generated":
        return {
            **common,
            "destination": "generated-rebuildable",
            "rebuildable": True,
            "review_status": "rebuildable",
        }
    if classification == "local-transient":
        return {**common, "destination": "local-transient", "review_status": "local-only"}
    return {
        **common,
        "blocks_portable_action": True,
        "destination": "local-review",
        "review_status": "needs-review",
    }


def _migration_manifest(
    database: Path,
    database_sha256: str,
    snapshot_sha256: str,
    wiki: _WikiSnapshot,
    tables: list[dict[str, Any]],
    portable_action: str,
    blocking_tables: tuple[str, ...],
) -> dict[str, Any]:
    action_reason = (
        "unknown legacy tables require explicit human classification"
        if blocking_tables
        else "staging never authorizes portable registration or promotion"
    )
    return {
        "confidential": True,
        "format": "madi.legacy-staging-manifest.v1",
        "portable_action": {
            "blocking_tables": list(blocking_tables),
            "reason": action_reason,
            "status": portable_action,
        },
        "source": {
            "database": {
                "authority": "legacy-runtime",
                "copied": True,
                "path": str(database),
                "sha256": database_sha256,
                "snapshot_sha256": snapshot_sha256,
            },
            "wiki": {
                "authority": "external-source",
                "copied": False,
                "files": [
                    {"path": item.path, "sha256": item.sha256, "size": item.size}
                    for item in wiki.files
                ],
                "path": str(wiki.root),
                "registration": {
                    "binding": "machine-local",
                    "read_only": True,
                    "registry_storage_class": "local-only",
                    "status": "requires-authorized-core-api",
                },
                "sha256": wiki.sha256,
            },
        },
        "storage_class": "local-only",
        "tables": tables,
    }


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _write_json_document(path: Path, value: dict[str, Any]) -> None:
    target = validate_path_chain(path, allow_missing=True)
    validate_path_chain(target.parent, allow_missing=False)
    encoded = _json_bytes(value)
    try:
        with target.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise LegacyStagingError("legacy staging JSON write failed") from error
    validate_path_chain(target, allow_missing=False)


def _remove_owned_staging_tree(staging: Path, legacy_root: Path) -> None:
    root = validate_path_chain(legacy_root, allow_missing=False).resolve(strict=True)
    target = validate_path_chain(staging, allow_missing=False).resolve(strict=True)
    if target.parent != root or not target.name.startswith(".") or not target.name.endswith(
        ".staging"
    ):
        raise LegacyStagingError("refusing to clean an unowned staging path")
    shutil.rmtree(target)


def _resolve_legacy_path(path: Path) -> Path:
    try:
        db_path = Path(path).resolve(strict=True)
    except FileNotFoundError as error:
        raise LegacyDatabaseNotFoundError(f"legacy database does not exist: {path}") from error
    except OSError as error:
        raise LegacyInspectionError(f"cannot access legacy database: {path}") from error

    if not db_path.is_file():
        raise LegacyDatabaseNotSQLiteError(f"legacy database is not a regular file: {db_path}")
    return db_path


def _snapshot(path: Path) -> _FileSnapshot:
    try:
        stat = path.stat()
        digest = _sha256_file(path)
    except FileNotFoundError as error:
        raise LegacyDatabaseNotFoundError(f"legacy database does not exist: {path}") from error
    except OSError as error:
        raise LegacyInspectionError(f"cannot read legacy database: {path}") from error
    return _FileSnapshot(size=stat.st_size, mtime_ns=stat.st_mtime_ns, sha256=digest)


def _snapshot_after_inspection(path: Path) -> _FileSnapshot:
    try:
        return _snapshot(path)
    except LegacyDatabaseNotFoundError as error:
        raise LegacyDatabaseChangedError(
            f"legacy database disappeared during inspection: {path}"
        ) from error


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sqlite_header(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            header = handle.read(len(_SQLITE_HEADER))
    except OSError as error:
        raise LegacyInspectionError(f"cannot read legacy database header: {path}") from error
    if header != _SQLITE_HEADER:
        raise LegacyDatabaseNotSQLiteError(f"not a SQLite database: {path}")


def _readonly_uri(path: Path) -> str:
    """Build a Windows-safe immutable read-only URI from an absolute path.

    ``immutable=1`` prevents SQLite from creating or updating source journal/WAL
    support files.  Databases with any such sidecar are rejected before opening,
    because immutable access would otherwise ignore uncheckpointed WAL content.
    """
    return f"{path.as_uri()}?mode=ro&immutable=1"


def _require_no_sidecars(path: Path, *, phase: str) -> None:
    try:
        sidecars = [
            sidecar
            for suffix in _SQLITE_SIDECAR_SUFFIXES
            if (sidecar := path.with_name(f"{path.name}{suffix}")).exists()
        ]
    except OSError as error:
        raise LegacyInspectionError(
            f"cannot check SQLite sidecars {phase}: {path}"
        ) from error
    if sidecars:
        names = ", ".join(sidecar.name for sidecar in sidecars)
        raise LegacyDatabaseChangedError(
            f"SQLite sidecar present {phase}; inspection requires a closed database: {names}"
        )


def _read_tables(connection: sqlite3.Connection) -> dict[str, LegacyTable]:
    rows = connection.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()

    raw_tables: list[tuple[str, str | None, tuple[LegacyColumn, ...], int]] = []
    for name, schema in rows:
        columns = _read_columns(connection, name)
        row_count = _read_row_count(connection, name)
        raw_tables.append((name, schema, columns, row_count))

    recognized_fts = _is_recognized_wiki_fts(raw_tables)
    return {
        name: LegacyTable(
            name=name,
            schema=schema,
            columns=columns,
            row_count=row_count,
            classification=classification,
            reason=reason,
        )
        for name, schema, columns, row_count in raw_tables
        for classification, reason in [
            _classify_table(name, schema, columns, recognized_fts)
        ]
    }


def _read_columns(connection: sqlite3.Connection, table_name: str) -> tuple[LegacyColumn, ...]:
    rows = connection.execute(
        """
        SELECT cid, name, type, "notnull", dflt_value, pk, hidden
        FROM pragma_table_xinfo(?)
        ORDER BY cid
        """,
        (table_name,),
    ).fetchall()
    return tuple(
        LegacyColumn(
            cid=cid,
            name=name,
            declared_type=declared_type,
            not_null=bool(not_null),
            default=default,
            primary_key_position=primary_key_position,
            hidden=hidden,
            generated={2: "virtual", 3: "stored"}.get(hidden),
        )
        for (
            cid,
            name,
            declared_type,
            not_null,
            default,
            primary_key_position,
            hidden,
        ) in rows
    )


def _read_row_count(connection: sqlite3.Connection, table_name: str) -> int:
    escaped_name = table_name.replace('"', '""')
    return connection.execute(f'SELECT COUNT(*) FROM "{escaped_name}"').fetchone()[0]


def _is_recognized_wiki_fts(
    tables: list[tuple[str, str | None, tuple[LegacyColumn, ...], int]],
) -> bool:
    for name, schema, columns, _row_count in tables:
        if name != "wiki_fts":
            continue
        return (
            schema is not None
            and "using fts5" in schema.casefold()
            and _visible_column_names(columns) == ("path", "content")
        )
    return False


def _classify_table(
    name: str,
    schema: str | None,
    columns: tuple[LegacyColumn, ...],
    recognized_fts: bool,
) -> tuple[str, str]:
    if name == "sqlite_sequence":
        return (
            "local-transient",
            "SQLite AUTOINCREMENT runtime metadata; preserve locally without promotion",
        )

    if name in _SQLITE_PLANNER_TABLES:
        return (
            "generated",
            "SQLite query-planner statistics; preserved in the snapshot and rebuildable",
        )

    if name == "wiki_fts" and recognized_fts:
        return "generated", "recognized Mneme FTS5 index; rebuildable generated search state"

    if _is_recognized_fts_internal(name, recognized_fts):
        return "generated", "recognized internal table of the Mneme wiki_fts FTS5 index"

    if name == "wiki_index" and _has_known_columns(name, columns):
        return "generated", "recognized Mneme Wiki index; rebuildable generated search state"

    if name in {"facts", "episodes"}:
        return (
            "candidate-durable-local",
            "potentially durable legacy knowledge; retain losslessly in local review staging",
        )

    if name in {"working", "meta"} and _has_known_columns(name, columns):
        return "local-transient", "recognized Mneme runtime metadata with local-only semantics"

    if name in {"skills", "loop_cycles", "self_model", "growth_actions"} and _has_known_columns(
        name, columns
    ):
        return "growth-local", "recognized Mneme Growth state; keep local and do not promote automatically"

    return "needs-review", "not a recognized Mneme legacy table; retain losslessly for manual review"


def _is_recognized_fts_internal(name: str, recognized_fts: bool) -> bool:
    if not recognized_fts or not name.startswith("wiki_fts_"):
        return False
    suffix = name.removeprefix("wiki_fts_")
    return suffix in _FTS5_INTERNAL_SUFFIXES


def _has_known_columns(name: str, columns: tuple[LegacyColumn, ...]) -> bool:
    return (
        _column_names(columns) in _KNOWN_COLUMN_NAMES.get(name, ())
        and tuple(column.primary_key_position for column in columns)
        == _KNOWN_PRIMARY_KEYS.get(name)
    )


def _column_names(columns: tuple[LegacyColumn, ...]) -> tuple[str, ...]:
    return tuple(column.name for column in columns)


def _visible_column_names(columns: tuple[LegacyColumn, ...]) -> tuple[str, ...]:
    return tuple(column.name for column in columns if column.hidden == 0)


def _safe_sqlite_error(path: Path, error: sqlite3.DatabaseError) -> LegacyInspectionError:
    message = str(error).casefold()
    if "locked" in message or "busy" in message or "unable to open" in message:
        return LegacyDatabaseChangedError(
            f"legacy database could not be read consistently; retry after writers stop: {path}"
        )
    return LegacyDatabaseNotSQLiteError(f"not a readable SQLite database: {path}")
