"""Rebuildable, deterministic FTS recall over canonical Vault artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import tempfile
import threading
from typing import Iterable, Mapping

from mneme.core.errors import InvalidArtifact, UnsafePath
from mneme.core.fs import exclusive_file_lock, is_symlink_or_reparse
from mneme.core.memories import MemoryAuthority, MemoryScope, MemoryStore
from mneme.core.registries import RegistryStore
from mneme.core.sessions import SessionRevisionRef, SessionStore
from mneme.core.sources.filesystem import FileSystemSource
from mneme.core.sources.registry import SourceBinding
from mneme.core.validation.vault import validate_identifier

from .korean import expand_query, expand_token


_INDEX_LOCKS_GUARD = threading.Lock()
_INDEX_LOCKS: dict[Path, threading.Lock] = {}
_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_REGISTRY_CATEGORIES = frozenset(
    {"project-registry", "source-registry", "workstream-registry"}
)
_CATEGORIES = _REGISTRY_CATEGORIES | {"memory", "session", "source"}
_SCHEMA_OBJECTS = frozenset(
    {
        "index_state",
        "recall_fts",
        "recall_fts_config",
        "recall_fts_content",
        "recall_fts_data",
        "recall_fts_docsize",
        "recall_fts_idx",
        "recall_meta",
        "sqlite_autoindex_recall_meta_1",
    }
)
_FTS_DECLARATION = (
    "CREATE VIRTUAL TABLE recall_fts USING "
    "fts5(key UNINDEXED, content, tokenize='unicode61')"
)
_TABLE_DECLARATIONS = {
    "recall_meta": (
        "CREATE TABLE recall_meta ( key TEXT PRIMARY KEY, category TEXT NOT NULL, "
        "artifact_id TEXT NOT NULL, revision TEXT, source_id TEXT, path TEXT, "
        "authority TEXT NOT NULL, portability TEXT NOT NULL, policy_status TEXT "
        "NOT NULL, content_hash TEXT NOT NULL, scope TEXT NOT NULL, excerpt_base "
        "INTEGER NOT NULL )"
    ),
    "index_state": (
        "CREATE TABLE index_state ( singleton INTEGER PRIMARY KEY CHECK "
        "(singleton = 1), schema_version INTEGER NOT NULL, status TEXT NOT NULL, "
        "rows INTEGER NOT NULL, source_rows INTEGER NOT NULL, diagnostics TEXT "
        "NOT NULL )"
    ),
}
_TABLE_COLUMNS = {
    "recall_meta": (
        (0, "key", "TEXT", 0, None, 1),
        (1, "category", "TEXT", 1, None, 0),
        (2, "artifact_id", "TEXT", 1, None, 0),
        (3, "revision", "TEXT", 0, None, 0),
        (4, "source_id", "TEXT", 0, None, 0),
        (5, "path", "TEXT", 0, None, 0),
        (6, "authority", "TEXT", 1, None, 0),
        (7, "portability", "TEXT", 1, None, 0),
        (8, "policy_status", "TEXT", 1, None, 0),
        (9, "content_hash", "TEXT", 1, None, 0),
        (10, "scope", "TEXT", 1, None, 0),
        (11, "excerpt_base", "INTEGER", 1, None, 0),
    ),
    "index_state": (
        (0, "singleton", "INTEGER", 0, None, 1),
        (1, "schema_version", "INTEGER", 1, None, 0),
        (2, "status", "TEXT", 1, None, 0),
        (3, "rows", "INTEGER", 1, None, 0),
        (4, "source_rows", "INTEGER", 1, None, 0),
        (5, "diagnostics", "TEXT", 1, None, 0),
    ),
}
_CACHE_ERRORS = (
    OSError,
    sqlite3.Error,
    TypeError,
    ValueError,
    AttributeError,
    KeyError,
    IndexError,
    OverflowError,
    InvalidArtifact,
)


@dataclass(frozen=True, slots=True)
class IndexReport:
    rows: int
    source_rows: int
    status: str
    diagnostics: tuple[str, ...] = ()
    usable: bool = True


@dataclass(frozen=True, slots=True)
class RecallHit:
    """A candidate result.  Callers must authorize it before exposing it."""

    category: str
    artifact_id: str
    revision: str | None
    source_id: str | None
    path: str | None
    authority: str
    portability: str
    policy_status: str
    content_hash: str
    excerpt: str
    excerpt_start: int
    excerpt_end: int
    scope: dict[str, object] | None = None


def _unavailable_report() -> IndexReport:
    return IndexReport(
        0, 0, "degraded", ("generated index is unavailable",), usable=False
    )


def _corrupt_report(exc: BaseException) -> IndexReport:
    return IndexReport(
        0,
        0,
        "degraded",
        (f"generated index is corrupt: {type(exc).__name__}",),
        usable=False,
    )


class GeneratedIndex:
    """A local SQLite cache which can be discarded and rebuilt at any time."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_name(f".{self.db_path.name}.lock")
        normalized = self.lock_path.resolve(strict=False)
        with _INDEX_LOCKS_GUARD:
            self._process_lock = _INDEX_LOCKS.setdefault(normalized, threading.Lock())

    def rebuild(self, vault: object, bound_sources: Iterable[SourceBinding]) -> IndexReport:
        """Replace this generated database from Vault files and available mounts."""
        target_check = lambda: validate_generated_index_target(vault, self.db_path)
        target_check()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        rows: list[_IndexRow] = []
        diagnostics: list[str] = []
        try:
            rows.extend(_vault_rows(vault))
        except Exception as exc:
            report = IndexReport(
                0,
                0,
                "invalid",
                (f"canonical Vault indexing failed: {type(exc).__name__}",),
            )
            self._replace([], report, before_publish=target_check)
            return report

        for binding in sorted(tuple(bound_sources), key=lambda item: item.source_id):
            if not isinstance(binding, SourceBinding):
                raise TypeError("bound_sources must contain SourceBinding values")
            try:
                source = RegistryStore(vault).load_source(binding.source_id)
                if source.kind != "filesystem":
                    diagnostics.append(f"source {source.id} has unsupported kind {source.kind}")
                    continue
                mounted = FileSystemSource(binding.path)
                paths = mounted.list_markdown()
                if not binding.path.is_dir():
                    diagnostics.append(f"source {source.id} is unavailable")
                    continue
                for path in paths:
                    content = mounted.read(path)
                    if content is None:
                        continue
                    for chunk_number, chunk, start in _chunks(content):
                        rows.append(
                            _IndexRow(
                                key=f"source:{source.id}:{path}:{chunk_number}",
                                category="source",
                                artifact_id=source.id,
                                revision=None,
                                source_id=source.id,
                                path=path,
                                authority=source.authority,
                                portability="local-only",
                                policy_status="available",
                                scope={"project_id": source.project} if source.project else None,
                                content=chunk,
                                excerpt_base=start,
                            )
                        )
            except Exception as exc:
                diagnostics.append(f"source {binding.source_id} is unavailable: {type(exc).__name__}")

        source_rows = sum(row.category == "source" for row in rows)
        report = IndexReport(
            len(rows),
            source_rows,
            "degraded" if diagnostics else "resolved",
            tuple(diagnostics),
        )
        self._replace(rows, report, before_publish=target_check)
        return report

    def inspect_read_only(self) -> IndexReport:
        """Inspect without creating the operational lock file used by search."""
        try:
            if not self.db_path.is_file() or self.db_path.is_symlink():
                return _unavailable_report()
            conn = sqlite3.connect(self.db_path)
            try:
                return self._inspect_connection(conn)
            finally:
                conn.close()
        except _CACHE_ERRORS as exc:
            return _corrupt_report(exc)

    def inspect(self) -> IndexReport:
        """Return validated operational index state without granting policy authority."""
        try:
            with self._process_lock:
                with exclusive_file_lock(self.lock_path):
                    if not self.db_path.is_file():
                        return _unavailable_report()
                    conn = sqlite3.connect(self.db_path)
                    try:
                        return self._inspect_connection(conn)
                    finally:
                        conn.close()
        except _CACHE_ERRORS as exc:
            return _corrupt_report(exc)

    def search(self, query: str, limit: int, scope: object = None) -> list[RecallHit]:
        """Return candidates only; callers needing status use ``search_snapshot``."""
        _report, hits = self.search_snapshot(query, limit, scope)
        return hits

    def search_snapshot(
        self, query: str, limit: int, scope: object = None
    ) -> tuple[IndexReport, list[RecallHit]]:
        """Inspect and read candidates from one serialized index snapshot."""
        try:
            with self._process_lock:
                with exclusive_file_lock(self.lock_path):
                    if not self.db_path.is_file():
                        return _unavailable_report(), []
                    conn = sqlite3.connect(self.db_path)
                    try:
                        report = self._inspect_connection(conn)
                        if (
                            report.status == "invalid"
                            or limit <= 0
                            or not isinstance(query, str)
                            or not query.strip()
                        ):
                            return report, []
                        conn.row_factory = sqlite3.Row
                        rows = self._search_rows(
                            conn, query, -1 if scope is not None else limit
                        )
                        return report, self._decode_hits(rows, query, limit, scope)
                    finally:
                        conn.close()
        except _CACHE_ERRORS as exc:
            return _corrupt_report(exc), []

    @staticmethod
    def _decode_hits(rows: object, query: str, limit: int, scope: object) -> list[RecallHit]:
        hits: list[RecallHit] = []
        for row in rows:
            metadata = json.loads(row["scope"])
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("generated scope metadata is invalid")
            if not _in_scope(metadata, scope):
                continue
            content = row["content"]
            start, end = _excerpt_offsets(content, query)
            excerpt = content[start:end]
            hits.append(
                RecallHit(
                    category=row["category"],
                    artifact_id=row["artifact_id"],
                    revision=row["revision"],
                    source_id=row["source_id"],
                    path=row["path"],
                    authority=row["authority"],
                    portability=row["portability"],
                    policy_status=row["policy_status"],
                    content_hash=row["content_hash"],
                    excerpt=excerpt,
                    excerpt_start=row["excerpt_base"] + start,
                    excerpt_end=row["excerpt_base"] + end,
                    scope=metadata,
                )
            )
            if len(hits) == limit:
                break
        return hits

    def _replace(
        self,
        rows: list[_IndexRow],
        report: IndexReport,
        *,
        before_publish=None,
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.db_path.parent,
            prefix=f".{self.db_path.name}.",
            suffix=".rebuilding",
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            conn = sqlite3.connect(temporary)
            try:
                conn.executescript(
                    """
                    CREATE TABLE recall_meta (
                        key TEXT PRIMARY KEY, category TEXT NOT NULL, artifact_id TEXT NOT NULL,
                        revision TEXT, source_id TEXT, path TEXT, authority TEXT NOT NULL,
                        portability TEXT NOT NULL, policy_status TEXT NOT NULL,
                        content_hash TEXT NOT NULL, scope TEXT NOT NULL, excerpt_base INTEGER NOT NULL
                    );
                    CREATE VIRTUAL TABLE recall_fts USING fts5(key UNINDEXED, content, tokenize='unicode61');
                    CREATE TABLE index_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        schema_version INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        rows INTEGER NOT NULL,
                        source_rows INTEGER NOT NULL,
                        diagnostics TEXT NOT NULL
                    );
                    """
                )
                for row in rows:
                    digest = sha256(row.content.encode("utf-8")).hexdigest()
                    conn.execute(
                        "INSERT INTO recall_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (row.key, row.category, row.artifact_id, row.revision, row.source_id,
                         row.path, row.authority, row.portability, row.policy_status, digest,
                         json.dumps(row.scope, sort_keys=True), row.excerpt_base),
                    )
                    conn.execute("INSERT INTO recall_fts VALUES (?, ?)", (row.key, row.content))
                conn.execute(
                    "INSERT INTO index_state VALUES (1, 1, ?, ?, ?, ?)",
                    (
                        report.status,
                        report.rows,
                        report.source_rows,
                        json.dumps(report.diagnostics, ensure_ascii=False),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            if before_publish is not None:
                before_publish()
            self._publish(temporary)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _publish(self, temporary: Path) -> None:
        """Publish under the API lock, using SQLite backup for an open target."""
        with self._process_lock:
            with exclusive_file_lock(self.lock_path):
                if self.db_path.is_file():
                    source = target = None
                    try:
                        source = sqlite3.connect(temporary)
                        target = sqlite3.connect(self.db_path)
                        source.backup(target)
                        return
                    except sqlite3.DatabaseError:
                        pass
                    finally:
                        if target is not None:
                            target.close()
                        if source is not None:
                            source.close()
                os.replace(temporary, self.db_path)

    @staticmethod
    def _inspect_connection(conn: sqlite3.Connection) -> IndexReport:
        check = conn.execute("PRAGMA quick_check").fetchall()
        if check != [("ok",)]:
            raise sqlite3.DatabaseError("generated index integrity check failed")
        _validate_schema(conn)
        row = conn.execute(
            "SELECT schema_version, status, rows, source_rows, diagnostics "
            "FROM index_state WHERE singleton = 1"
        ).fetchone()
        if row is None or row[0] != 1 or row[1] not in {"resolved", "degraded", "invalid"}:
            raise sqlite3.DatabaseError("generated index state is invalid")
        rows, source_rows = row[2], row[3]
        if (
            not isinstance(rows, int)
            or isinstance(rows, bool)
            or rows < 0
            or not isinstance(source_rows, int)
            or isinstance(source_rows, bool)
            or not 0 <= source_rows <= rows
        ):
            raise sqlite3.DatabaseError("generated index counts are invalid")
        diagnostics_value = json.loads(row[4])
        if not isinstance(diagnostics_value, list) or not all(
            isinstance(item, str) for item in diagnostics_value
        ):
            raise ValueError("generated index diagnostics are invalid")
        GeneratedIndex._validate_candidate_rows(conn)
        if row[1] == "invalid" and (
            rows != 0
            or conn.execute("SELECT EXISTS(SELECT 1 FROM recall_meta)").fetchone()[0]
            or conn.execute("SELECT EXISTS(SELECT 1 FROM recall_fts)").fetchone()[0]
        ):
            raise sqlite3.DatabaseError("invalid generated index contains candidate rows")
        return IndexReport(rows, source_rows, row[1], tuple(diagnostics_value))

    @staticmethod
    def _validate_candidate_rows(conn: sqlite3.Connection) -> None:
        columns = (
            "key, category, artifact_id, revision, source_id, path, authority, "
            "portability, policy_status, content_hash, scope, excerpt_base"
        )
        metadata: dict[str, tuple[object, ...]] = {}
        for row in conn.execute(f"SELECT {columns} FROM recall_meta"):
            key = _required_text(row[0], "candidate key")
            if key in metadata:
                raise sqlite3.DatabaseError("generated candidate key is duplicated")
            _validate_metadata_row(row)
            metadata[key] = row

        content_by_key: dict[str, str] = {}
        for key_value, content_value in conn.execute("SELECT key, content FROM recall_fts"):
            key = _required_text(key_value, "FTS key")
            content = _required_text(content_value, "FTS content")
            if key in content_by_key:
                raise sqlite3.DatabaseError("generated FTS key is duplicated")
            content_by_key[key] = content

        if metadata.keys() != content_by_key.keys():
            raise sqlite3.DatabaseError("generated candidate tables do not match")
        for key, row in metadata.items():
            content = content_by_key[key]
            if row[9] != sha256(content.encode("utf-8")).hexdigest():
                raise sqlite3.DatabaseError("generated candidate content hash is invalid")
            if row[11] > (2**63 - 1) - len(content):
                raise sqlite3.DatabaseError("generated candidate excerpt range is invalid")

    @staticmethod
    def _search_rows(conn: sqlite3.Connection, query: str, limit: int):
        sql = """
            SELECT m.*, f.content
            FROM recall_fts AS f JOIN recall_meta AS m ON m.key = f.key
            WHERE recall_fts MATCH ?
            ORDER BY bm25(recall_fts), f.key
            LIMIT ?
        """
        try:
            return conn.execute(sql, (expand_query(query), limit)).fetchall()
        except sqlite3.OperationalError as expanded_error:
            literal = '"' + query.replace('"', '""') + '"'
            try:
                return conn.execute(sql, (literal, limit)).fetchall()
            except sqlite3.OperationalError as literal_error:
                raise literal_error from expanded_error

def validate_generated_index_target(vault: object, db_path: Path) -> Path:
    """Require the one local generated database path before every publication."""
    try:
        local_root = Path(vault.local_root)
        index_root = local_root / "index"
        expected = index_root / "state.db"
        candidate = Path(db_path)
        if (
            is_symlink_or_reparse(local_root)
            or is_symlink_or_reparse(index_root)
            or candidate != expected
            or is_symlink_or_reparse(candidate)
            or not index_root.resolve(strict=False).is_relative_to(
                local_root.resolve(strict=False)
            )
            or index_root.resolve(strict=False) != expected.parent.resolve(strict=False)
        ):
            raise UnsafePath("generated index target is unsafe")
        return expected
    except (AttributeError, OSError, RuntimeError, ValueError) as exc:
        raise UnsafePath("generated index target is unsafe") from exc


def _validate_schema(conn: sqlite3.Connection) -> None:
    objects = {
        name: (object_type, table_name, declaration)
        for object_type, name, table_name, declaration in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        )
    }
    if objects.keys() != _SCHEMA_OBJECTS:
        raise sqlite3.DatabaseError("generated index schema objects are invalid")
    fts_type, fts_table, fts_declaration = objects["recall_fts"]
    if (
        fts_type != "table"
        or fts_table != "recall_fts"
        or fts_declaration != _FTS_DECLARATION
    ):
        raise sqlite3.DatabaseError("generated recall_fts is not canonical FTS5")
    for table, expected_declaration in _TABLE_DECLARATIONS.items():
        object_type, table_name, declaration = objects[table]
        if (
            object_type != "table"
            or table_name != table
            or not isinstance(declaration, str)
            or " ".join(declaration.split()) != expected_declaration
        ):
            raise sqlite3.DatabaseError(
                f"generated {table} declaration is invalid"
            )
    for shadow in (
        "recall_fts_config",
        "recall_fts_content",
        "recall_fts_data",
        "recall_fts_docsize",
        "recall_fts_idx",
    ):
        object_type, table_name, declaration = objects[shadow]
        if object_type != "table" or table_name != shadow or not declaration:
            raise sqlite3.DatabaseError("generated FTS5 shadow schema is invalid")
    if objects["sqlite_autoindex_recall_meta_1"] != (
        "index",
        "recall_meta",
        None,
    ):
        raise sqlite3.DatabaseError("generated recall_meta primary index is invalid")
    for table, expected in _TABLE_COLUMNS.items():
        if tuple(conn.execute(f"PRAGMA table_info({table})")) != expected:
            raise sqlite3.DatabaseError(f"generated {table} columns are invalid")
    if tuple(conn.execute("PRAGMA index_list(recall_meta)")) != (
        (0, "sqlite_autoindex_recall_meta_1", 1, "pk", 0),
    ) or tuple(conn.execute("PRAGMA index_list(index_state)")):
        raise sqlite3.DatabaseError("generated application indexes are invalid")


def _validate_metadata_row(row: tuple[object, ...]) -> None:
    (
        key_value,
        category_value,
        artifact_id_value,
        revision,
        source_id,
        path,
        authority_value,
        portability_value,
        policy_status_value,
        content_hash_value,
        encoded_scope,
        excerpt_base,
    ) = row
    key = _required_text(key_value, "candidate key")
    category = _required_text(category_value, "candidate category")
    artifact_id = _required_identifier(artifact_id_value, "candidate artifact id")
    authority = _required_identifier(authority_value, "candidate authority")
    portability = _required_text(portability_value, "candidate portability")
    policy_status = _required_text(policy_status_value, "candidate policy status")
    content_hash = _required_text(content_hash_value, "candidate content hash")
    if category not in _CATEGORIES:
        raise ValueError("generated candidate category is invalid")
    if not _CONTENT_HASH.fullmatch(content_hash):
        raise ValueError("generated candidate content hash is invalid")
    if (
        not isinstance(excerpt_base, int)
        or isinstance(excerpt_base, bool)
        or excerpt_base < 0
    ):
        raise ValueError("generated candidate excerpt base is invalid")
    if not isinstance(encoded_scope, str):
        raise TypeError("generated candidate scope is not text")
    scope = json.loads(encoded_scope)
    if scope is not None and not isinstance(scope, dict):
        raise ValueError("generated candidate scope is invalid")
    if encoded_scope != json.dumps(scope, sort_keys=True):
        raise ValueError("generated candidate scope is not canonical JSON")

    if category == "source":
        source = _required_identifier(source_id, "candidate source id")
        source_path = _required_source_path(path)
        key_prefix = f"source:{artifact_id}:{source_path}:"
        chunk_number = key.removeprefix(key_prefix)
        if (
            revision is not None
            or artifact_id != source
            or portability != "local-only"
            or policy_status != "available"
            or not key.startswith(key_prefix)
            or not chunk_number.isdigit()
            or str(int(chunk_number)) != chunk_number
        ):
            raise ValueError("generated source candidate is inconsistent")
        if scope is not None:
            if set(scope) != {"project_id"}:
                raise ValueError("generated source scope is invalid")
            _required_identifier(scope["project_id"], "candidate project id")
        return

    if source_id is not None or path is not None:
        raise ValueError("generated Vault candidate names a mounted source")
    if category == "memory":
        if (
            revision is not None
            or key != f"memory:{artifact_id}"
            or authority not in {item.value for item in MemoryAuthority}
            or portability not in {"local-only", "personal-vault", "shareable"}
            or policy_status != "recorded"
            or excerpt_base != 0
        ):
            raise ValueError("generated Memory candidate is inconsistent")
        MemoryScope.parse(scope)
        return

    if category == "session":
        session_revision = _required_identifier(revision, "candidate Session revision")
        if not isinstance(scope, dict) or set(scope) != {"workstream_id"}:
            raise ValueError("generated Session scope is invalid")
        workstream_id = _required_identifier(
            scope["workstream_id"], "candidate workstream id"
        )
        if (
            key != f"session:{workstream_id}:{artifact_id}:{session_revision}"
            or authority != "personal"
            or portability not in {"portable", "local-only"}
            or policy_status != "recorded"
            or excerpt_base != 0
        ):
            raise ValueError("generated Session candidate is inconsistent")
        return

    if (
        revision is not None
        or scope is not None
        or key != f"{category}:{artifact_id}"
        or portability != "personal-vault"
        or policy_status != "available"
        or excerpt_base != 0
    ):
        raise ValueError("generated Registry candidate is inconsistent")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"generated {label} must be non-empty text")
    return value


