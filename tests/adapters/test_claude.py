"""Claude adapter boundaries around the agent-neutral Core contract."""

from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def service(tmp_path):
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    RegistryStore(vault).create_workstream("ws-1", project=None, mode="parallel")
    return CoreService(vault)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return root


def _envelope(**updates: object) -> dict[str, object]:
    envelope: dict[str, object] = {
        "version": 1,
        "event": "pre_compact",
        "adapter": "claude",
        "session_id": "claude-1",
        "workstream_id": "ws-1",
    }
    envelope.update(updates)
    return envelope


def _checkpoint_payload(session_id: str = "claude-1") -> dict[str, object]:
    return {
        "workstream_id": "ws-1",
        "session_id": session_id,
        "storage_class": "portable",
        "expected_parent": None,
        "expected_registry_generation": 0,
        "body": {
            "adapter_id": "claude",
            "objective": "Preserve a host-selected decision",
            "current_state": "Ready for compaction",
            "verified_facts": ["The host composed this checkpoint."],
            "completed_work": ["Observed the lifecycle boundary."],
            "blockers": [],
            "next_actions": ["Resume from the explicit revision."],
            "source_refs": [],
        },
        "relations": [],
    }


def _write_checkpoint(path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_claude_adapter_failure_warns_without_blocking_and_hides_raw_error(project):
    """Catches service exceptions escaping, blocking Claude, or exposing local data."""
    from mneme.adapters.claude import ClaudeAdapter

    class UnavailableService:
        def observe(self, event):
            raise OSError("C:/private/madi-token.txt is unavailable")

    result = ClaudeAdapter(UnavailableService(), project_root=project).handle(_envelope())

    assert result.ok is False
    assert result.block_host is False
    assert result.warning == "Madi is unavailable; continue ordinary Claude work."
    assert "private" not in json.dumps(result.as_dict())
    assert "token" not in json.dumps(result.as_dict())


def test_bare_precompact_observes_lifecycle_and_requires_checkpoint_without_writing(
    service, project
):
    """Catches a lifecycle observation silently becoming a canonical checkpoint."""
    from mneme.adapters.claude import ClaudeAdapter

    before = tuple(sorted(service.vault.root.rglob("*")))
    result = ClaudeAdapter(service, project_root=project).handle(_envelope())

    assert result.ok is True
    assert result.block_host is False
    assert result.event == "pre_compact"
    assert result.checkpoint_required is True
    assert result.context_required is False
    assert result.command_result is None
    assert tuple(sorted(service.vault.root.rglob("*"))) == before


def test_host_composed_checkpoint_file_issues_only_the_core_revision_command(
    service, project
):
    """Catches the adapter deriving checkpoint semantics or forwarding a file path to Core."""
    from mneme.adapters.claude import ClaudeAdapter

    checkpoint = project / ".madi" / "checkpoints" / "selected.json"
    _write_checkpoint(checkpoint, _checkpoint_payload())

    result = ClaudeAdapter(service, project_root=project).handle(
        _envelope(checkpoint_file=str(checkpoint))
    )

    assert result.ok is True
    assert result.command_result is not None
    assert result.command_result.command == "create_session_revision"
    assert result.command_result.result["revision"] == "000001"
    assert (service.vault.root / "workstreams/ws-1/sessions/claude-1/000001.md").is_file()
    assert str(checkpoint) not in json.dumps(result.as_dict())


def test_adapter_rejects_detectable_secret_checkpoint_without_writing_or_echoing(
    service, project
):
    """Catches a secret-bearing checkpoint reaching the portable Session writer."""
    from mneme.adapters.claude import ClaudeAdapter

    checkpoint = project / ".madi" / "checkpoints" / "secret.json"
    payload = _checkpoint_payload()
    payload["body"]["current_state"] = "API_KEY=SENSITIVE_SENTINEL"
    _write_checkpoint(checkpoint, payload)

    result = ClaudeAdapter(service, project_root=project).handle(
        _envelope(checkpoint_file=str(checkpoint))
    )

    encoded = json.dumps(result.as_dict())
    assert result.ok is False
    assert result.block_host is False
    assert "SENSITIVE_SENTINEL" not in encoded
    assert str(checkpoint) not in encoded
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []


@pytest.mark.parametrize(
    ("storage_class", "workstream_id"),
    [("portable", "ws-1"), ("local-only", "ws-local")],
)
def test_adapter_rejects_detectable_credential_reference_without_writing(
    service, project, storage_class, workstream_id
):
    """Catches a reference value bypassing the adapter's shared Session guard."""
    from mneme.adapters.claude import ClaudeAdapter
    from mneme.core.artifacts import StorageClass
    from mneme.core.registries import RegistryStore

    selected_class = StorageClass(storage_class)
    if selected_class is StorageClass.LOCAL_ONLY:
        RegistryStore(service.vault, selected_class).create_workstream(
            workstream_id, project=None, mode="parallel"
        )
    checkpoint = project / ".madi" / "checkpoints" / f"reference-{storage_class}.json"
    payload = _checkpoint_payload()
    payload["storage_class"] = storage_class
    payload["workstream_id"] = workstream_id
    payload["body"]["source_refs"] = [
        {
            "kind": "label",
            "storage_class": "portable",
            "value": "API_KEY=SENSITIVE_SENTINEL",
        }
    ]
    _write_checkpoint(checkpoint, payload)

    result = ClaudeAdapter(service, project_root=project).handle(
        _envelope(workstream_id=workstream_id, checkpoint_file=str(checkpoint))
    )

    encoded = json.dumps(result.as_dict())
    root = (
        service.vault.root
        if selected_class is StorageClass.PORTABLE
        else service.vault.local_root / "overlays"
    )
    registry = RegistryStore(service.vault, selected_class).load_workstream(workstream_id)
    assert result.ok is False
    assert result.block_host is False
    assert "SENSITIVE_SENTINEL" not in encoded
    assert str(checkpoint) not in encoded
    assert list(root.glob("workstreams/*/sessions/*/*.md")) == []
    assert registry.generation == 0
    assert registry.active_heads == ()


def test_adapter_accepts_checkpoint_under_an_explicit_configured_temp_root(
    service, project, tmp_path
):
    """Catches an explicit narrow temporary checkpoint root being rejected."""
    from mneme.adapters.claude import ClaudeAdapter

    temp_root = tmp_path / "madi-checkpoints"
    temp_root.mkdir()
    checkpoint = temp_root / "selected.json"
    _write_checkpoint(checkpoint, _checkpoint_payload())

    result = ClaudeAdapter(
        service, project_root=project, temp_root=temp_root
    ).handle(_envelope(checkpoint_file=str(checkpoint)))

    assert result.ok is True
    assert result.command_result is not None
    assert str(checkpoint) not in json.dumps(result.as_dict())


def test_checkpoint_file_must_be_under_the_project_or_configured_temp_root(
    service, project, tmp_path
):
    """Catches a native envelope using an arbitrary host path as Core input."""
    from mneme.adapters.claude import ClaudeAdapter

    temp_root = tmp_path / "adapter-temp"
    temp_root.mkdir()
    outside = tmp_path / "outside.json"
    _write_checkpoint(outside, _checkpoint_payload())

    result = ClaudeAdapter(
        service, project_root=project, temp_root=temp_root
    ).handle(_envelope(checkpoint_file=str(outside)))

    assert result.ok is False
    assert result.block_host is False
    assert result.warning == "Madi could not use the selected checkpoint; continue ordinary Claude work."
    assert str(outside) not in json.dumps(result.as_dict())
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []


def test_adapter_does_not_trust_a_sibling_system_temp_checkpoint_by_default(
    service, project, tmp_path
):
    """Catches the full system temporary directory becoming an implicit trust root."""
    from mneme.adapters.claude import ClaudeAdapter

    sibling_checkpoint = tmp_path / "sibling-project-checkpoint.json"
    _write_checkpoint(sibling_checkpoint, _checkpoint_payload())

    result = ClaudeAdapter(service, project_root=project).handle(
        _envelope(checkpoint_file=str(sibling_checkpoint))
    )

    encoded = json.dumps(result.as_dict())
    assert result.ok is False
    assert result.block_host is False
    assert str(sibling_checkpoint) not in encoded
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []


def test_adapter_rejects_a_hard_linked_checkpoint_alias(service, project, tmp_path):
    """Catches an outside checkpoint gaining project trust through a hard link."""
    from mneme.adapters.claude import ClaudeAdapter

    outside = tmp_path / "outside-content.json"
    _write_checkpoint(outside, _checkpoint_payload())
    alias = project / ".madi" / "checkpoints" / "alias.json"
    alias.parent.mkdir(parents=True)
    try:
        os.link(outside, alias)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc.__class__.__name__}")

    result = ClaudeAdapter(service, project_root=project).handle(
        _envelope(checkpoint_file=str(alias))
    )

    encoded = json.dumps(result.as_dict())
    assert result.ok is False
    assert result.block_host is False
    assert str(alias) not in encoded
    assert str(outside) not in encoded
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []


def test_adapter_rejects_raw_transcript_fields_instead_of_summarizing_them(service, project):
    """Catches Claude-native transcript material crossing the normalized envelope boundary."""
    from mneme.adapters.claude import ClaudeAdapter

    result = ClaudeAdapter(service, project_root=project).handle(
        _envelope(transcript="API_KEY=private-value")
    )

    assert result.ok is False
    assert result.block_host is False
    assert result.warning == "Madi could not use this Claude lifecycle event; continue ordinary Claude work."
    assert "private-value" not in json.dumps(result.as_dict())
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []


def test_adapter_requires_a_concrete_workstream_identity(service, project):
    """Catches a normalized envelope bypassing the explicit workstream boundary."""
    from mneme.adapters.claude import ClaudeAdapter

    result = ClaudeAdapter(service, project_root=project).handle(
        _envelope(workstream_id=None)
    )

    assert result.ok is False
    assert result.block_host is False
    assert result.event == "unknown"
    assert result.warning == "Madi could not use this Claude lifecycle event; continue ordinary Claude work."
