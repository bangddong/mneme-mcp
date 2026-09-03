"""Rebuildable, deterministic FTS recall over canonical Vault artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Iterable, Mapping

from mneme.core.fs import exclusive_file_lock
from mneme.core.memories import MemoryStore
from mneme.core.registries import RegistryStore
from mneme.core.sessions import SessionRevisionRef, SessionStore
from mneme.core.sources.filesystem import FileSystemSource
from mneme.core.sources.registry import SourceBinding

from .korean import expand_query, expand_token


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


class GeneratedIndex:
    """A local SQLite cache which can be discarded and rebuilt at any time."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_name(f".{self.db_path.name}.lock")

    def rebuild(self, vault: object, bound_sources: Iterable[SourceBinding]) -> IndexReport:
        """Replace this generated database from Vault files and available mounts."""
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
            self._replace([], report)
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
        self._replace(rows, report)
        return report

    def inspect(self) -> IndexReport:
        """Return validated operational index state without granting policy authority."""
        if not self.db_path.is_file():
            return IndexReport(
                0, 0, "degraded", ("generated index is unavailable",), usable=False
            )
        try:
            with exclusive_file_lock(self.lock_path):
                conn = sqlite3.connect(self.db_path)
                try:
                    return self._inspect_connection(conn)
                finally:
                    conn.close()
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            return IndexReport(
                0,
                0,
                "degraded",
                (f"generated index is corrupt: {type(exc).__name__}",),
                usable=False,
            )

    def search(self, query: str, limit: int, scope: object = None) -> list[RecallHit]:
        if limit <= 0 or not isinstance(query, str) or not query.strip() or not self.db_path.is_file():
            return []
        try:
            with exclusive_file_lock(self.lock_path):
                conn = sqlite3.connect(self.db_path)
                try:
                    report = self._inspect_connection(conn)
                    if report.status == "invalid":
                        return []
                    conn.row_factory = sqlite3.Row
                    rows = self._search_rows(
                        conn, query, -1 if scope is not None else limit
                    )
                    return self._decode_hits(rows, query, limit, scope)
                finally:
                    conn.close()
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            return []

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

    def _replace(self, rows: list[_IndexRow], report: IndexReport) -> None:
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
            self._publish(temporary)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _publish(self, temporary: Path) -> None:
        """Publish under the API lock, using SQLite backup for an open target."""
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
        for (encoded_scope,) in conn.execute("SELECT scope FROM recall_meta"):
            decoded_scope = json.loads(encoded_scope)
            if decoded_scope is not None and not isinstance(decoded_scope, dict):
                raise ValueError("generated scope metadata is invalid")
        if row[1] == "invalid" and (
            rows != 0
            or conn.execute("SELECT EXISTS(SELECT 1 FROM recall_meta)").fetchone()[0]
            or conn.execute("SELECT EXISTS(SELECT 1 FROM recall_fts)").fetchone()[0]
        ):
            raise sqlite3.DatabaseError("invalid generated index contains candidate rows")
        return IndexReport(rows, source_rows, row[1], tuple(diagnostics_value))

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
        except sqlite3.OperationalError:
            literal = '"' + query.replace('"', '""') + '"'
            try:
                return conn.execute(sql, (literal, limit)).fetchall()
            except sqlite3.OperationalError:
                return []


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
