"""Current-policy regressions for generated Core outputs."""

from __future__ import annotations

import pytest


@pytest.fixture
def admitted_artifacts(tmp_path):
    """Create portable artifacts under source and project policies that allow them."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import (
        CheckpointRequest,
        SessionBody,
        SessionStore,
        session_semantic_hash,
    )
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    policies = PolicyStore(vault)
    permitted = policies.create_revision(
        "source-policy", "1", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    policies.activate(permitted, expected_generation=1)

    registries = RegistryStore(vault)
    project = registries.register_project("project-01")
    source = registries.register_source("source-01", project=project.id)
    registries.create_workstream("ws-01", project=project.id, mode="parallel")
    registries.assign_project_policy(project.id, permitted, expected_generation=0)
    registries.assign_source_policy(source.id, permitted, expected_generation=0)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "reference.md").write_text("mounted source material", encoding="utf-8")
    registries.bind_source(source.id, checkout)

    source_ref = ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, source.id)
    default = policies.load_active("vault-default", StorageClass.PORTABLE)
    inputs = (
        policies.load_rule(default),
        policies.load_rule(permitted),
        policies.load_rule(permitted),
    )
    body = SessionBody(
        "codex",
        "sensitive-session-body",
        "sensitive-session-state",
        (),
        (),
        (),
        (),
        (source_ref,),
    )
    session_receipt = evaluate_portability(
        Portability.PERSONAL_VAULT, inputs, session_semantic_hash(body, ())
    )
    session = SessionStore(vault).create_revision(
        CheckpointRequest(
            "ws-01",
            "session-01",
            StorageClass.PORTABLE,
            None,
            0,
            body,
            (),
            session_receipt,
        )
    )

    memory_body = "sensitive-preference-body"
    memory_scope = {"type": "project", "project_id": project.id}
    memory_receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        inputs,
        memory_semantic_hash(
            body=memory_body,
            kind="preference",
            scope=memory_scope,
            provenance=(source_ref,),
        ),
    )
    memories = MemoryStore(vault)
    memory = memories.submit_candidate(
        "preference",
        memory_scope,
        "personal",
        "personal-vault",
        memory_body,
        memory_receipt,
        provenance=(source_ref,),
    )
    memory = memories.promote(memory.id, 0, memory_receipt)
    return vault, session, memory, permitted


def _tighten(vault, permitted):
    """Advance the official policy pointer, then CAS-assign both affected registries."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore

    policies = PolicyStore(vault)
    restrictive = policies.create_revision(
        permitted.policy_id,
        "2",
        {"ceiling": "local-only"},
        StorageClass.PORTABLE,
    )
    policies.activate(restrictive, expected_generation=2)
    registries = RegistryStore(vault)
    project = registries.load_project("project-01")
    source = registries.load_source("source-01")
    registries.assign_project_policy(
        project.id, restrictive, expected_generation=project.generation
    )
    registries.assign_source_policy(
        source.id, restrictive, expected_generation=source.generation
    )
    return restrictive


def test_tightening_without_reindex_withholds_existing_session_and_memory(
    admitted_artifacts,
):
    """Catches admission receipts or FTS rows authorizing after current policy tightens."""
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile
    from mneme.core.service import CoreService

    vault, session, memory, permitted = admitted_artifacts
    service = CoreService(vault)
    service.reindex()
    assert service.recall("sensitive", 10).hits
    assert "sensitive-session-body" in service.context("ws-01").text
    assert "sensitive-preference-body" in render_profile(
        MemoryReaders(MemoryStore(vault)),
        {"type": "project", "project_id": "project-01"},
    ).text

    _tighten(vault, permitted)

    recall = service.recall("sensitive", 10)
    current = service.context("ws-01")
    profile = render_profile(
        MemoryReaders(MemoryStore(vault)),
        {"type": "project", "project_id": "project-01"},
    )

    assert recall.hits == ()
    assert "sensitive-session-body" not in current.text
    assert "sensitive-session-state" not in current.text
    assert "sensitive-preference-body" not in profile.text
    assert "source-01" not in profile.text
    assert (vault.root / "workstreams/ws-01/sessions/session-01/000001.md").is_file()
    assert (vault.root / f"memory/{memory.id}.md").is_file()
    assert "policy_receipt" in (vault.root / f"memory/{memory.id}.md").read_text(
        encoding="utf-8"
    )


