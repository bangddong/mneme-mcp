"""Deterministic Vault diagnostics with a deliberately narrow repair surface.

Doctor is an operational observer, not a semantic conflict resolver.  It uses
the same validating stores as recall and context, reports only logical artifact
references, and never selects heads, rewrites portable state, or interprets
content.  The optional repair pass is limited to the disposable local index and
old machine-local lock files.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import time
from typing import Iterable

from mneme.core.artifacts import StorageClass
from mneme.core.errors import InvalidArtifact, PortabilityViolation
from mneme.core.memories import MemoryStore
from mneme.core.policy import PolicyRef, PolicyStore, reevaluate_portability
from mneme.core.registries import HeadRef, RegistryStore, WorkstreamRegistry
from mneme.core.search.index import GeneratedIndex, validate_generated_index_target
from mneme.core.service import CoreService
from mneme.core.sessions import SessionRevisionRef, SessionStore
from mneme.core.sources.filesystem import FileSystemSource
from mneme.core.sources.registry import SourceBindingStore
from mneme.core.validation.vault import validate_identifier
from mneme.core.vault import Vault


_STALE_LOCK_AGE_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class DoctorIssue:
    """One portable-safe, deterministic operational diagnosis.

    ``artifact`` is a logical artifact reference rather than a filesystem path.
    ``detail`` is intentionally fixed per issue code: neither field is allowed
    to expose a local source binding, a local-only identifier, or confidential
    existence to a portable-facing caller.
    """

    code: str
    severity: str
    artifact: str
    detail: str

    def __post_init__(self) -> None:
        if self.severity not in {"degraded", "invalid"}:
            raise ValueError("doctor issue severity must be degraded or invalid")


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Stable diagnosis and the local-only repairs actually performed."""

    status: str
    issues: tuple[DoctorIssue, ...]
    repairs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"resolved", "degraded", "invalid"}:
            raise ValueError("doctor report status is invalid")