def _required_identifier(value: object, label: str) -> str:
    text = _required_text(value, label)
    validate_identifier(text, label=label)
    return text


def _required_source_path(value: object) -> str:
    path = _required_text(value, "candidate source path")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if (
        "\\" in path
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
        or posix.suffix.lower() != ".md"
        or posix.as_posix() != path
    ):
        raise ValueError("generated candidate source path is unsafe")
    return path


@dataclass(frozen=True, slots=True)
class _IndexRow:
    key: str
    category: str
    artifact_id: str
    revision: str | None
    source_id: str | None
    path: str | None
    authority: str
    portability: str
    policy_status: str
    scope: dict[str, object] | None
    content: str
    excerpt_base: int = 0


def _vault_rows(vault: object) -> list[_IndexRow]:
    registries = RegistryStore(vault)
    rows: list[_IndexRow] = []
    for directory, loader, category in (
        (vault.root / "projects", registries.load_project, "project-registry"),
        (vault.root / "sources", registries.load_source, "source-registry"),
    ):
        for path in sorted(directory.glob("*.yaml")):
            record = loader(path.stem)
            rows.append(_registry_row(category, record))
    for path in sorted((vault.root / "workstreams").glob("*/workstream.yaml")):
        record = registries.load_workstream(path.parent.name)
        rows.append(_registry_row("workstream-registry", record))

    sessions = SessionStore(vault)
    for path in sorted((vault.root / "workstreams").glob("*/sessions/*/*.md")):
        workstream_id, session_id, revision = path.parts[-4], path.parts[-2], path.stem
        request = sessions.read_revision(SessionRevisionRef(session_id, revision), workstream_id=workstream_id)
        content = _session_text(request)
        rows.append(_IndexRow(
            f"session:{workstream_id}:{session_id}:{revision}", "session", session_id, revision,
            None, None, "personal", request.storage_class.value, "recorded",
            {"workstream_id": workstream_id}, content,
        ))

    for record in MemoryStore(vault).iter_records():
        rows.append(_IndexRow(
            f"memory:{record.id}", "memory", record.id, None, None, None,
            record.authority.value, record.portability.value, "recorded",
            record.scope.as_dict(), record.body,
        ))
    return rows


