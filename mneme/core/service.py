"""Small, deterministic Core composition surface for generated recall and context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any
from uuid import uuid4

from mneme.core.artifacts import (
    ArtifactReference,
    ReferenceKind,
    StorageClass,
)
from mneme.core.contracts import (
    CONTRACT_VERSION,
    CommandResult,
    CoreCommand,
    DomainEvent,
    LifecycleEvent,
    ObservationResult,
    PolicyDenied,
)
from mneme.core.context import ContextReaders, ContextView, render_current
from mneme.core.errors import InvalidArtifact
from mneme.core.memories import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryStore,
    memory_semantic_hash,
)
from mneme.core.policy import (
    PolicyRef,
    PolicyStore,
    Portability,
    evaluate_portability,
    policy_admission_gate,
)
from mneme.core.registries import HeadRef, RegistryStore
from mneme.core.resolver import SessionRevision
from mneme.core.security import PolicyArtifactRef, PolicyAuthorizer, PolicyDecision
from mneme.core.search.index import (
    GeneratedIndex,
    IndexReport,
    RecallHit,
    _IndexRow,
    _chunks,
    _registry_row,
    _session_text,
)
from mneme.core.sessions import (
    CheckpointRequest,
    SessionBody,
    SessionProvenanceRef,
    SessionRelation,
    SessionRevisionRef,
    SessionStore,
    session_semantic_hash,
)
from mneme.core.sources.filesystem import FileSystemSource
from mneme.core.sources.registry import SourceBinding, SourceBindingStore
from mneme.core.validation.vault import validate_identifier


@dataclass(frozen=True, slots=True)
class RecallResult:
    hits: tuple[RecallHit, ...]
    status: str
    diagnostics: tuple[str, ...] = ()


class CoreService:
    """Coordinates explicit Core commands with canonical stores and live gates."""

    def __init__(self, vault: object, index: GeneratedIndex | None = None):
        self.vault = vault
        self.index = index or GeneratedIndex(vault.local_root / "index" / "state.db")
        self.authorizer = PolicyAuthorizer(vault)

    def observe(self, event: LifecycleEvent) -> ObservationResult:
        """Return host guidance without inferring or persisting a command."""
        if not isinstance(event, LifecycleEvent):
            raise InvalidArtifact("observe requires a LifecycleEvent")
        return ObservationResult(
            CONTRACT_VERSION,
            event.name,
            checkpoint_required=event.name
            in {
                "pre_compact",
                "milestone_reached",
                "session_ended",
                "agent_switched",
            },
            context_required=event.name in {"session_started", "session_resumed"},
        )

    def execute(self, command: CoreCommand) -> CommandResult:
        """Execute one validated command and return a closed portable-safe envelope."""
        if not isinstance(command, CoreCommand):
            return CommandResult.failure(
                "unknown", InvalidArtifact("execute requires a CoreCommand")
            )
        try:
            handler = getattr(self, f"_execute_{command.name}")
            return handler(command.payload)
        except (KeyError, TypeError, ValueError) as exc:
            return CommandResult.failure(
                command.name, InvalidArtifact("command payload is invalid")
            )
        except Exception as exc:
            return CommandResult.failure(command.name, exc)

    def _execute_get_context(self, value: Mapping[str, Any]) -> CommandResult:
        payload = _payload(
            value,
            required={"workstream_id"},
            optional={"mode"},
            label="get_context",
        )
        mode = payload.get("mode", "portable")
        if not isinstance(mode, str):
            raise InvalidArtifact("context mode must be text")
        view = self.context(_text(payload["workstream_id"], "workstream id"), mode)
        result = {
            "effective_status": view.effective_status.value,
            "mode": view.mode,
            "overlay_status": (
                None if view.overlay_status is None else view.overlay_status.value
            ),
            "portable_status": view.portable_status.value,
            "text": view.text,
            "workstream_id": view.workstream_id,
        }
        return CommandResult.success(
            "get_context", result, status=view.effective_status.value
        )

    def _execute_open_session(self, value: Mapping[str, Any]) -> CommandResult:
        payload = _payload(
            value,
            required={"session_id", "workstream_id"},
            optional={"storage_class"},
            label="open_session",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        workstream_id = _text(payload["workstream_id"], "workstream id")
        session_id = _text(payload["session_id"], "session id")
        validate_identifier(session_id, label="session id")
        if not self.authorizer.authorize_current(
            PolicyArtifactRef.workstream(workstream_id, storage_class), "context"
        ).allowed:
            raise PolicyDenied("workstream is unavailable under current policy")
        registry = RegistryStore(self.vault, storage_class).load_workstream(
            workstream_id
        )
        matches = tuple(
            head for head in registry.active_heads if head.session == session_id
        )
        active_head = None if not matches else _head_dict(matches[0])
        return CommandResult.success(
            "open_session",
            {
                "active_head": active_head,
                "registry_generation": registry.generation,
                "session_id": session_id,
                "storage_class": storage_class.value,
                "workstream_id": workstream_id,
            },
        )

    def _execute_create_session_revision(
        self, value: Mapping[str, Any]
    ) -> CommandResult:
        payload = _payload(
            value,
            required={
                "workstream_id",
                "session_id",
                "storage_class",
                "expected_parent",
                "expected_registry_generation",
                "body",
                "relations",
            },
            label="create_session_revision",
        )
        storage_class = _storage_class(payload["storage_class"])
        body = _session_body(payload["body"])
        relations = _session_relations(payload["relations"])
        expected_parent = _session_parent(payload["expected_parent"])
        workstream_id = _text(payload["workstream_id"], "workstream id")
        session_id = _text(payload["session_id"], "session id")
        generation = _generation(
            payload["expected_registry_generation"], "workstream generation"
        )
        semantic_hash = session_semantic_hash(body, relations)
        receipt = evaluate_portability(
            _requested_portability(storage_class),
            self._checkpoint_policy_rules(
                storage_class, workstream_id, body, relations
            ),
            semantic_hash,
        )
        if not receipt.allowed:
            raise PolicyDenied("checkpoint portability exceeds current policy")
        request = CheckpointRequest(
            workstream_id,
            session_id,
            storage_class,
            expected_parent,
            generation,
            body,
            relations,
            receipt,
        )
        reference = SessionStore(self.vault, storage_class).create_revision(request)
        result = {
            "revision": reference.revision,
            "session_id": reference.session,
            "storage_class": storage_class.value,
            "workstream_id": workstream_id,
        }
        events = [
            DomainEvent(
                CONTRACT_VERSION,
                "session_revision_created",
                result,
            )
        ]
        events.extend(
            DomainEvent(
                CONTRACT_VERSION,
                "handoff_recorded",
                {
                    "revision": reference.revision,
                    "session_id": reference.session,
                    "workstream_id": workstream_id,
                },
            )
            for relation in relations
            if relation.kind == "handoff"
        )
        return CommandResult.success(
            "create_session_revision", result, domain_events=tuple(events)
        )

    def _execute_submit_memory(self, value: Mapping[str, Any]) -> CommandResult:
        payload = _payload(
            value,
            required={"authority", "body", "kind", "portability", "scope"},
            optional={
                "expected_generation",
                "memory_id",
                "provenance",
                "rationale",
                "storage_class",
                "supersedes",
            },
            label="submit_memory",
        )
        try:
            portability = Portability(payload["portability"])
        except (TypeError, ValueError) as exc:
            raise InvalidArtifact("memory portability is invalid") from exc
        storage_class = _storage_class(
            payload.get(
                "storage_class",
                "local-only"
                if portability is Portability.LOCAL_ONLY
                else "portable",
            )
        )
        if _requested_portability(storage_class) is not portability:
            raise InvalidArtifact("memory portability does not match storage class")
        scope = MemoryScope.parse(payload["scope"])
        provenance = _artifact_references(payload.get("provenance", ()))
        body = _text(payload["body"], "memory body", trimmed=False)
        rationale = payload.get("rationale", "")
        if not isinstance(rationale, str):
            raise InvalidArtifact("memory rationale must be text")
        store = MemoryStore(self.vault, storage_class)
        is_supersession = "supersedes" in payload or "expected_generation" in payload
        if is_supersession:
            if (
                "supersedes" not in payload
                or "expected_generation" not in payload
                or "memory_id" in payload
            ):
                raise InvalidArtifact(
                    "supersession requires predecessor and expected generation"
                )
            predecessor_id = _text(payload["supersedes"], "superseded memory id")
            prior = store.read(predecessor_id)
            if payload["kind"] != prior.kind.value:
                raise InvalidArtifact("supersession cannot change memory kind")
            if "rationale" not in payload:
                rationale = prior.rationale
            if "provenance" not in payload:
                provenance = prior.provenance
            semantic_hash = memory_semantic_hash(
                kind=prior.kind,
                scope=scope,
                authority=payload["authority"],
                portability=portability,
                body=body,
                rationale=rationale,
                provenance=provenance,
                supersedes=prior.id,
            )
            receipt = evaluate_portability(
                portability,
                self._memory_policy_rules(storage_class, scope, provenance),
                semantic_hash,
            )
            if not receipt.allowed:
                raise PolicyDenied("memory portability exceeds current policy")
            record = store.supersede(
                prior.id,
                _generation(payload["expected_generation"], "memory generation"),
                body=body,
                receipt=receipt,
                rationale=rationale,
                scope=scope,
                authority=payload["authority"],
                portability=portability,
                provenance=provenance,
            )
        else:
            memory_id = payload.get("memory_id") or f"mem-{uuid4().hex}"
            memory_id = _text(memory_id, "memory id")
            semantic_hash = memory_semantic_hash(
                id=memory_id,
                kind=payload["kind"],
                scope=scope,
                authority=payload["authority"],
                portability=portability,
                body=body,
                rationale=rationale,
                provenance=provenance,
            )
            receipt = evaluate_portability(
                portability,
                self._memory_policy_rules(storage_class, scope, provenance),
                semantic_hash,
            )
            if not receipt.allowed:
                raise PolicyDenied("memory portability exceeds current policy")
            record = store.submit_candidate(
                payload["kind"],
                scope,
                payload["authority"],
                portability,
                body,
                receipt,
                rationale=rationale,
                provenance=provenance,
                memory_id=memory_id,
            )
        result = _memory_result(record, storage_class)
        return CommandResult.success(
            "submit_memory",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "memory_submitted", result),
            ),
        )

    def _execute_promote_memory(self, value: Mapping[str, Any]) -> CommandResult:
        payload = _payload(
            value,
            required={"expected_generation", "memory_id"},
            optional={"storage_class"},
            label="promote_memory",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        store = MemoryStore(self.vault, storage_class)
        record = store.read(_text(payload["memory_id"], "memory id"))
        receipt = evaluate_portability(
            record.portability,
            self._memory_policy_rules(
                storage_class, record.scope, record.provenance
            ),
            record.semantic_hash,
        )
        if not receipt.allowed:
            raise PolicyDenied("memory promotion exceeds current policy")
        promoted = store.promote(
            record.id,
            _generation(payload["expected_generation"], "memory generation"),
            receipt,
        )
        result = _memory_result(promoted, storage_class)
        return CommandResult.success(
            "promote_memory",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "memory_accepted", result),
            ),
        )

    def _execute_retire_memory(self, value: Mapping[str, Any]) -> CommandResult:
        payload = _payload(
            value,
            required={"expected_generation", "memory_id", "reason"},
            optional={"privacy_remediation", "storage_class"},
            label="retire_memory",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        privacy_remediation = payload.get("privacy_remediation", False)
        if not isinstance(privacy_remediation, bool):
            raise InvalidArtifact("privacy_remediation must be a boolean")
        retired = MemoryStore(self.vault, storage_class).retire(
            _text(payload["memory_id"], "memory id"),
            _generation(payload["expected_generation"], "memory generation"),
            _text(payload["reason"], "retirement reason"),
            privacy_remediation=privacy_remediation,
        )
        result = _memory_result(retired, storage_class)
        return CommandResult.success(
            "retire_memory",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "memory_retired", result),
            ),
        )

    def _execute_set_active_heads(self, value: Mapping[str, Any]) -> CommandResult:
        payload = _payload(
            value,
            required={"active_heads", "expected_generation", "workstream_id"},
            optional={"storage_class"},
            label="set_active_heads",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        workstream_id = _text(payload["workstream_id"], "workstream id")
        if not self.authorizer.authorize_current(
            PolicyArtifactRef.workstream(workstream_id, storage_class), "context"
        ).allowed:
            raise PolicyDenied("workstream is unavailable under current policy")
        heads = _heads(payload["active_heads"])
        store = RegistryStore(self.vault, storage_class)
        current = store.load_workstream(workstream_id)
        sessions = SessionStore(self.vault, storage_class)
        for head in heads:
            sessions.read_revision(
                SessionRevisionRef(head.session, head.revision),
                workstream_id=workstream_id,
            )
            if not self.authorizer.authorize_current(
                PolicyArtifactRef.session(
                    workstream_id,
                    head.session,
                    head.revision,
                    storage_class,
                ),
                "context",
            ).allowed:
                raise PolicyDenied("session head is unavailable under current policy")
        updated = store.update_workstream(
            replace(current, active_heads=heads),
            expected_generation=_generation(
                payload["expected_generation"], "workstream generation"
            ),
        )
        result = _workstream_head_result(updated, storage_class)
        return CommandResult.success(
            "set_active_heads",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "active_heads_changed", result),
            ),
        )

    def _execute_set_preferred_head(
        self, value: Mapping[str, Any]
    ) -> CommandResult:
        payload = _payload(
            value,
            required={"expected_generation", "preferred_head", "workstream_id"},
            optional={"storage_class"},
            label="set_preferred_head",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        workstream_id = _text(payload["workstream_id"], "workstream id")
        head = _head(payload["preferred_head"])
        store = RegistryStore(self.vault, storage_class)
        current = store.load_workstream(workstream_id)
        if head not in current.active_heads:
            raise InvalidArtifact("preferred head must be active")
        SessionStore(self.vault, storage_class).read_revision(
            SessionRevisionRef(head.session, head.revision),
            workstream_id=workstream_id,
        )
        if not self.authorizer.authorize_current(
            PolicyArtifactRef.session(
                workstream_id,
                head.session,
                head.revision,
                storage_class,
            ),
            "context",
        ).allowed:
            raise PolicyDenied("preferred head is unavailable under current policy")
        updated = store.update_workstream(
            replace(current, mode="preferred", preferred_head=head),
            expected_generation=_generation(
                payload["expected_generation"], "workstream generation"
            ),
        )
        result = _workstream_head_result(updated, storage_class)
        return CommandResult.success(
            "set_preferred_head",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "preferred_head_changed", result),
            ),
        )

    def _execute_register_source(self, value: Mapping[str, Any]) -> CommandResult:
        payload = _payload(
            value,
            required={"source_id"},
            optional={
                "authority",
                "kind",
                "locator",
                "project",
                "storage_class",
            },
            label="register_source",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        source = RegistryStore(self.vault, storage_class).register_source(
            _text(payload["source_id"], "source id"),
            kind=payload.get("kind", "filesystem"),
            authority=payload.get("authority", "project"),
            project=payload.get("project"),
            locator=payload.get("locator"),
        )
        result = {
            "authority": source.authority,
            "generation": source.generation,
            "id": source.id,
            "kind": source.kind,
            "project": source.project,
            "storage_class": storage_class.value,
        }
        return CommandResult.success(
            "register_source",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "source_registered", result),
            ),
        )

    def _execute_create_policy_revision(
        self, value: Mapping[str, Any]
    ) -> CommandResult:
        payload = _payload(
            value,
            required={"policy_id", "revision", "rule", "storage_class"},
            label="create_policy_revision",
        )
        rule = payload["rule"]
        if not isinstance(rule, Mapping):
            raise InvalidArtifact("policy rule must be a mapping")
        rule = _json_copy(rule)
        storage_class = _storage_class(payload["storage_class"])
        reference = PolicyStore(self.vault).create_revision(
            _text(payload["policy_id"], "policy id"),
            _text(payload["revision"], "policy revision"),
            rule,
            storage_class,
        )
        policy_ref = _policy_ref_dict(reference)
        result = {
            "policy_ref": policy_ref,
            "storage_class": storage_class.value,
        }
        return CommandResult.success(
            "create_policy_revision",
            result,
            domain_events=(
                DomainEvent(
                    CONTRACT_VERSION,
                    "policy_revision_created",
                    {
                        "policy_id": reference.policy_id,
                        "revision": reference.revision,
                        "storage_class": storage_class.value,
                    },
                ),
            ),
        )

    def _execute_activate_policy_revision(
        self, value: Mapping[str, Any]
    ) -> CommandResult:
        payload = _payload(
            value,
            required={"expected_generation", "policy_ref"},
            label="activate_policy_revision",
        )
        reference = _policy_ref(payload["policy_ref"])
        index = PolicyStore(self.vault).activate(
            reference,
            _generation(payload["expected_generation"], "policy index generation"),
        )
        result = {
            "generation": index.generation,
            "policy_id": reference.policy_id,
            "revision": reference.revision,
            "storage_class": index.storage_class.value,
        }
        return CommandResult.success(
            "activate_policy_revision",
            result,
            domain_events=(
                DomainEvent(
                    CONTRACT_VERSION, "policy_revision_activated", result
                ),
            ),
        )

    def _execute_assign_project_policy(
        self, value: Mapping[str, Any]
    ) -> CommandResult:
        payload = _payload(
            value,
            required={"expected_generation", "policy_ref", "project_id"},
            optional={"storage_class"},
            label="assign_project_policy",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        updated = RegistryStore(self.vault, storage_class).assign_project_policy(
            _text(payload["project_id"], "project id"),
            _policy_ref(payload["policy_ref"]),
            expected_generation=_generation(
                payload["expected_generation"], "project generation"
            ),
        )
        result = {
            "generation": updated.generation,
            "project_id": updated.id,
            "storage_class": storage_class.value,
        }
        return CommandResult.success(
            "assign_project_policy",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "project_policy_assigned", result),
            ),
        )

    def _execute_assign_source_policy(
        self, value: Mapping[str, Any]
    ) -> CommandResult:
        payload = _payload(
            value,
            required={"expected_generation", "policy_ref", "source_id"},
            optional={"storage_class"},
            label="assign_source_policy",
        )
        storage_class = _storage_class(payload.get("storage_class", "portable"))
        updated = RegistryStore(self.vault, storage_class).assign_source_policy(
            _text(payload["source_id"], "source id"),
            _policy_ref(payload["policy_ref"]),
            expected_generation=_generation(
                payload["expected_generation"], "source generation"
            ),
        )
        result = {
            "generation": updated.generation,
            "source_id": updated.id,
            "storage_class": storage_class.value,
        }
        return CommandResult.success(
            "assign_source_policy",
            result,
            domain_events=(
                DomainEvent(CONTRACT_VERSION, "source_policy_assigned", result),
            ),
        )

    def _memory_policy_rules(
        self,
        storage_class: StorageClass,
        scope: MemoryScope,
        provenance: tuple[ArtifactReference, ...],
    ) -> tuple[object, ...]:
        policies = PolicyStore(self.vault)
        registries = RegistryStore(self.vault, storage_class)
        refs = [policies.load_active("vault-default", StorageClass.PORTABLE)]
        project_ids: set[tuple[StorageClass, str]] = set()
        if scope.project_id is not None:
            project_ids.add((storage_class, scope.project_id))
        if scope.workstream_id is not None:
            workstream = _load_registry(
                self.vault,
                registries,
                storage_class,
                "workstream",
                scope.workstream_id,
            )
            refs.extend(workstream.policy_refs)
            if workstream.project is not None:
                project_ids.add((storage_class, workstream.project))
        for reference in provenance:
            if reference.kind is not ReferenceKind.ID or not isinstance(
                reference.value, str
            ):
                continue
            source_storage_class = reference.storage_class
            source = _load_registry(
                self.vault,
                RegistryStore(self.vault, source_storage_class),
                source_storage_class,
                "source",
                reference.value,
            )
            if source.policy_ref is not None:
                refs.append(source.policy_ref)
            if source.project is not None:
                project_ids.add((source_storage_class, source.project))
        for project_storage_class, project_id in sorted(project_ids):
            project = _load_registry(
                self.vault,
                RegistryStore(self.vault, project_storage_class),
                project_storage_class,
                "project",
                project_id,
            )
            if project.policy_ref is not None:
                refs.append(project.policy_ref)
        return tuple(policies.load_rule(reference) for reference in refs)

    def _checkpoint_policy_rules(
        self,
        storage_class: StorageClass,
        workstream_id: str,
        body: SessionBody,
        relations: tuple[SessionRelation, ...],
    ) -> tuple[object, ...]:
        registries = RegistryStore(self.vault, storage_class)
        workstream = _load_registry(
            self.vault,
            registries,
            storage_class,
            "workstream",
            workstream_id,
        )
        policies = PolicyStore(self.vault)
        refs = [policies.load_active("vault-default", StorageClass.PORTABLE)]
        refs.extend(workstream.policy_refs)
        project_ids = (
            {workstream.project} if workstream.project is not None else set()
        )
        source_ids = [
            reference.value
            for reference in body.source_refs
            if reference.kind is ReferenceKind.ID
            and isinstance(reference.value, str)
        ]
        source_ids.extend(
            reference.id
            for relation in relations
            for reference in relation.provenance_refs
            if isinstance(reference, SessionProvenanceRef)
            and reference.kind == "source"
        )
        for source_id in dict.fromkeys(source_ids):
            source = _load_registry(
                self.vault,
                registries,
                storage_class,
                "source",
                source_id,
            )
            if source.policy_ref is not None:
                refs.append(source.policy_ref)
            if source.project is not None:
                project_ids.add(source.project)
        for project_id in sorted(project_ids):
            project = _load_registry(
                self.vault,
                registries,
                storage_class,
                "project",
                project_id,
            )
            if project.policy_ref is not None:
                refs.append(project.policy_ref)
        return tuple(policies.load_rule(reference) for reference in refs)

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
        with policy_admission_gate(self.vault):
            view_decision = self.authorizer.authorize_context_view(
                workstream_id, mode=mode
            )
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
                authorize_revision=lambda selected, head, storage_class: self.authorizer.authorize_current(
                    PolicyArtifactRef.session(
                        selected, head.session, head.revision, storage_class
                    ),
                    "context",
                ).allowed,
            )
            view = render_current(readers, workstream_id, mode)
            return self._cache_view("CURRENT.md", view, view_decision)

    def profile(
        self, scope: object, *, storage_class: StorageClass = StorageClass.PORTABLE
    ) -> ContextView:
        """Render PROFILE from the matching storage tree under the live gate."""
        from mneme.core.memories import MemoryReaders, render_profile

        if not isinstance(storage_class, StorageClass):
            raise TypeError("PROFILE requires an explicit StorageClass")
        with policy_admission_gate(self.vault):
            view_decision = self.authorizer.authorize_profile_view(scope, storage_class)
            view = render_profile(
                MemoryReaders(
                    MemoryStore(self.vault, storage_class),
                    authorize_record=lambda record: self.authorizer.authorize_current(
                        PolicyArtifactRef.memory(record.id, storage_class), "profile"
                    ).allowed,
                ),
                scope,
                storage_class=storage_class,
            )
            return self._cache_view("PROFILE.md", view, view_decision)

    def authorize_export(self, artifact_ref: PolicyArtifactRef) -> PolicyDecision:
        """Expose the same live gate to any explicit export adapter."""
        return self.authorizer.authorize_current(artifact_ref, "export")

    def _cache_view(
        self, name: str, view: ContextView, decision: PolicyDecision
    ) -> ContextView:
        """Reuse only a common-gate-authorized local projection, otherwise rerender."""
        if not decision.allowed:
            return view
        try:
            cached = self.vault.views.load(name, authorization=decision)
            if cached is None:
                self.vault.views.write(name, view.text, authorization=decision)
                cached = self.vault.views.load(name, authorization=decision)
                if cached is None:
                    return view
            return replace(view, text=cached)
        except OSError:
            return view

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
        if hit.category == "source":
            return self._authorize_source(hit)
        if hit.category == "memory":
            memories = MemoryStore(self.vault)
            record = memories.read(hit.artifact_id)
            return (
                self.authorizer.authorize_current(
                    PolicyArtifactRef.memory(record.id), "recall"
                ).allowed
                and
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
                self.authorizer.authorize_current(
                    PolicyArtifactRef.session(
                        request.workstream_id, hit.artifact_id, hit.revision or ""
                    ),
                    "recall",
                ).allowed
                and
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
        artifact_ref = {
            "source-registry": PolicyArtifactRef.source,
            "project-registry": PolicyArtifactRef.project,
            "workstream-registry": PolicyArtifactRef.workstream,
        }.get(hit.category)
        if artifact_ref is None or not self.authorizer.authorize_current(
            artifact_ref(hit.artifact_id), "recall"
        ).allowed:
            return False
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
        if not self.authorizer.authorize_current(
            PolicyArtifactRef.source(source.id), "recall"
        ).allowed:
            return False
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


def _payload(
    value: object,
    *,
    required: set[str],
    optional: set[str] | None = None,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidArtifact(f"{label} payload must be a JSON object")
    allowed = required | (optional or set())
    if not required.issubset(value) or not set(value).issubset(allowed):
        raise InvalidArtifact(f"{label} payload has an invalid schema")
    return value


def _text(value: object, label: str, *, trimmed: bool = True) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidArtifact(f"{label} must be non-empty text")
    if trimmed and value != value.strip():
        raise InvalidArtifact(f"{label} must not have surrounding whitespace")
    return value


def _generation(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidArtifact(f"{label} must be a non-negative integer")
    return value


def _storage_class(value: object) -> StorageClass:
    try:
        return StorageClass(value)
    except (TypeError, ValueError) as exc:
        raise InvalidArtifact("storage_class is invalid") from exc


def _requested_portability(storage_class: StorageClass) -> Portability:
    return (
        Portability.LOCAL_ONLY
        if storage_class is StorageClass.LOCAL_ONLY
        else Portability.PERSONAL_VAULT
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise InvalidArtifact(f"{label} must be a JSON object")
    return value


def _json_copy(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _json_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_copy(item) for item in value]
    return value


def _exact_mapping(
    value: object, keys: set[str], label: str
) -> Mapping[str, Any]:
    mapping = _mapping(value, label)
    if set(mapping) != keys:
        raise InvalidArtifact(f"{label} has an invalid schema")
    return mapping


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise InvalidArtifact(f"{label} must be a list of text values")
    return tuple(value)


def _session_body(value: object) -> SessionBody:
    mapping = _exact_mapping(
        value,
        {
            "adapter_id",
            "objective",
            "current_state",
            "verified_facts",
            "completed_work",
            "blockers",
            "next_actions",
            "source_refs",
        },
        "checkpoint body",
    )
    return SessionBody(
        mapping["adapter_id"],
        mapping["objective"],
        mapping["current_state"],
        _text_tuple(mapping["verified_facts"], "verified_facts"),
        _text_tuple(mapping["completed_work"], "completed_work"),
        _text_tuple(mapping["blockers"], "blockers"),
        _text_tuple(mapping["next_actions"], "next_actions"),
        _artifact_references(mapping["source_refs"]),
    )


def _session_relations(value: object) -> tuple[SessionRelation, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidArtifact("checkpoint relations must be a list")
    relations: list[SessionRelation] = []
    for item in value:
        mapping = _exact_mapping(
            item,
            {
                "kind",
                "target",
                "purpose",
                "required_context",
                "next_action",
                "provenance_refs",
            },
            "checkpoint relation",
        )
        relations.append(
            SessionRelation(
                mapping["kind"],
                mapping["target"],
                mapping["purpose"],
                mapping["required_context"],
                mapping["next_action"],
                _provenance_references(mapping["provenance_refs"]),
            )
        )
    return tuple(relations)


def _artifact_references(value: object) -> tuple[ArtifactReference, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidArtifact("artifact references must be a list")
    references: list[ArtifactReference] = []
    for item in value:
        mapping = _exact_mapping(
            item,
            {"kind", "storage_class", "value"},
            "artifact reference",
        )
        try:
            references.append(
                ArtifactReference(
                    ReferenceKind(mapping["kind"]),
                    StorageClass(mapping["storage_class"]),
                    mapping["value"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise InvalidArtifact("artifact reference is invalid") from exc
    return tuple(references)


def _provenance_references(
    value: object,
) -> tuple[ArtifactReference | SessionProvenanceRef, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidArtifact("relation provenance_refs must be a list")
    references: list[ArtifactReference | SessionProvenanceRef] = []
    for item in value:
        mapping = _mapping(item, "relation provenance reference")
        if mapping.get("type") == "typed":
            mapping = _exact_mapping(
                mapping,
                {"type", "kind", "storage_class", "id"},
                "relation provenance reference",
            )
            try:
                references.append(
                    SessionProvenanceRef(
                        mapping["kind"],
                        StorageClass(mapping["storage_class"]),
                        mapping["id"],
                    )
                )
            except (TypeError, ValueError) as exc:
                raise InvalidArtifact(
                    "relation provenance reference is invalid"
                ) from exc
        else:
            references.extend(_artifact_references((mapping,)))
    return tuple(references)


def _session_parent(value: object) -> SessionRevisionRef | None:
    if value is None:
        return None
    mapping = _exact_mapping(
        value, {"session", "revision"}, "expected_parent"
    )
    return SessionRevisionRef(mapping["session"], mapping["revision"])


def _policy_ref(value: object) -> PolicyRef:
    mapping = _exact_mapping(
        value, {"policy_id", "revision", "digest"}, "policy_ref"
    )
    return PolicyRef(
        mapping["policy_id"], mapping["revision"], mapping["digest"]
    )


def _policy_ref_dict(reference: PolicyRef) -> dict[str, str]:
    return {
        "digest": reference.digest,
        "policy_id": reference.policy_id,
        "revision": reference.revision,
    }


def _head(value: object) -> HeadRef:
    mapping = _exact_mapping(
        value, {"session", "revision"}, "session head"
    )
    return HeadRef(mapping["session"], mapping["revision"])


def _heads(value: object) -> tuple[HeadRef, ...]:
    if not isinstance(value, (list, tuple)):
        raise InvalidArtifact("active_heads must be a list")
    return tuple(_head(item) for item in value)


def _head_dict(head: HeadRef) -> dict[str, str]:
    return {"revision": head.revision, "session": head.session}


def _memory_result(
    record: MemoryRecord, storage_class: StorageClass
) -> dict[str, object]:
    return {
        "authority": record.authority.value,
        "generation": record.generation,
        "id": record.id,
        "kind": record.kind.value,
        "portability": record.portability.value,
        "scope": record.scope.as_dict(),
        "status": record.status.value,
        "storage_class": storage_class.value,
    }


def _workstream_head_result(
    registry: object, storage_class: StorageClass
) -> dict[str, object]:
    return {
        "active_heads": [_head_dict(head) for head in registry.active_heads],
        "generation": registry.generation,
        "mode": registry.mode,
        "preferred_head": (
            None
            if registry.preferred_head is None
            else _head_dict(registry.preferred_head)
        ),
        "storage_class": storage_class.value,
        "workstream_id": registry.id,
    }


def _load_registry(
    vault: object,
    store: RegistryStore,
    storage_class: StorageClass,
    kind: str,
    identifier: str,
) -> object:
    loaders = {
        "project": store.load_project,
        "source": store.load_source,
        "workstream": store.load_workstream,
    }
    loader = loaders[kind]
    try:
        return loader(identifier)
    except InvalidArtifact as local_error:
        if storage_class is not StorageClass.LOCAL_ONLY or kind == "workstream":
            raise
        portable = RegistryStore(vault, StorageClass.PORTABLE)
        portable_loader = {
            "project": portable.load_project,
            "source": portable.load_source,
        }[kind]
        try:
            return portable_loader(identifier)
        except InvalidArtifact:
            raise local_error
