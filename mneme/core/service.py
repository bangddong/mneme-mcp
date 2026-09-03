"""Small, deterministic Core composition surface for generated recall and context."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from mneme.core.artifacts import StorageClass
from mneme.core.context import ContextReaders, ContextView, render_current
from mneme.core.memories import MemoryRecord, MemoryStatus, MemoryStore
from mneme.core.policy import PolicyStore
from mneme.core.registries import RegistryStore
from mneme.core.resolver import SessionRevision
from mneme.core.search.index import (
    GeneratedIndex,
    IndexReport,
    RecallHit,
    _IndexRow,
    _chunks,
    _registry_row,
    _session_text,
)
from mneme.core.sessions import SessionRevisionRef, SessionStore
from mneme.core.sources.filesystem import FileSystemSource
from mneme.core.sources.registry import SourceBinding, SourceBindingStore


@dataclass(frozen=True, slots=True)
class RecallResult:
    hits: tuple[RecallHit, ...]
    status: str
    diagnostics: tuple[str, ...] = ()


class CoreService:
    """Coordinates read-only canonical readers with disposable local index state."""

    def __init__(self, vault: object, index: GeneratedIndex | None = None):
        self.vault = vault
        self.index = index or GeneratedIndex(vault.local_root / "index" / "state.db")

    def reindex(self) -> IndexReport:
        bindings, diagnostics = self._bound_sources()
        report = self.index.rebuild(self.vault, tuple(bindings))
        all_diagnostics = tuple(sorted(set(report.diagnostics + tuple(diagnostics))))
        status = report.status
        if status != "invalid" and all_diagnostics:
            status = "degraded"
        return IndexReport(report.rows, report.source_rows, status, all_diagnostics)

    def recall(self, query: str, limit: int, scope: object = None) -> RecallResult:
        hits: list[RecallHit] = []
        diagnostics = list(self._bound_sources()[1])
        candidate_limit = limit
        processed = 0
        rebuilt = False
        while True:
            report, candidates = self.index.search_snapshot(
                query, candidate_limit, scope
            )
            if not report.usable:
                if rebuilt:
                    return RecallResult(
                        (),
                        "degraded",
                        tuple(sorted(set(diagnostics + list(report.diagnostics)))),
                    )
                try:
                    rebuilt_report = self.reindex()
                except Exception as exc:
                    return RecallResult(
                        (),
                        "degraded",
                        (f"generated index rebuild failed: {type(exc).__name__}",),
                    )
                if rebuilt_report.status == "invalid":
                    return RecallResult(
                        (), "invalid", rebuilt_report.diagnostics
                    )
                rebuilt = True
                processed = 0
                candidate_limit = limit
                continue
            if report.status == "invalid":
                return RecallResult((), "invalid", report.diagnostics)
            diagnostics.extend(report.diagnostics)
            if candidate_limit <= 0:
                break
            for candidate in candidates[processed:]:
                try:
                    if self._authorize(candidate):
                        hits.append(candidate)
                    else:
                        diagnostics.append(f"{candidate.category} candidate was withheld")
                except Exception:
                    diagnostics.append(f"{candidate.category} candidate was withheld")
                if len(hits) == limit:
                    break
            if len(hits) == limit or len(candidates) < candidate_limit:
                break
            processed = len(candidates)
            candidate_limit *= 2
        return RecallResult(
            tuple(hits),
            "degraded" if diagnostics else "resolved",
            tuple(sorted(set(diagnostics))),
        )

    def context(self, workstream_id: str, mode: str = "portable") -> ContextView:
        registries = RegistryStore(self.vault)
        sessions = SessionStore(self.vault)
        _bindings, source_diagnostics = self._bound_sources()
        readers = ContextReaders(
            registries.load_workstream,
            lambda selected, head: SessionRevision(
                head,
                sessions.read_revision(
                    SessionRevisionRef(head.session, head.revision),
                    workstream_id=selected,
                ),
            ),
            portable_optional_inputs=lambda _selected: (
                self.index.db_path.is_file(),
                not source_diagnostics,
            ),
        )
        return render_current(readers, workstream_id, mode)

    def _bound_sources(self) -> tuple[list[SourceBinding], list[str]]:
        bindings: list[SourceBinding] = []
        diagnostics: list[str] = []
        registries = RegistryStore(self.vault)
        binding_store = SourceBindingStore(self.vault)
        for source_file in sorted((self.vault.root / "sources").glob("*.yaml")):
            source_id = source_file.stem
            try:
                registries.load_source(source_id)
                binding = binding_store.load(source_id)
                if binding is None or not binding.path.is_dir():
                    diagnostics.append(f"source {source_id} is unavailable")
                else:
                    bindings.append(binding)
            except Exception:
                diagnostics.append(f"source {source_id} is unavailable")
        return bindings, diagnostics

    def _authorize(self, hit: RecallHit) -> bool:
        self._current_policy_pointers()
        if hit.category == "source":
            return self._authorize_source(hit)
        if hit.category == "memory":
            memories = MemoryStore(self.vault)
            record = memories.read(hit.artifact_id)
            return (
                self._accepted_memory_is_current(memories, record)
                and hit.revision is None
                and hit.source_id is None
                and hit.path is None
                and hit.authority == record.authority.value
                and hit.portability == record.portability.value
                and hit.scope == record.scope.as_dict()
                and sha256(record.body.encode("utf-8")).hexdigest() == hit.content_hash
                and self._excerpt_matches(record.body, hit)
            )
        if hit.category == "session":
            request = SessionStore(self.vault).read_revision(
                SessionRevisionRef(hit.artifact_id, hit.revision or ""),
                workstream_id=(hit.scope or {}).get("workstream_id"),
            )
            return (
                hit.source_id is None
                and hit.path is None
                and hit.authority == "personal"
                and hit.portability == request.storage_class.value
                and hit.scope == {"workstream_id": request.workstream_id}
                and sha256(_session_text(request).encode("utf-8")).hexdigest()
                == hit.content_hash
                and self._excerpt_matches(_session_text(request), hit)
            )
        if hit.category == "source-registry":
            return self._authorize_registry(
                hit, RegistryStore(self.vault).load_source(hit.artifact_id)
            )
        if hit.category == "project-registry":
            return self._authorize_registry(
                hit, RegistryStore(self.vault).load_project(hit.artifact_id)
            )
        if hit.category == "workstream-registry":
            return self._authorize_registry(
                hit, RegistryStore(self.vault).load_workstream(hit.artifact_id)
            )
        return False

    def _authorize_registry(self, hit: RecallHit, record: object) -> bool:
        policies = PolicyStore(self.vault)
        policy_ref = getattr(record, "policy_ref", None)
        if policy_ref is not None:
            policies.load_rule(policy_ref)
        for reference in getattr(record, "policy_refs", ()):
            policies.load_rule(reference)
        return self._registry_matches(hit, record)

    @staticmethod
    def _accepted_memory_is_current(
        memories: MemoryStore, record: MemoryRecord
    ) -> bool:
        if record.status is not MemoryStatus.ACCEPTED:
            return False
        if record.superseded_by is None:
            return True
        successor = memories.read(record.superseded_by)
        return successor.accepted_semantic_hash is None

    def _current_policy_pointers(self) -> None:
        PolicyStore(self.vault).load_active("vault-default", StorageClass.PORTABLE)

    def _authorize_source(self, hit: RecallHit) -> bool:
        if hit.source_id is None or hit.path is None:
            return False
        source = RegistryStore(self.vault).load_source(hit.source_id)
        expected_scope = {"project_id": source.project} if source.project else None
        if (
            source.kind != "filesystem"
            or hit.artifact_id != source.id
            or hit.revision is not None
            or hit.authority != source.authority
            or hit.portability != "local-only"
            or hit.scope != expected_scope
        ):
            return False
        binding = SourceBindingStore(self.vault).load(hit.source_id)
        if binding is None:
            return False
        content = FileSystemSource(binding.path).read(hit.path)
        if content is None:
            return False
        if source.policy_ref is not None:
            PolicyStore(self.vault).load_rule(source.policy_ref)
        return self._excerpt_matches(content, hit) and any(
            sha256(chunk.encode("utf-8")).hexdigest() == hit.content_hash
            and start <= hit.excerpt_start
            and hit.excerpt_end <= start + len(chunk)
            for _number, chunk, start in _chunks(content)
        )

    @staticmethod
    def _registry_matches(hit: RecallHit, record: object) -> bool:
        row = _registry_row(hit.category, record)
        return CoreService._metadata_matches(hit, row) and (
            sha256(row.content.encode("utf-8")).hexdigest() == hit.content_hash
            and CoreService._excerpt_matches(row.content, hit)
        )

    @staticmethod
    def _metadata_matches(hit: RecallHit, row: _IndexRow) -> bool:
        """Compare cache provenance with canonical metadata, excluding policy hints."""
        return (
            hit.category == row.category
            and hit.artifact_id == row.artifact_id
            and hit.revision == row.revision
            and hit.source_id == row.source_id
            and hit.path == row.path
            and hit.authority == row.authority
            and hit.portability == row.portability
            and hit.scope == row.scope
        )

    @staticmethod
    def _excerpt_matches(content: str, hit: RecallHit) -> bool:
        return (
            isinstance(hit.excerpt_start, int)
            and not isinstance(hit.excerpt_start, bool)
            and isinstance(hit.excerpt_end, int)
            and not isinstance(hit.excerpt_end, bool)
            and 0 <= hit.excerpt_start < hit.excerpt_end <= len(content)
            and content[hit.excerpt_start : hit.excerpt_end] == hit.excerpt
        )