def test_cached_policy_hints_neither_grant_nor_deny_live_recall(admitted_artifacts):
    """Catches FTS policy status replacing a current canonical authorization result."""
    import sqlite3

    from mneme.core.service import CoreService

    vault, _session, _memory, permitted = admitted_artifacts
    service = CoreService(vault)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            "UPDATE recall_meta SET policy_status = 'denied' "
            "WHERE category IN ('session', 'memory')"
        )

    allowed = service.recall("sensitive", 10)

    assert allowed.hits

    _tighten(vault, permitted)
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            "UPDATE recall_meta SET policy_status = 'recorded' "
            "WHERE category IN ('session', 'memory')"
        )

    denied = service.recall("sensitive", 10)

    assert denied.hits == ()


def test_view_cache_fingerprint_tracks_assignment_changes_without_index_activation(
    admitted_artifacts,
):
    """Catches a view cache checking only the global policy-index generation."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore
    from mneme.core.security import PolicyArtifactRef, PolicyAuthorizer

    vault, session, memory, permitted = admitted_artifacts
    policies = PolicyStore(vault)
    restrictive = policies.create_revision(
        permitted.policy_id,
        "2",
        {"ceiling": "local-only"},
        StorageClass.PORTABLE,
    )
    policies.activate(restrictive, expected_generation=2)
    authorizer = PolicyAuthorizer(vault)
    session_ref = PolicyArtifactRef.session("ws-01", session.session, session.revision)
    memory_ref = PolicyArtifactRef.memory(memory.id)
    current_fingerprint = authorizer.authorize_current(
        session_ref, "context"
    ).authorization_fingerprint
    profile_fingerprint = authorizer.authorize_current(
        memory_ref, "profile"
    ).authorization_fingerprint
    assert current_fingerprint is not None
    assert profile_fingerprint is not None
    vault.views.write(
        "CURRENT.md", "allowed generated current", authorization_fingerprint=current_fingerprint
    )
    vault.views.write(
        "PROFILE.md", "allowed generated profile", authorization_fingerprint=profile_fingerprint
    )

    registries = RegistryStore(vault)
    project = registries.load_project("project-01")
    source = registries.load_source("source-01")
    registries.assign_project_policy(
        project.id, restrictive, expected_generation=project.generation
    )
    registries.assign_source_policy(
        source.id, restrictive, expected_generation=source.generation
    )

    denied_current = authorizer.authorize_current(session_ref, "context")
    denied_profile = authorizer.authorize_current(memory_ref, "profile")

    assert denied_current.allowed is False
    assert denied_profile.allowed is False
    assert vault.views.load(
        "CURRENT.md", authorization_fingerprint=denied_current.authorization_fingerprint
    ) is None
    assert vault.views.load(
        "PROFILE.md", authorization_fingerprint=denied_profile.authorization_fingerprint
    ) is None


def test_export_and_sync_return_only_closed_current_policy_codes(admitted_artifacts):
    """Catches export or sync disclosing policy detail after an allowed artifact tightens."""
    from mneme.core.git_sync import sync_preflight
    from mneme.core.security import PolicyArtifactRef, PolicyAuthorizer

    vault, session, _memory, permitted = admitted_artifacts
    _tighten(vault, permitted)
    decision = PolicyAuthorizer(vault).authorize_current(
        PolicyArtifactRef.session("ws-01", session.session, session.revision),
        "export",
    )
    preflight = sync_preflight(
        vault,
        ("workstreams/ws-01/sessions/session-01/000001.md",),
    )

    assert decision.allowed is False
    assert decision.issue_codes == ("policy-current-denied",)
    assert preflight.allowed is False
    assert "policy-current-denied" in preflight.blocker_codes
    assert set(preflight.blocker_codes) <= {
        "policy-current-denied",
        "policy-current-unavailable",
        "policy-reevaluation-noncompliant",
    }
    assert "sensitive" not in " ".join(preflight.blocker_codes)


def test_direct_portable_current_without_an_authorizer_withholds_after_tightening(
    admitted_artifacts,
):
    """Catches a direct read-only CURRENT renderer treating an omitted gate as allow."""
    from mneme.core.context import ContextReaders, render_current
    from mneme.core.registries import RegistryStore
    from mneme.core.resolver import SessionRevision
    from mneme.core.sessions import SessionRevisionRef, SessionStore

    vault, _session, _memory, permitted = admitted_artifacts
    _tighten(vault, permitted)
    sessions = SessionStore(vault)
    view = render_current(
        ContextReaders(
            RegistryStore(vault).load_workstream,
            lambda workstream_id, head: SessionRevision(
                head,
                sessions.read_revision(
                    SessionRevisionRef(head.session, head.revision),
                    workstream_id=workstream_id,
                ),
            ),
        ),
        "ws-01",
        "portable",
    )

    assert view.status.value == "invalid"
    assert "sensitive-session-body" not in view.text
    assert "sensitive-session-state" not in view.text


def test_denied_decision_cannot_load_a_generated_view_after_cache_corruption(
    admitted_artifacts,
):
    """Catches a denial fingerprint becoming a capability to read stale local text."""
    from mneme.core.security import PolicyArtifactRef, PolicyAuthorizer

    vault, _session, memory, permitted = admitted_artifacts
    _tighten(vault, permitted)
    denied = PolicyAuthorizer(vault).authorize_current(
        PolicyArtifactRef.memory(memory.id), "profile"
    )
    vault.views.write(
        "PROFILE.md",
        "sensitive-preference-body",
        authorization_fingerprint=denied.authorization_fingerprint,
    )

    assert denied.allowed is False
    assert denied.authorization_fingerprint is None
    assert vault.views.load(
        "PROFILE.md", authorization_fingerprint=denied.authorization_fingerprint
    ) is None


def test_tampered_memory_receipt_cannot_change_actual_portable_authorization(
    admitted_artifacts,
):
    """Catches mutable receipt request fields downgrading a portable accepted Memory."""
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile
    from mneme.core.security import PolicyArtifactRef
    from mneme.core.service import CoreService

    vault, _session, memory, permitted = admitted_artifacts
    service = CoreService(vault)
    service.reindex()
    memory_path = vault.root / f"memory/{memory.id}.md"
    document = memory_path.read_text(encoding="utf-8")
    assert "requested: personal-vault" in document
    assert "effective_ceiling: personal-vault" in document
    memory_path.write_text(
        document.replace("requested: personal-vault", "requested: local-only").replace(
            "effective_ceiling: personal-vault", "effective_ceiling: local-only"
        ),
        encoding="utf-8",
    )
    _tighten(vault, permitted)

    assert service.recall("sensitive-preference", 10).hits == ()
    assert "sensitive-preference-body" not in render_profile(
        MemoryReaders(MemoryStore(vault)),
        {"type": "project", "project_id": "project-01"},
    ).text
    assert service.authorize_export(PolicyArtifactRef.memory(memory.id)).allowed is False


def test_service_view_cache_uses_all_parallel_current_heads(admitted_artifacts):
    """Catches CURRENT cache validation using one head's policy inputs for many heads."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.security import PolicyArtifactRef
    from mneme.core.service import CoreService
    from mneme.core.sessions import CheckpointRequest, SessionBody, SessionStore, session_semantic_hash

    vault, session, _memory, permitted = admitted_artifacts
    registries = RegistryStore(vault)
    body = SessionBody(
        "codex", "parallel-session-body", "parallel-session-state", (), (), (), (),
        (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "source-01"),),
    )
    policies = PolicyStore(vault)
    current_rules = tuple(
        policies.load_rule(reference)
        for reference in (
            policies.load_active("vault-default", StorageClass.PORTABLE),
            permitted,
            permitted,
        )
    )
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT, current_rules, session_semantic_hash(body, ())
    )
    observed = registries.load_workstream("ws-01")
    second = SessionStore(vault).create_revision(
        CheckpointRequest(
            "ws-01", "session-02", StorageClass.PORTABLE, None,
            observed.generation, body, (), receipt,
        )
    )
    service = CoreService(vault)
    single = service.authorizer.authorize_current(
        PolicyArtifactRef.session("ws-01", session.session, session.revision), "context"
    )
    aggregate = service.authorizer.authorize_context_view("ws-01")
    vault.views.write(
        "CURRENT.md", "one-head cache body", authorization_fingerprint=single.authorization_fingerprint
    )

    view = service.context("ws-01")

    assert aggregate.allowed is True
    assert aggregate.authorization_fingerprint != single.authorization_fingerprint
    assert "one-head cache body" not in view.text
    assert "sensitive-session-body" in view.text
    assert "parallel-session-body" in view.text
    assert vault.views.load(
        "CURRENT.md", authorization_fingerprint=aggregate.authorization_fingerprint
    ) == view.text


