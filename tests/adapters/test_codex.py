"""Codex PreCompact boundaries around the agent-neutral Core contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


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
        "adapter": "codex",
        "session_id": "codex-1",
        "workstream_id": "ws-1",
    }
    envelope.update(updates)
    return envelope


def _checkpoint_payload(
    *,
    session_id: str = "codex-1",
    adapter_id: str = "codex",
    generation: int = 0,
    relations: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "workstream_id": "ws-1",
        "session_id": session_id,
        "storage_class": "portable",
        "expected_parent": None,
        "expected_registry_generation": generation,
        "body": {
            "adapter_id": adapter_id,
            "objective": "Preserve host-selected continuity",
            "current_state": "Ready for the next agent turn",
            "verified_facts": ["The host composed this checkpoint."],
            "completed_work": ["Observed the lifecycle boundary."],
            "blockers": [],
            "next_actions": ["Resume from this explicit revision."],
            "source_refs": [],
        },
        "relations": [] if relations is None else relations,
    }


def _write_checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_native_fixture_translates_to_the_pinned_agent_neutral_event():
    """Catches native field names or native identity leaking into the Core event."""
    from mneme.adapters.codex import CodexLifecycleEvent, translate_pre_compact
    from mneme.core.contracts import LifecycleEvent

    native = _fixture("pre_compact.native.json")
    expected = _fixture("pre_compact.normalized.json")

    translated = translate_pre_compact(native, {"workstream_id": "ws-1"})

    assert isinstance(translated, LifecycleEvent)
    assert isinstance(translated, CodexLifecycleEvent)
    assert translated.as_dict() == expected["event"]
    assert translated.native_metadata == expected["native_metadata"]
    assert translated.as_core_event() == LifecycleEvent(
        version=1,
        name="pre_compact",
        adapter="codex",
        session_id="codex-1",
        workstream_id="ws-1",
    )


def test_optional_agent_fields_are_ignored_and_native_private_fields_are_noncanonical():
    """Catches optional/native host details entering the portable lifecycle shape."""
    from mneme.adapters.codex import translate_pre_compact

    with_agent = _fixture("pre_compact.native.json")
    without_agent = {
        key: value
        for key, value in with_agent.items()
        if key not in {"agent_id", "agent_type"}
    }

    first = translate_pre_compact(with_agent, {"workstream_id": "ws-1"})
    second = translate_pre_compact(without_agent, {"workstream_id": "ws-1"})
    encoded = json.dumps(first.as_dict())

    assert first == second
    assert first.native_metadata == second.native_metadata
    for private_value in (
        with_agent["transcript_path"],
        with_agent["cwd"],
        with_agent["model"],
        with_agent["agent_id"],
        with_agent["agent_type"],
    ):
        assert private_value not in encoded


def test_transcript_path_may_be_null_without_becoming_a_discovery_condition():
    """Catches the translator requiring or opening a transcript to normalize a hook."""
    from mneme.adapters.codex import translate_pre_compact

    native = _fixture("pre_compact.native.json")
    native["transcript_path"] = None

    translated = translate_pre_compact(native, {"workstream_id": "ws-1"})

    assert translated.session_id == "codex-1"
    assert translated.workstream_id == "ws-1"


@pytest.mark.parametrize(
    "binding",
    [
        {},
        {"workstream_id": None},
        {"workstream_id": "ws-1", "transcript_path": "forbidden"},
    ],
)
def test_workstream_identity_comes_only_from_a_closed_machine_local_binding(binding):
    """Catches a missing, nullable, or native-derived workstream association."""
    from mneme.adapters.codex import translate_pre_compact
    from mneme.core.errors import InvalidArtifact

    with pytest.raises(InvalidArtifact):
        translate_pre_compact(_fixture("pre_compact.native.json"), binding)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("model"),
        lambda payload: payload.__setitem__("hook_event_name", "SessionEnd"),
        lambda payload: payload.__setitem__("trigger", "scheduled"),
    ],
    ids=("missing-required", "wrong-event", "unknown-trigger"),
)
def test_invalid_native_payload_returns_a_closed_nonblocking_result(
    service, project, mutate
):
    """Catches malformed native input escaping or exposing its host fields."""
    from mneme.adapters.codex import CodexAdapter

    payload = _fixture("pre_compact.native.json")
    mutate(payload)

    result = CodexAdapter(service, project_root=project).handle_native(
        payload, {"workstream_id": "ws-1"}
    )

    encoded = json.dumps(result.as_dict())
    assert result.ok is False
    assert result.block_host is False
    assert result.event == "unknown"
    assert result.warning == (
        "Madi could not use this Codex lifecycle event; continue ordinary Codex work."
    )
    assert "private" not in encoded
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []


def test_native_precompact_observes_a_plain_lifecycle_event_without_writing(
    service, project
):
    """Catches native metadata crossing into Core or observation creating an artifact."""
    from mneme.adapters.codex import CodexAdapter
    from mneme.core.contracts import LifecycleEvent

    class RecordingService:
        def __init__(self, delegate):
            self.delegate = delegate
            self.observed = None

        def observe(self, event):
            self.observed = event
            return self.delegate.observe(event)

        def execute(self, command):
            raise AssertionError("a bare lifecycle event must not execute a command")

    recording = RecordingService(service)
    before = tuple(sorted(service.vault.root.rglob("*")))

    result = CodexAdapter(recording, project_root=project).handle_native(
        _fixture("pre_compact.native.json"), {"workstream_id": "ws-1"}
    )

    assert result.ok is True
    assert result.checkpoint_required is True
    assert result.command_result is None
    assert type(recording.observed) is LifecycleEvent
    assert recording.observed.as_dict() == _fixture("pre_compact.normalized.json")[
        "event"
    ]
    assert tuple(sorted(service.vault.root.rglob("*"))) == before


def test_codex_precompact_uses_the_same_explicit_core_checkpoint_command(service, project):
    """Catches Codex bypassing the shared create_session_revision command path."""
    from mneme.adapters.codex import CodexAdapter

    checkpoint = project / ".madi" / "checkpoints" / "codex-selected.json"
    _write_checkpoint(checkpoint, _checkpoint_payload())

    result = CodexAdapter(service, project_root=project).handle(
        _envelope(checkpoint_file=str(checkpoint))
    )

    assert result.ok is True
    assert result.command_result is not None
    assert result.command_result.command == "create_session_revision"
    assert result.domain_events[0].name == "session_revision_created"
    assert (service.vault.root / "workstreams/ws-1/sessions/codex-1/000001.md").is_file()
    assert str(checkpoint) not in json.dumps(result.as_dict())


def test_native_details_never_reach_a_codex_session_or_registry(service, project):
    """Catches transcript, cwd, model, turn, or agent details becoming Vault state."""
    from mneme.adapters.codex import CodexAdapter

    adapter = CodexAdapter(service, project_root=project)
    native = _fixture("pre_compact.native.json")
    observed = adapter.handle_native(native, {"workstream_id": "ws-1"})
    checkpoint = project / ".madi" / "checkpoints" / "sanitized.json"
    _write_checkpoint(checkpoint, _checkpoint_payload())
    created = adapter.handle(_envelope(checkpoint_file=str(checkpoint)))

    canonical_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in service.vault.root.rglob("*")
        if path.is_file() and path.suffix in {".md", ".yaml"}
    )
    assert observed.ok is created.ok is True
    for native_field in (
        "transcript_path",
        "cwd",
        "model",
        "turn_id",
        "agent_id",
        "agent_type",
    ):
        assert str(native[native_field]) not in canonical_text


def test_codex_service_failure_is_closed_and_never_blocks_ordinary_work(project):
    """Catches unavailable Core details escaping or blocking the Codex host."""
    from mneme.adapters.codex import CodexAdapter

    class UnavailableService:
        def observe(self, event):
            raise OSError("C:/private/madi-token.txt is unavailable")

    result = CodexAdapter(UnavailableService(), project_root=project).handle_native(
        _fixture("pre_compact.native.json"), {"workstream_id": "ws-1"}
    )

    encoded = json.dumps(result.as_dict())
    assert result.ok is False
    assert result.block_host is False
    assert result.warning == "Madi is unavailable; continue ordinary Codex work."
    assert "private" not in encoded
    assert "token" not in encoded


def test_agent_switch_creates_a_distinct_codex_lineage_without_implicit_preference(
    service, project
):
    """Catches continuation overwriting Claude or translator magic selecting Codex."""
    from mneme.adapters.codex import CodexAdapter
    from mneme.core.contracts import CoreCommand
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import SessionRevisionRef, SessionStore

    claude = service.execute(
        CoreCommand(
            1,
            "create_session_revision",
            _checkpoint_payload(session_id="claude-1", adapter_id="claude"),
        )
    )
    assert claude.ok is True
    relation = {
        "kind": "continues_from",
        "target": "claude-1@000001",
        "purpose": "Continue the same work with Codex",
        "required_context": "Use the selected Claude revision",
        "next_action": "Resume in the distinct Codex session",
        "provenance_refs": [],
    }
    checkpoint = project / ".madi" / "checkpoints" / "agent-switch.json"
    _write_checkpoint(
        checkpoint,
        _checkpoint_payload(generation=1, relations=[relation]),
    )

    switched = CodexAdapter(service, project_root=project).handle(
        _envelope(event="agent_switched", checkpoint_file=str(checkpoint))
    )

    registry = RegistryStore(service.vault).load_workstream("ws-1")
    codex_revision = SessionStore(service.vault).read_revision(
        SessionRevisionRef("codex-1", "000001"), workstream_id="ws-1"
    )
    assert switched.ok is True
    assert {head.session for head in registry.active_heads} == {"claude-1", "codex-1"}
    assert registry.mode == "parallel"
    assert registry.preferred_head is None
    assert codex_revision.relations[0].kind == "continues_from"
    assert codex_revision.relations[0].target == "claude-1@000001"

    preferred = service.execute(
        CoreCommand(
            1,
            "set_preferred_head",
            {
                "expected_generation": 2,
                "preferred_head": {"session": "codex-1", "revision": "000001"},
                "storage_class": "portable",
                "workstream_id": "ws-1",
            },
        )
    )

    selected = RegistryStore(service.vault).load_workstream("ws-1")
    assert preferred.ok is True
    assert preferred.domain_events[0].name == "preferred_head_changed"
    assert selected.preferred_head.session == "codex-1"
