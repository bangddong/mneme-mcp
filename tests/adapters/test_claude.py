"""Claude adapter boundaries around the agent-neutral Core contract."""

from __future__ import annotations

import json

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


def test_checkpoint_file_must_be_under_the_project_or_configured_temp_root(
    service, project, tmp_path
):
    """Catches a native envelope using an arbitrary host path as Core input."""
    from mneme.adapters.claude import ClaudeAdapter

    outside = tmp_path / "outside.json"
    _write_checkpoint(outside, _checkpoint_payload())

    result = ClaudeAdapter(
        service, project_root=project, temp_root=tmp_path / "adapter-temp"
    ).handle(_envelope(checkpoint_file=str(outside)))

    assert result.ok is False
    assert result.block_host is False
    assert result.warning == "Madi could not use the selected checkpoint; continue ordinary Claude work."
    assert str(outside) not in json.dumps(result.as_dict())
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