def test_service_view_cache_uses_all_rendered_profile_memories(admitted_artifacts):
    """Catches PROFILE cache validation using only one accepted preference."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.security import PolicyArtifactRef
    from mneme.core.service import CoreService

    vault, _session, memory, permitted = admitted_artifacts
    source_ref = ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "source-01")
    body = "second-sensitive-preference"
    policies = PolicyStore(vault)
    rules = tuple(
        policies.load_rule(reference)
        for reference in (
            policies.load_active("vault-default", StorageClass.PORTABLE),
            permitted,
            permitted,
        )
    )
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        rules,
        memory_semantic_hash(
            body=body,
            kind="preference",
            scope={"type": "project", "project_id": "project-01"},
            provenance=(source_ref,),
        ),
    )
    second = MemoryStore(vault).submit_candidate(
        "preference", {"type": "project", "project_id": "project-01"},
        "personal", "personal-vault", body, receipt, provenance=(source_ref,),
    )
    second = MemoryStore(vault).promote(second.id, 0, receipt)
    service = CoreService(vault)
    single = service.authorizer.authorize_current(
        PolicyArtifactRef.memory(memory.id), "profile"
    )
    aggregate = service.authorizer.authorize_profile_view(
        {"type": "project", "project_id": "project-01"}
    )
    vault.views.write(
        "PROFILE.md", "one-memory cache body", authorization_fingerprint=single.authorization_fingerprint
    )

    view = service.profile({"type": "project", "project_id": "project-01"})

    assert aggregate.allowed is True
    assert aggregate.authorization_fingerprint != single.authorization_fingerprint
    assert "one-memory cache body" not in view.text
    assert "sensitive-preference-body" in view.text
    assert "second-sensitive-preference" in view.text
    assert vault.views.load(
        "PROFILE.md", authorization_fingerprint=aggregate.authorization_fingerprint
    ) == view.text


def test_local_session_context_authorization_uses_local_session_and_portable_source(
    tmp_path,
):
    """Catches a storage-aware local CURRENT gate rejecting a valid portable source fallback."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.security import PolicyArtifactRef, PolicyAuthorizer
    from mneme.core.sessions import CheckpointRequest, SessionBody, SessionStore, session_semantic_hash
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    RegistryStore(vault).register_source("portable-source")
    local_registries = RegistryStore(vault, StorageClass.LOCAL_ONLY)
    local_registries.create_workstream("local-ws", project=None, mode="parallel")
    source_ref = ArtifactReference(
        ReferenceKind.ID, StorageClass.PORTABLE, "portable-source"
    )
    body = SessionBody("codex", "local continuity", "local state", (), (), (), (), (source_ref,))
    policies = PolicyStore(vault)
    default = policies.load_active("vault-default", StorageClass.PORTABLE)
    receipt = evaluate_portability(
        Portability.LOCAL_ONLY,
        (policies.load_rule(default),),
        session_semantic_hash(body, ()),
    )
    revision = SessionStore(vault, StorageClass.LOCAL_ONLY).create_revision(
        CheckpointRequest(
            "local-ws",
            "local-session",
            StorageClass.LOCAL_ONLY,
            None,
            0,
            body,
            (),
            receipt,
        )
    )

    decision = PolicyAuthorizer(vault).authorize_current(
        PolicyArtifactRef.session(
            "local-ws",
            revision.session,
            revision.revision,
            StorageClass.LOCAL_ONLY,
        ),
        "context",
    )

    assert decision.allowed is True


