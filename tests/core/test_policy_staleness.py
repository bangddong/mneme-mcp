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
    registries.create_workstream("ws-01", project=project.id, mode="single")
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
