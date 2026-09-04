"""Provisional JSON command-line adapter for the Vault Core APIs."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
from mneme.core.doctor import Doctor
from mneme.core.errors import (
    ArtifactExists,
    ConcurrentWrite,
    InvalidArtifact,
    MadiError,
    PortabilityViolation,
)
from mneme.core.memories import (
    MemoryAuthority,
    MemoryKind,
    MemoryScope,
    MemoryStore,
    memory_semantic_hash,
)
from mneme.core.policy import (
    InvalidPolicy,
    PolicyStore,
    Portability,
    UnknownPolicy,
    evaluate_portability,
)
from mneme.core.registries import RegistryConflict, RegistryStore
from mneme.core.service import CoreService
from mneme.core.sessions import (
    CheckpointRequest,
    SessionBody,
    SessionProvenanceRef,
    SessionRelation,
    SessionRevisionRef,
    SessionStore,
    session_semantic_hash,
)
from mneme.core.vault import Vault


_COMMANDS = frozenset(
    {"status", "doctor", "context", "recall", "remember", "checkpoint", "project", "reindex"}
)


class _CliUsageError(MadiError):
    pass


class _PolicyDenied(MadiError):
    pass


class _JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliUsageError(message)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command in-process and return its process exit code."""
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    command = _command_hint(raw_arguments)
    try:
        arguments = _parser().parse_args(raw_arguments)
        command = arguments.command
        vault = Vault.open(arguments.vault_root, arguments.state_home)
        status, result = _dispatch(vault, arguments)
    except Exception as exc:
        _write_json(sys.stderr, _error_envelope(command, exc))
        return 2
    _write_json(
        sys.stdout,
        {"command": command, "ok": True, "result": result, "status": status},
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _JsonParser(prog="mneme-vault", description="Provisional Vault CLI")
    parser.add_argument("--vault-root", "--vault", required=True, type=Path)
    parser.add_argument("--state-home", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_JsonParser)

    commands.add_parser("status", help="summarize Vault health")

    doctor = commands.add_parser("doctor", help="diagnose Vault invariants")
    doctor.add_argument("--repair", action="store_true")

    context = commands.add_parser("context", help="render generated current context")
    context.add_argument("--workstream", required=True)
    context.add_argument(
        "--mode", choices=("portable", "effective-local"), default="portable"
    )

    recall = commands.add_parser("recall", help="search deterministic Vault recall")
    recall.add_argument("query")
    recall.add_argument("--limit", type=int, default=10)

    remember = commands.add_parser("remember", help="submit a semantic candidate")
    remember.add_argument("--kind", required=True, choices=tuple(item.value for item in MemoryKind))
    remember.add_argument(
        "--scope",
        required=True,
        choices=("personal-global", "project", "workstream"),
    )
    remember.add_argument("--scope-id")
    remember.add_argument(
        "--authority",
        required=True,
        choices=tuple(item.value for item in MemoryAuthority),
    )
    remember.add_argument(
        "--portability",
        required=True,
        choices=(Portability.LOCAL_ONLY.value, Portability.PERSONAL_VAULT.value),
    )
    body = remember.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path)
    remember.add_argument("--rationale", default="")
    remember.add_argument("--memory-id")

    checkpoint = commands.add_parser(
        "checkpoint", help="persist a host-composed session revision"
    )
    checkpoint.add_argument("--payload", required=True, type=Path)

    project = commands.add_parser("project", help="register a personal project identity")
    project.add_argument("--id", required=True)
    project.add_argument("--authority", default="project")
    project.add_argument("--locator")
    project.add_argument(
        "--storage-class",
        choices=tuple(item.value for item in StorageClass),
        default=StorageClass.PORTABLE.value,
    )

    commands.add_parser("reindex", help="rebuild disposable local recall state")
    return parser