def test_profile_cache_binds_the_exact_selected_scope(admitted_artifacts):
    """Catches a global PROFILE cache body being reused for a project PROFILE."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.service import CoreService

    vault, _session, _memory, _permitted = admitted_artifacts
    body = "GLOBAL_SECRET"
    policies = PolicyStore(vault)
    default = policies.load_active("vault-default", StorageClass.PORTABLE)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (policies.load_rule(default),),
        memory_semantic_hash(body=body, kind="preference"),
    )
    records = MemoryStore(vault)
    global_record = records.submit_candidate(
        "preference", {"type": "personal-global"}, "personal", "personal-vault", body, receipt
    )
    records.promote(global_record.id, 0, receipt)
    service = CoreService(vault)

    global_view = service.profile({"type": "personal-global"})
    project_view = service.profile({"type": "project", "project_id": "project-01"})

    assert body in global_view.text
    assert "sensitive-preference-body" in project_view.text
    assert body not in project_view.text


def test_profile_cache_tracks_memory_lifecycle_promotion(admitted_artifacts):
    """Catches a candidate-to-accepted transition leaving a PROFILE cache stale."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.service import CoreService

    vault, _session, _memory, permitted = admitted_artifacts
    body = "PROMOTED_PROJECT_SECRET"
    provenance = (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, "source-01"),)
    policies = PolicyStore(vault)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        tuple(
            policies.load_rule(reference)
            for reference in (policies.load_active("vault-default", StorageClass.PORTABLE), permitted, permitted)
        ),
        memory_semantic_hash(
            body=body, kind="preference", scope={"type": "project", "project_id": "project-01"}, provenance=provenance
        ),
    )
    records = MemoryStore(vault)
    candidate = records.submit_candidate(
        "preference", {"type": "project", "project_id": "project-01"}, "personal", "personal-vault", body, receipt,
        provenance=provenance,
    )
    service = CoreService(vault)

    before = service.profile({"type": "project", "project_id": "project-01"})
    records.promote(candidate.id, 0, receipt)
    after = service.profile({"type": "project", "project_id": "project-01"})

    assert body not in before.text
    assert body in after.text


