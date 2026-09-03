"""Rebuildable, deterministic FTS recall over canonical Vault artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping

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

    def rebuild(self, vault: object, bound_sources: Iterable[SourceBinding]) -> IndexReport:
        """Replace this generated database from Vault files and available mounts."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        rows: list[_IndexRow] = []
        diagnostics: list[str] = []
        canonical_invalid = False
        try:
            rows.extend(_vault_rows(vault))
        except Exception as exc:
            canonical_invalid = True
            diagnostics.append(f"canonical Vault indexing failed: {type(exc).__name__}")

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

        self._replace(rows)
        source_rows = sum(row.category == "source" for row in rows)
        status = "invalid" if canonical_invalid else ("degraded" if diagnostics else "resolved")
        return IndexReport(len(rows), source_rows, status, tuple(diagnostics))

    def search(self, query: str, limit: int, scope: object = None) -> list[RecallHit]:
        if limit <= 0 or not isinstance(query, str) or not query.strip() or not self.db_path.is_file():
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = self._search_rows(conn, query, -1 if scope is not None else limit)
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return []
        hits: list[RecallHit] = []
        for row in rows:
            metadata = json.loads(row["scope"])
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

    def _replace(self, rows: list[_IndexRow]) -> None:
        temporary = self.db_path.with_suffix(self.db_path.suffix + ".rebuilding")
        if temporary.exists():
            temporary.unlink()
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
                conn.commit()
            finally:
                conn.close()
            temporary.replace(self.db_path)
        finally:
            if temporary.exists():
                temporary.unlink()

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