def _dispatch(vault: Vault, arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if arguments.command == "status":
        report = Doctor(vault).run()
        counts = {
            severity: sum(issue.severity == severity for issue in report.issues)
            for severity in ("degraded", "invalid")
        }
        return report.status, {"issue_counts": counts, "vault_id": vault.id}
    if arguments.command == "doctor":
        report = Doctor(vault).run(repair=arguments.repair)
        return report.status, {
            "issues": [asdict(issue) for issue in report.issues],
            "repairs": list(report.repairs),
        }
    if arguments.command == "context":
        view = CoreService(vault).context(arguments.workstream, arguments.mode)
        return view.effective_status.value, {
            "effective_status": view.effective_status.value,
            "mode": view.mode,
            "overlay_status": (
                None if view.overlay_status is None else view.overlay_status.value
            ),
            "portable_status": view.portable_status.value,
            "text": view.text,
            "workstream_id": view.workstream_id,
        }
    if arguments.command == "recall":
        if arguments.limit <= 0:
            raise InvalidArtifact("recall limit must be a positive integer")
        result = CoreService(vault).recall(arguments.query, arguments.limit)
        return result.status, {
            "diagnostics": list(result.diagnostics),
            "hits": [asdict(hit) for hit in result.hits],
        }
    if arguments.command == "remember":
        return "resolved", _remember(vault, arguments)
    if arguments.command == "checkpoint":
        return "resolved", _checkpoint(vault, arguments.payload)
    if arguments.command == "project":
        storage_class = StorageClass(arguments.storage_class)
        project = RegistryStore(vault, storage_class).register_project(
            arguments.id,
            authority=arguments.authority,
            locator=arguments.locator,
        )
        return "resolved", {
            "authority": project.authority,
            "generation": project.generation,
            "id": project.id,
            "locator": project.locator,
            "storage_class": storage_class.value,
        }
    if arguments.command == "reindex":
        report = CoreService(vault).reindex()
        return report.status, {
            "diagnostics": list(report.diagnostics),
            "rows": report.rows,
            "source_rows": report.source_rows,
        }
    raise _CliUsageError(f"unknown command: {arguments.command}")


def _remember(vault: Vault, arguments: argparse.Namespace) -> dict[str, Any]:
    scope = _memory_scope(arguments.scope, arguments.scope_id)
    portability = Portability(arguments.portability)
    storage_class = (
        StorageClass.LOCAL_ONLY
        if portability is Portability.LOCAL_ONLY
        else StorageClass.PORTABLE
    )
    body = (
        arguments.body
        if arguments.body is not None
        else _read_utf8(arguments.body_file, "memory body")
    )
    memory_id = arguments.memory_id or f"mem-{uuid4().hex}"
    semantic_hash = memory_semantic_hash(
        id=memory_id,
        kind=arguments.kind,
        scope=scope,
        authority=arguments.authority,
        portability=portability,
        body=body,
        rationale=arguments.rationale,
    )
    receipt = evaluate_portability(
        portability,
        _memory_policy_rules(vault, storage_class, scope),
        semantic_hash,
    )
    if not receipt.allowed:
        raise _PolicyDenied("memory portability exceeds its current policy ceiling")
    record = MemoryStore(vault, storage_class).submit_candidate(
        arguments.kind,
        scope,
        arguments.authority,
        portability,
        body,
        receipt,
        rationale=arguments.rationale,
        memory_id=memory_id,
    )
    return {
        "authority": record.authority.value,
        "generation": record.generation,
        "id": record.id,
        "kind": record.kind.value,
        "portability": record.portability.value,
        "scope": record.scope.as_dict(),
        "status": record.status.value,
    }


def _checkpoint(vault: Vault, payload_path: Path) -> dict[str, Any]:
    payload = _read_json_mapping(payload_path)
    _require_keys(
        payload,
        {
            "workstream_id",
            "session_id",
            "storage_class",
            "expected_parent",
            "expected_registry_generation",
            "body",
            "relations",
        },
        "checkpoint payload",
    )
    try:
        storage_class = StorageClass(payload["storage_class"])
    except (TypeError, ValueError) as exc:
        raise InvalidArtifact("checkpoint storage_class is invalid") from exc
    body = _session_body(payload["body"])
    relations = _session_relations(payload["relations"])
    expected_parent = _session_parent(payload["expected_parent"])
    semantic_hash = session_semantic_hash(body, relations)
    receipt = evaluate_portability(
        _requested_portability(storage_class),
        _checkpoint_policy_rules(
            vault,
            storage_class,
            payload["workstream_id"],
            body,
            relations,
        ),
        semantic_hash,
    )
    if not receipt.allowed:
        raise _PolicyDenied("checkpoint portability exceeds its current policy ceiling")
    request = CheckpointRequest(
        payload["workstream_id"],
        payload["session_id"],
        storage_class,
        expected_parent,
        payload["expected_registry_generation"],
        body,
        relations,
        receipt,
    )
    reference = SessionStore(vault, storage_class).create_revision(request)
    return {
        "revision": reference.revision,
        "session_id": reference.session,
        "workstream_id": request.workstream_id,
    }


def _memory_scope(kind: str, scope_id: str | None) -> MemoryScope:
    if kind == "personal-global":
        if scope_id is not None:
            raise InvalidArtifact("personal-global scope cannot use --scope-id")
        return MemoryScope.parse({"type": kind})
    if scope_id is None:
        raise InvalidArtifact(f"{kind} scope requires --scope-id")
    key = "project_id" if kind == "project" else "workstream_id"
    return MemoryScope.parse({"type": kind, key: scope_id})


def _memory_policy_rules(
    vault: Vault, storage_class: StorageClass, scope: MemoryScope
) -> tuple[object, ...]:
    policies = PolicyStore(vault)
    registries = RegistryStore(vault, storage_class)
    refs = [policies.load_active("vault-default", StorageClass.PORTABLE)]
    project_ids: set[str] = set()
    if scope.project_id is not None:
        project_ids.add(scope.project_id)
    if scope.workstream_id is not None:
        workstream = registries.load_workstream(scope.workstream_id)
        refs.extend(workstream.policy_refs)
        if workstream.project is not None:
            project_ids.add(workstream.project)
    for project_id in sorted(project_ids):
        project = _load_registry(registries, vault, storage_class, "project", project_id)
        if project.policy_ref is not None:
            refs.append(project.policy_ref)
    return tuple(policies.load_rule(reference) for reference in refs)


def _checkpoint_policy_rules(
    vault: Vault,
    storage_class: StorageClass,
    workstream_id: object,
    body: SessionBody,
    relations: tuple[SessionRelation, ...],
) -> tuple[object, ...]:
    registries = RegistryStore(vault, storage_class)
    workstream = registries.load_workstream(workstream_id)
    policies = PolicyStore(vault)
    refs = [policies.load_active("vault-default", StorageClass.PORTABLE)]
    refs.extend(workstream.policy_refs)
    project_ids = {workstream.project} if workstream.project is not None else set()
    source_ids = [
        reference.value
        for reference in body.source_refs
        if reference.kind is ReferenceKind.ID and isinstance(reference.value, str)
    ]
    source_ids.extend(
        reference.id
        for relation in relations
        for reference in relation.provenance_refs
        if isinstance(reference, SessionProvenanceRef) and reference.kind == "source"
    )
    for source_id in dict.fromkeys(source_ids):
        source = _load_registry(registries, vault, storage_class, "source", source_id)
        if source.policy_ref is not None:
            refs.append(source.policy_ref)
        if source.project is not None:
            project_ids.add(source.project)
    for project_id in sorted(project_ids):
        project = _load_registry(registries, vault, storage_class, "project", project_id)
        if project.policy_ref is not None:
            refs.append(project.policy_ref)
    return tuple(policies.load_rule(reference) for reference in refs)


def _load_registry(
    registries: RegistryStore,
    vault: Vault,
    storage_class: StorageClass,
    kind: str,
    identifier: str,
) -> object:
    loader = registries.load_project if kind == "project" else registries.load_source
    try:
        return loader(identifier)
    except InvalidArtifact as local_error:
        if storage_class is not StorageClass.LOCAL_ONLY:
            raise
        portable = RegistryStore(vault, StorageClass.PORTABLE)
        portable_loader = portable.load_project if kind == "project" else portable.load_source
        try:
            return portable_loader(identifier)
        except InvalidArtifact:
            raise local_error


def _session_body(value: object) -> SessionBody:
    mapping = _require_mapping(value, "checkpoint body")
    _require_keys(
        mapping,
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
    if not isinstance(value, list):
        raise InvalidArtifact("checkpoint relations must be a list")
    result = []
    for item in value:
        relation = _require_mapping(item, "checkpoint relation")
        _require_keys(
            relation,
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
        result.append(
            SessionRelation(
                relation["kind"],
                relation["target"],
                relation["purpose"],
                relation["required_context"],
                relation["next_action"],
                _provenance_references(relation["provenance_refs"]),
            )
        )
    return tuple(result)


def _artifact_references(value: object) -> tuple[ArtifactReference, ...]:
    if not isinstance(value, list):
        raise InvalidArtifact("artifact references must be a list")
    references = []
    for item in value:
        reference = _require_mapping(item, "artifact reference")
        _require_keys(reference, {"kind", "storage_class", "value"}, "artifact reference")
        try:
            references.append(
                ArtifactReference(
                    ReferenceKind(reference["kind"]),
                    StorageClass(reference["storage_class"]),
                    reference["value"],
                )
            )
        except (TypeError, ValueError) as exc:
            raise InvalidArtifact("artifact reference value is invalid") from exc
    return tuple(references)


def _provenance_references(
    value: object,
) -> tuple[ArtifactReference | SessionProvenanceRef, ...]:
    if not isinstance(value, list):
        raise InvalidArtifact("relation provenance_refs must be a list")
    result = []
    for item in value:
        mapping = _require_mapping(item, "relation provenance reference")
        if mapping.get("type") == "typed":
            _require_keys(
                mapping,
                {"type", "kind", "storage_class", "id"},
                "relation provenance reference",
            )
            try:
                result.append(
                    SessionProvenanceRef(
                        mapping["kind"],
                        StorageClass(mapping["storage_class"]),
                        mapping["id"],
                    )
                )
            except (TypeError, ValueError) as exc:
                raise InvalidArtifact("relation provenance reference is invalid") from exc
        else:
            result.extend(_artifact_references([mapping]))
    return tuple(result)


def _session_parent(value: object) -> SessionRevisionRef | None:
    if value is None:
        return None
    mapping = _require_mapping(value, "expected_parent")
    _require_keys(mapping, {"session", "revision"}, "expected_parent")
    return SessionRevisionRef(mapping["session"], mapping["revision"])


def _read_json_mapping(path: Path) -> Mapping[str, Any]:
    text = _read_utf8(path, "checkpoint payload")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidArtifact("checkpoint payload is not valid JSON") from exc
    return _require_mapping(value, "checkpoint payload")


def _read_utf8(path: Path, label: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InvalidArtifact(f"{label} is unavailable or not UTF-8") from exc


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise InvalidArtifact(f"{label} must be a JSON object")
    return value


def _require_keys(value: Mapping[str, Any], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise InvalidArtifact(f"{label} has an invalid schema")


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InvalidArtifact(f"checkpoint {label} must be a list")
    return tuple(value)


def _requested_portability(storage_class: StorageClass) -> Portability:
    return (
        Portability.LOCAL_ONLY
        if storage_class is StorageClass.LOCAL_ONLY
        else Portability.PERSONAL_VAULT
    )


def _error_envelope(command: str, error: Exception) -> dict[str, Any]:
    if isinstance(error, _CliUsageError):
        code = "cli-usage"
    elif isinstance(error, _PolicyDenied):
        code = "policy-denied"
    elif isinstance(error, (UnknownPolicy, InvalidPolicy)):
        code = "policy-error"
    elif isinstance(error, RegistryConflict):
        code = error.code
    elif isinstance(error, ConcurrentWrite):
        code = "concurrent-write"
    elif isinstance(error, ArtifactExists):
        code = "artifact-conflict"
    elif isinstance(error, PortabilityViolation):
        code = "portability-violation"
    elif isinstance(error, InvalidArtifact):
        code = "invalid-artifact"
    elif isinstance(error, MadiError):
        code = "core-error"
    else:
        code = "internal-error"
    message = str(error).strip() or type(error).__name__
    return {
        "command": command,
        "error": {"code": code, "message": message},
        "ok": False,
        "status": "error",
    }


def _command_hint(arguments: Sequence[str]) -> str:
    return next((item for item in arguments if item in _COMMANDS), "unknown")


def _write_json(stream: TextIO, value: Mapping[str, Any]) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