def test_profile_does_not_reuse_pre_tightening_cache_after_render_race(
    admitted_artifacts, monkeypatch
):
    """Catches an allowed pre-render decision loading a cache after official tightening."""
    import threading

    import mneme.core.memories as memories_module

    from mneme.core.service import CoreService

    vault, _session, _memory, permitted = admitted_artifacts
    service = CoreService(vault)
    safe_before_tightening = service.profile({"type": "project", "project_id": "project-01"})
    original = memories_module.render_profile
    started = threading.Event()
    completed = threading.Event()
    triggered = False

    def tighten_concurrently():
        started.set()
        _tighten(vault, permitted)
        completed.set()

    def render_after_official_tightening(*args, **kwargs):
        nonlocal triggered
        if triggered:
            return original(*args, **kwargs)
        triggered = True
        thread = threading.Thread(target=tighten_concurrently)
        thread.start()
        assert started.wait(timeout=2)
        thread.join(timeout=0.5)
        try:
            return original(*args, **kwargs)
        finally:
            if completed.is_set():
                thread.join(timeout=2)

    monkeypatch.setattr(memories_module, "render_profile", render_after_official_tightening)
    after = service.profile({"type": "project", "project_id": "project-01"})
    completed_during_render = completed.is_set()
    assert completed.wait(timeout=5)
    final = service.profile({"type": "project", "project_id": "project-01"})

    assert "sensitive-preference-body" in safe_before_tightening.text
    if completed_during_render:
        assert "sensitive-preference-body" not in after.text
    assert "sensitive-preference-body" not in final.text