def _registry_row(category: str, record: object) -> _IndexRow:
    values = asdict(record)
    content = json.dumps(values, sort_keys=True, default=str, ensure_ascii=False)
    return _IndexRow(
        f"{category}:{record.id}", category, record.id, None, None, None,
        getattr(record, "authority", "personal"), "personal-vault", "available", None, content,
    )


def _session_text(request: object) -> str:
    body = request.body
    return "\n".join((
        body.objective, body.current_state, *body.verified_facts, *body.completed_work,
        *body.blockers, *body.next_actions,
    ))


def _chunks(content: str, size: int = 1200) -> list[tuple[int, str, int]]:
    return [(number, content[start : start + size], start) for number, start in enumerate(range(0, len(content), size))]


def _excerpt_offsets(content: str, query: str) -> tuple[int, int]:
    lowered = content.casefold()
    terms = [
        variant.casefold()
        for token in query.split()
        for variant in expand_token(token)
        if variant
    ]
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    if not positions:
        return 0, min(len(content), 240)
    match_start = min(positions)
    matched = next(term for term in terms if lowered.find(term) == match_start)
    return max(0, match_start - 80), min(len(content), match_start + len(matched) + 160)


def _in_scope(record_scope: dict[str, object] | None, scope: object) -> bool:
    if scope is None:
        return True
    if not isinstance(scope, Mapping):
        return False
    if record_scope is None:
        return False
    return all(record_scope.get(key) == value for key, value in scope.items())
