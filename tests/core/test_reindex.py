import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def test_missing_registered_mount_is_a_degraded_reindex_diagnostic(vault):
    """Catches an optional source mount preventing Vault-only recall startup."""
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService

    RegistryStore(vault).register_source("source-01", authority="external-reference")

    report = CoreService(vault).reindex()

    assert report.status == "degraded"
    assert report.diagnostics == ("source source-01 is unavailable",)


def test_missing_registered_mount_degrades_context_after_successful_reindex(vault):
    """Catches an existing generated DB hiding an unavailable optional source."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService
    from mneme.core.sessions import (
        CheckpointRequest,
        SessionBody,
        SessionStore,
        session_semantic_hash,
    )

    registries = RegistryStore(vault)
    registries.register_source("source-01", authority="external-reference")
    registries.create_workstream("ws-01", project=None, mode="single")
    body = SessionBody(
        "codex", "Retain mounted source diagnostics", "working", (), (), (), (), ()
    )
    policies = PolicyStore(vault)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (policies.load_rule(policies.load_active("vault-default", StorageClass.PORTABLE)),),
        session_semantic_hash(body, ()),
    )
    SessionStore(vault).create_revision(
        CheckpointRequest(
            "ws-01", "ses-01", StorageClass.PORTABLE, None, 0, body, (), receipt
        )
    )
    service = CoreService(vault)
    assert service.reindex().status == "degraded"

    view = service.context("ws-01")
    recall = service.recall("mounted source diagnostics", 10)

    assert view.status.value == "degraded"
    assert any(hit.category == "session" for hit in recall.hits)
    assert recall.status == "degraded"


def test_malformed_canonical_artifact_is_invalid_not_an_optional_degradation(vault):
    """Catches a broken canonical Vault being classified like a missing mount."""
    from mneme.core.service import CoreService

    (vault.root / "memory" / "broken.md").write_text(
        "---\nschema: invalid\n---\nbroken\n", encoding="utf-8"
    )

    report = CoreService(vault).reindex()

    assert report.status == "invalid"
    assert report.diagnostics == (
        "canonical Vault indexing failed: InvalidArtifact",
    )