def test_local_memory_authorization_tracks_portable_source_policy_inputs(tmp_path):
    """Catches a local Memory ignoring its permitted portable source assignment or digest."""
    from mneme.core.artifacts import ArtifactReference, ReferenceKind, StorageClass
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.security import PolicyArtifactRef, PolicyAuthorizer
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    policies = PolicyStore(vault)
    initial = policies.create_revision(
        "portable-source-policy", "1", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    policies.activate(initial, expected_generation=1)
    source = RegistryStore(vault).register_source("portable-source")
    RegistryStore(vault).assign_source_policy(source.id, initial, expected_generation=0)
    body = "LOCAL_SECRET_FROM_PORTABLE_SOURCE"
    provenance = (ArtifactReference(ReferenceKind.ID, StorageClass.PORTABLE, source.id),)
    default = policies.load_active("vault-default", StorageClass.PORTABLE)
    receipt = evaluate_portability(
        Portability.LOCAL_ONLY, (policies.load_rule(default),),
        memory_semantic_hash(body=body, kind="preference", portability="local-only", provenance=provenance),
    )
    records = MemoryStore(vault, StorageClass.LOCAL_ONLY)
    candidate = records.submit_candidate(
        "preference", {"type": "personal-global"}, "personal", "local-only", body, receipt, provenance=provenance
    )
    local = records.promote(candidate.id, 0, receipt)
    authorizer = PolicyAuthorizer(vault)
    reference = PolicyArtifactRef.memory(local.id, StorageClass.LOCAL_ONLY)
    before = authorizer.authorize_current(reference, "profile")
    changed = policies.create_revision(
        initial.policy_id, "2", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    policies.activate(changed, expected_generation=2)
    observed = RegistryStore(vault).load_source(source.id)
    RegistryStore(vault).assign_source_policy(source.id, changed, expected_generation=observed.generation)
    after_assignment = authorizer.authorize_current(reference, "profile")
    policy_path = vault.root / f".madi/policies/{changed.policy_id}/{changed.revision}.yaml"
    policy_path.write_text("corrupt policy", encoding="utf-8")
    unavailable = authorizer.authorize_current(reference, "profile")

    assert before.allowed is True
    assert after_assignment.allowed is True
    assert after_assignment.authorization_fingerprint != before.authorization_fingerprint
    assert unavailable.allowed is False
    assert unavailable.issue_codes == ("policy-current-unavailable",)


@pytest.mark.parametrize("view_kind", ("context", "profile"))
@pytest.mark.parametrize("operation", ("load", "write"))
def test_generated_view_io_failure_returns_the_fresh_safe_projection(
    admitted_artifacts, monkeypatch, view_kind, operation
):
    """Catches a generated-view OSError leaking a local path through Core output."""
    from mneme.core.service import CoreService
    from mneme.core.storage import ViewStore

    vault, _session, _memory, _permitted = admitted_artifacts
    local_path = r"C:\\private\\madi-local\\views\\CURRENT.md"

    def fail_io(*_args, **_kwargs):
        raise OSError(f"unable to access {local_path}")

    monkeypatch.setattr(ViewStore, operation, fail_io)
    service = CoreService(vault)
    view = (
        service.context("ws-01")
        if view_kind == "context"
        else service.profile({"type": "project", "project_id": "project-01"})
    )

    expected_body = (
        "sensitive-session-body"
        if view_kind == "context"
        else "sensitive-preference-body"
    )
    assert expected_body in view.text
    assert local_path not in view.text


def test_current_cache_binds_the_exact_render_mode(admitted_artifacts):
    """Catches a portable CURRENT cache being reused for effective-local output."""
    from mneme.core.service import CoreService

    vault, _session, _memory, _permitted = admitted_artifacts
    service = CoreService(vault)

    portable = service.context("ws-01", "portable")
    effective_local = service.context("ws-01", "effective-local")

    assert "# Current" in portable.text
    assert "# CURRENT" in effective_local.text
    assert "# Portable base" in effective_local.text
    assert effective_local.text != portable.text
