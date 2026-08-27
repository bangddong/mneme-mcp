"""Read-only inventory of the mixed-state legacy Mneme SQLite database.

This module deliberately has no dependency on ``mneme.memory``.  In particular,
it never calls its initializer, which could create or migrate a legacy database.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import sqlite3


class LegacyInspectionError(RuntimeError):
    """The legacy database could not be safely inventoried."""


class LegacyDatabaseNotFoundError(LegacyInspectionError):
    """The requested legacy database does not exist."""


class LegacyDatabaseNotSQLiteError(LegacyInspectionError):
    """The requested file is not a readable SQLite database."""


class LegacyDatabaseChangedError(LegacyInspectionError):
    """The database changed while its read-only inventory was being collected."""


@dataclass(frozen=True)
class LegacyColumn:
    """Column metadata preserved for later, offline migration review."""

    name: str
    declared_type: str
    not_null: bool
    default: str | None
    primary_key_position: int


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
class _FileSnapshot:
    size: int
    mtime_ns: int
    sha256: str


_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_FTS5_INTERNAL_SUFFIXES = frozenset({"data", "idx", "content", "docsize", "config"})

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
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
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
        SELECT name, type, "notnull", dflt_value, pk
        FROM pragma_table_info(?)
        ORDER BY cid
        """,
        (table_name,),
    ).fetchall()
    return tuple(
        LegacyColumn(
            name=name,
            declared_type=declared_type,
            not_null=bool(not_null),
            default=default,
            primary_key_position=primary_key_position,
        )
        for name, declared_type, not_null, default, primary_key_position in rows
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
            and _column_names(columns) == ("path", "content")
        )
    return False


def _classify_table(
    name: str,
    schema: str | None,
    columns: tuple[LegacyColumn, ...],
    recognized_fts: bool,
) -> tuple[str, str]:
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


def _safe_sqlite_error(path: Path, error: sqlite3.DatabaseError) -> LegacyInspectionError:
    message = str(error).casefold()
    if "locked" in message or "busy" in message or "unable to open" in message:
        return LegacyDatabaseChangedError(
            f"legacy database could not be read consistently; retry after writers stop: {path}"
        )
    return LegacyDatabaseNotSQLiteError(f"not a readable SQLite database: {path}")