class Doctor:
    """Inspect a Vault deterministically and optionally repair generated state.

    ``repair`` is explicit at call time.  A repair run is skipped when canonical
    state is invalid, so generated output cannot disguise a required semantic
    decision.  It never writes Registry, Session, Memory, or Policy artifacts.
    """

    def __init__(
        self,
        vault: object,
        index: GeneratedIndex | None = None,
        *,
        stale_lock_age_seconds: int = _STALE_LOCK_AGE_SECONDS,
    ) -> None:
        if (
            not hasattr(vault, "root")
            or not hasattr(vault, "local_root")
            or not hasattr(vault, "artifacts")
            or not isinstance(stale_lock_age_seconds, int)
            or isinstance(stale_lock_age_seconds, bool)
            or stale_lock_age_seconds <= 0
        ):
            raise TypeError("Doctor requires a Vault and a positive lock age")
        self._vault = vault
        expected = vault.local_root / "index" / "state.db"
        self._unsafe_supplied_index = index is not None and index.db_path != expected
        self._index = GeneratedIndex(expected) if self._unsafe_supplied_index else (index or GeneratedIndex(expected))
        self._stale_lock_age_seconds = stale_lock_age_seconds

    def run(self, *, repair: bool = False) -> DoctorReport:
        """Diagnose canonical and optional state, optionally repairing local output."""
        if not isinstance(repair, bool):
            raise TypeError("repair must be a boolean")
        issues = self._diagnose()
        repairs: list[str] = []
        if repair:
            repairs.extend(self._remove_stale_locks())
            if (
                not any(
                    issue.severity == "invalid"
                    and issue.code not in {"generated-index-invalid"}
                    for issue in issues
                )
                and any(
                    issue.code in {"generated-index-rebuildable", "generated-index-invalid"}
                    for issue in issues
                )
            ):
                try:
                    CoreService(self._vault, self._index).reindex()
                except Exception:
                    # A generated-index failure remains inspectable on the fresh pass.
                    pass
                else:
                    repairs.append("generated-index-rebuilt")
            issues = self._diagnose()
        return DoctorReport(_status(issues), tuple(issues), tuple(sorted(set(repairs))))

    def _diagnose(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []
        try:
            Vault.open(self._vault.root, self._vault.state_home)
        except Exception:
            issues.append(_issue("canonical-vault-invalid", "invalid", "vault"))
        if self._unsafe_supplied_index:
            issues.append(_issue("generated-index-unsafe", "invalid", "generated:index"))
        try:
            validate_generated_index_target(self._vault, self._index.db_path)
        except Exception:
            issues.append(_issue("generated-index-unsafe", "invalid", "generated:index"))
        policies = PolicyStore(self._vault)
        try:
            policies.load_active("vault-default", StorageClass.PORTABLE)
        except Exception:
            issues.append(_issue("canonical-policy-invalid", "invalid", "policy:vault-default"))
        self._scan_policy_revisions(policies, issues)

        registries = RegistryStore(self._vault)
        self._scan_projects(registries, issues)
        self._scan_sources(registries, issues)
        self._scan_workstreams(registries, policies, issues)
        self._scan_memories(policies, issues)
        self._scan_generated_index(issues)
        self._scan_stale_locks(issues)
        return sorted(set(issues), key=lambda item: (item.code, item.artifact, item.severity))

    def _scan_policy_revisions(
        self, policies: PolicyStore, issues: list[DoctorIssue]
    ) -> None:
        root = self._vault.root / ".madi" / "policies"
        for policy_directory in _directories(root):
            for path in _files(policy_directory, ".yaml"):
                artifact = f"policy:{_safe_id(policy_directory.name)}:{_safe_id(path.stem)}"
                try:
                    reference = PolicyRef(
                        policy_directory.name,
                        path.stem,
                        sha256(path.read_bytes()).hexdigest(),
                    )
                    policies.load_rule(reference)
                except Exception:
                    issues.append(_issue("canonical-policy-invalid", "invalid", artifact))

    def _scan_projects(self, registries: RegistryStore, issues: list[DoctorIssue]) -> None:
        for path in _files(self._vault.root / "projects", ".yaml"):
            artifact = f"project:{_safe_id(path.stem)}"
            try:
                registries.load_project(path.stem)
            except Exception:
                issues.append(_issue("canonical-artifact-invalid", "invalid", artifact))

    def _scan_sources(self, registries: RegistryStore, issues: list[DoctorIssue]) -> None:
        bindings = SourceBindingStore(self._vault)
        for path in _files(self._vault.root / "sources", ".yaml"):
            source_id = path.stem
            artifact = f"source:{_safe_id(source_id)}"
            try:
                source = registries.load_source(source_id)
            except Exception:
                issues.append(_issue("canonical-artifact-invalid", "invalid", artifact))
                continue
            try:
                binding = bindings.load(source.id)
            except Exception:
                binding = None
            if binding is None or not binding.path.is_dir():
                issues.append(_issue("source-unavailable", "degraded", artifact))
                continue
            try:
                mounted = FileSystemSource(binding.path)
                for relative_path in mounted.list_markdown():
                    if mounted.read(relative_path) is None:
                        issues.append(_issue("source-markdown-unreadable", "degraded", artifact))
                        break
            except Exception:
                issues.append(_issue("source-unavailable", "degraded", artifact))

    def _scan_workstreams(
        self,
        registries: RegistryStore,
        policies: PolicyStore,
        issues: list[DoctorIssue],
    ) -> None:
        root = self._vault.root / "workstreams"
        for directory in _directories(root):
            workstream_id = directory.name
            artifact = f"workstream:{_safe_id(workstream_id)}"
            try:
                registry = registries.load_workstream(workstream_id)
            except Exception:
                issues.append(_issue("canonical-artifact-invalid", "invalid", artifact))
                self._scan_session_files(workstream_id, (), None, policies, issues)
                continue
            self._scan_workstream_policy_refs(registry.policy_refs, policies, artifact, issues)
            self._scan_session_files(
                workstream_id, registry.active_heads, registry, policies, issues
            )

    def _scan_workstream_policy_refs(
        self,
        refs: Iterable[PolicyRef],
        policies: PolicyStore,
        artifact: str,
        issues: list[DoctorIssue],
    ) -> None:
        for ref in refs:
            try:
                policies.load_rule(ref)
            except Exception:
                issues.append(_issue("canonical-policy-invalid", "invalid", artifact))

    def _scan_session_files(
        self,
        workstream_id: str,
        active_heads: tuple[HeadRef, ...],
        registry: WorkstreamRegistry | None,
        policies: PolicyStore,
        issues: list[DoctorIssue],
    ) -> None:
        sessions_root = self._vault.root / "workstreams" / workstream_id / "sessions"
        valid: set[HeadRef] = set()
        sessions = SessionStore(self._vault)
        for session_directory in _directories(sessions_root):
            for path in _files(session_directory, ".md"):
                artifact = f"session:{_safe_id(workstream_id)}:{_safe_id(session_directory.name)}:{_safe_id(path.stem)}"
                try:
                    ref = SessionRevisionRef(session_directory.name, path.stem)
                    request = sessions.read_revision(ref, workstream_id=workstream_id)
                except PortabilityViolation:
                    issues.append(_issue("portable-local-reference", "invalid", artifact))
                    continue
                except Exception:
                    issues.append(_issue("canonical-artifact-invalid", "invalid", artifact))
                    continue
                valid.add(ref.as_head())
                self._scan_receipt(request.policy_evaluation, policies, artifact, issues)
                if registry is not None:
                    try:
                        rules = sessions._applicable_policy_rules(request, registry)
                        self._scan_reevaluation(
                            request.policy_evaluation, rules, artifact, issues
                        )
                    except Exception:
                        issues.append(_issue("policy-reevaluation-failed", "invalid", artifact))
        for head in active_heads:
            if head not in valid:
                issues.append(_issue("missing-active-head", "invalid", f"workstream:{_safe_id(workstream_id)}"))
        for ref in sorted(valid - set(active_heads), key=lambda item: (item.session, item.revision)):
            issues.append(
                _issue(
                    "session-orphan",
                    "degraded",
                    f"session:{_safe_id(workstream_id)}:{ref.session}:{ref.revision}",
                )
            )

    def _scan_memories(self, policies: PolicyStore, issues: list[DoctorIssue]) -> None:
        directory = self._vault.root / "memory"
        store = MemoryStore(self._vault)
        for path in _files(directory, ".md"):
            artifact = f"memory:{_safe_id(path.stem)}"
            try:
                record = store.read(path.stem)
            except PortabilityViolation:
                issues.append(_issue("portable-local-reference", "invalid", artifact))
                continue
            except Exception:
                issues.append(_issue("canonical-artifact-invalid", "invalid", artifact))
                continue
            self._scan_receipt(record.policy_receipt, policies, artifact, issues)
            try:
                rules = store._applicable_policy_rules(record)
                self._scan_reevaluation(record.policy_receipt, rules, artifact, issues)
            except Exception:
                issues.append(_issue("policy-reevaluation-failed", "invalid", artifact))

    @staticmethod
    def _scan_receipt(receipt: object, policies: PolicyStore, artifact: str, issues: list[DoctorIssue]) -> None:
        refs = getattr(receipt, "refs", ())
        for ref in refs:
            if not isinstance(ref, PolicyRef):
                continue
            try:
                current = policies.load_active(ref.policy_id, StorageClass.PORTABLE)
            except Exception:
                # A direct assignment may name an immutable revision not globally active.
                continue
            if current != ref:
                issues.append(_issue("policy-receipt-stale", "degraded", artifact))

    @staticmethod
    def _scan_reevaluation(receipt: object, rules: tuple[object, ...], artifact: str, issues: list[DoctorIssue]) -> None:
        current = reevaluate_portability(receipt, rules)
        if not current.allowed:
            issues.append(_issue("policy-reevaluation-noncompliant", "invalid", artifact))
        elif (
            current.effective_ceiling != receipt.effective_ceiling
            or current.refs != receipt.refs
        ):
            issues.append(_issue("policy-receipt-stale", "degraded", artifact))

    def _scan_generated_index(self, issues: list[DoctorIssue]) -> None:
        report = self._index.inspect_read_only()
        if not report.usable:
            issues.append(_issue("generated-index-rebuildable", "degraded", "generated:index"))
        elif report.status == "invalid":
            issues.append(_issue("generated-index-invalid", "invalid", "generated:index"))

    def _scan_stale_locks(self, issues: list[DoctorIssue]) -> None:
        for _lock in self._stale_locks():
            issues.append(_issue("stale-local-lock", "degraded", "generated:local-lock"))

    def _stale_locks(self) -> tuple[Path, ...]:
        locks = self._vault.local_root / "locks"
        try:
            if locks.is_symlink() or not locks.resolve(strict=False).is_relative_to(
                self._vault.local_root.resolve(strict=False)
            ):
                return ()
        except (OSError, RuntimeError, ValueError):
            return ()
        now = time.time()
        result: list[Path] = []
        for path in _files(self._vault.local_root / "locks", ".lock"):
            try:
                if now - path.stat().st_mtime > self._stale_lock_age_seconds:
                    result.append(path)
            except OSError:
                continue
        return tuple(result)

    def _remove_stale_locks(self) -> list[str]:
        """Lock age cannot establish liveness; automatic lock removal is disabled."""
        return []


def _issue(code: str, severity: str, artifact: str) -> DoctorIssue:
    details = {
        "canonical-artifact-invalid": "canonical artifact failed validation",
        "canonical-vault-invalid": "foundational Vault state failed validation",
        "canonical-policy-invalid": "canonical policy failed validation",
        "generated-index-rebuildable": "generated index is unavailable or invalid and can be rebuilt",
        "generated-index-invalid": "generated index records an invalid canonical snapshot",
        "generated-index-unsafe": "generated index target is outside the allowed local index location",
        "missing-active-head": "declared active head is unavailable or invalid",
        "policy-receipt-stale": "historic policy receipt differs from the current policy pointer",
        "policy-reevaluation-failed": "current policy reevaluation failed",
        "policy-reevaluation-noncompliant": "current policy reevaluation no longer permits the artifact",
        "portable-local-reference": "portable canonical artifact contains a local-only reference",
        "session-orphan": "immutable revision is not declared as an active head",
        "source-markdown-unreadable": "mounted Markdown could not be read as UTF-8",
        "source-unavailable": "optional source mount is unavailable",
        "stale-local-lock": "local lock is older than the configured safe threshold",
    }
    return DoctorIssue(code, severity, artifact, details[code])


def _status(issues: Iterable[DoctorIssue]) -> str:
    severities = {issue.severity for issue in issues}
    if "invalid" in severities:
        return "invalid"
    if "degraded" in severities:
        return "degraded"
    return "resolved"


def _directories(root: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted((item for item in root.iterdir() if item.is_dir() and not item.is_symlink()), key=lambda item: item.name))
    except OSError:
        return ()


def _files(root: Path, suffix: str) -> tuple[Path, ...]:
    try:
        return tuple(sorted((item for item in root.iterdir() if item.is_file() and not item.is_symlink() and item.suffix == suffix), key=lambda item: item.name))
    except OSError:
        return ()


def _safe_id(value: object) -> str:
    try:
        validate_identifier(value, label="artifact id")
    except Exception:
        return "invalid"
    return str(value)
