"""Agent-neutral lifecycle, command, and event contract regressions."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def service(tmp_path):
    from mneme.core.service import CoreService
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    return CoreService(vault)


def _checkpoint_payload(*, generation: int = 0) -> dict[str, object]:
    return {
        "workstream_id": "ws-1",
        "session_id": "s1",
        "storage_class": "portable",
        "expected_parent": None,
        "expected_registry_generation": generation,
        "body": {
            "adapter_id": "codex",
            "objective": "Preserve selected continuity",
            "current_state": "Ready to compact",
            "verified_facts": ["The host selected this checkpoint."],
            "completed_work": ["Separated lifecycle observation from mutation."],
            "blockers": [],
            "next_actions": ["Resume from the explicit head."],
            "source_refs": [],
        },
        "relations": [],
    }


def _command(name: str, payload: dict[str, object]):
    from mneme.core.contracts import CoreCommand

    return CoreCommand(version=1, name=name, payload=payload)


@pytest.mark.parametrize(
    ("storage_class", "workstream_id"),
    [("portable", "ws-1"), ("local-only", "ws-local")],
)
def test_checkpoint_rejects_detectable_credentials_before_any_session_write(
    service, storage_class, workstream_id
):
    """Catches credentials entering durable portable or local-only Session revisions."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.registries import RegistryStore

    selected_class = StorageClass(storage_class)
    RegistryStore(service.vault, selected_class).create_workstream(
        workstream_id, project=None, mode="parallel"
    )
    payload = _checkpoint_payload()
    payload["storage_class"] = storage_class
    payload["workstream_id"] = workstream_id
    payload["body"]["current_state"] = "API_KEY=SENSITIVE_SENTINEL"

    result = service.execute(_command("create_session_revision", payload))

    encoded = json.dumps(result.as_dict())
    root = (
        service.vault.root
        if selected_class is StorageClass.PORTABLE
        else service.vault.local_root / "overlays"
    )
    registry = RegistryStore(service.vault, selected_class).load_workstream(workstream_id)
    assert result.ok is False
    assert result.error.code == "invalid-artifact"
    assert "SENSITIVE_SENTINEL" not in encoded
    assert list(root.glob("workstreams/*/sessions/*/*.md")) == []
    assert registry.generation == 0
    assert registry.active_heads == ()


def test_checkpoint_allows_a_non_secret_reference_to_credential_detection(service):
    """Catches the deterministic guard becoming a broad transcript-content ban."""
    from mneme.core.registries import RegistryStore

    RegistryStore(service.vault).create_workstream("ws-1", project=None, mode="parallel")
    payload = _checkpoint_payload()
    payload["body"]["verified_facts"] = [
        "The API key detector rejects detectable credentials before persistence."
    ]

    result = service.execute(_command("create_session_revision", payload))

    assert result.ok is True
    assert result.result["revision"] == "000001"


def test_lifecycle_event_cannot_mutate_core(service):
    """Catches pre-compact observation being treated as an implicit checkpoint."""
    from mneme.core.contracts import LifecycleEvent

    event = LifecycleEvent(
        version=1, name="pre_compact", adapter="codex", session_id="s1"
    )
    result = service.observe(event)

    assert result.checkpoint_required is True
    assert result.context_required is False
    assert list(service.vault.root.glob("workstreams/*/sessions/*/*.md")) == []
    assert list(service.vault.root.glob("memory/*.md")) == []


@pytest.mark.parametrize(
    ("event_name", "checkpoint_required", "context_required"),
    [
        ("session_started", False, True),
        ("session_resumed", False, True),
        ("pre_compact", True, False),
        ("milestone_reached", True, False),
        ("session_ended", True, False),
        ("agent_switched", True, False),
    ],
)
def test_lifecycle_vocabulary_has_observation_only_outcomes(
    service, event_name, checkpoint_required, context_required
):
    """Catches a lifecycle name acquiring an implicit domain-state transition."""
    from mneme.core.contracts import LifecycleEvent

    before = tuple(sorted(path.relative_to(service.vault.root) for path in service.vault.root.rglob("*")))
    result = service.observe(LifecycleEvent(1, event_name, "codex", "s1", "ws-1"))

    assert result.checkpoint_required is checkpoint_required
    assert result.context_required is context_required
    assert result.domain_events == ()
    assert tuple(sorted(path.relative_to(service.vault.root) for path in service.vault.root.rglob("*"))) == before


def test_contract_vocabularies_are_closed(service):
    """Catches adapter-native or event vocabulary entering the Core command surface."""
    from mneme.core.contracts import CoreCommand, DomainEvent, LifecycleEvent, OperationalEvent
    from mneme.core.errors import InvalidArtifact

    with pytest.raises(InvalidArtifact):
        LifecycleEvent(1, "claude_pre_compact", "claude", "s1")
    with pytest.raises(InvalidArtifact):
        CoreCommand(1, "pre_compact", {})
    with pytest.raises(InvalidArtifact):
        DomainEvent(1, "source_unavailable", {})
    with pytest.raises(InvalidArtifact):
        OperationalEvent(1, "session_revision_created", {})


def test_command_result_envelope_rejects_open_names_and_statuses():
    """Catches arbitrary transport text entering nominally closed output fields."""
    from mneme.core.contracts import CommandResult
    from mneme.core.errors import InvalidArtifact

    with pytest.raises(InvalidArtifact):
        CommandResult.success("adapter_native_action", {})
    with pytest.raises(InvalidArtifact):
        CommandResult.success("madi_recall", {}, status="C:/private/result")


@pytest.mark.parametrize(
    "name",
    [
        "get_context",
        "open_session",
        "create_session_revision",
        "submit_memory",
        "promote_memory",
        "retire_memory",
        "set_active_heads",
        "set_preferred_head",
        "register_source",
        "create_policy_revision",
        "activate_policy_revision",
        "assign_project_policy",
        "assign_source_policy",
    ],
)
def test_core_command_vocabulary_accepts_each_approved_name(name):
    """Catches an approved D3 command being dropped or silently renamed."""
    from mneme.core.contracts import CoreCommand

    assert CoreCommand(1, name, {}).name == name


def test_get_context_and_open_session_are_non_mutating_explicit_reads(service):
    """Catches opening a session fabricating an empty canonical Session artifact."""
    from mneme.core.registries import RegistryStore

    RegistryStore(service.vault).create_workstream("ws-1", project=None, mode="parallel")
    before = tuple(sorted(path.relative_to(service.vault.root) for path in service.vault.root.rglob("*")))

    opened = service.execute(
        _command(
            "open_session",
            {
                "session_id": "s1",
                "storage_class": "portable",
                "workstream_id": "ws-1",
            },
        )
    )
    context = service.execute(
        _command("get_context", {"mode": "portable", "workstream_id": "ws-1"})
    )

    assert opened.ok is True
    assert opened.result == {
        "active_head": None,
        "registry_generation": 0,
        "session_id": "s1",
        "storage_class": "portable",
        "workstream_id": "ws-1",
    }
    assert opened.domain_events == ()
    assert context.ok is True
    assert context.result["workstream_id"] == "ws-1"
    assert context.domain_events == ()
    assert tuple(sorted(path.relative_to(service.vault.root) for path in service.vault.root.rglob("*"))) == before


def test_open_session_hides_confidential_workstream_existence(service):
    """Catches read-before-authorize turning a forbidden workstream into an oracle."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore

    restrictive = PolicyStore(service.vault).create_revision(
        "confidential-workstream",
        "1",
        {"ceiling": "local-only"},
        StorageClass.PORTABLE,
    )
    RegistryStore(service.vault).create_workstream(
        "secret-workstream",
        project=None,
        mode="parallel",
        policy_refs=(restrictive,),
    )

    forbidden = service.execute(
        _command(
            "open_session",
            {"session_id": "s1", "workstream_id": "secret-workstream"},
        )
    )
    absent = service.execute(
        _command(
            "open_session",
            {"session_id": "s1", "workstream_id": "absent-workstream"},
        )
    )

    assert forbidden.ok is absent.ok is False
    assert forbidden.error.code == "policy-denied"
    assert forbidden.as_dict() == absent.as_dict()


def test_create_session_revision_returns_domain_events_without_an_event_artifact(service):
    """Catches a successful checkpoint omitting its fact or adding a fourth artifact family."""
    from mneme.core.registries import RegistryStore

    RegistryStore(service.vault).create_workstream("ws-1", project=None, mode="parallel")
    result = service.execute(_command("create_session_revision", _checkpoint_payload()))

    assert result.ok is True
    assert result.status == "resolved"
    assert result.result == {
        "revision": "000001",
        "session_id": "s1",
        "storage_class": "portable",
        "workstream_id": "ws-1",
    }
    assert [event.name for event in result.domain_events] == [
        "session_revision_created"
    ]
    assert result.operational_events == ()
    assert (service.vault.root / "workstreams/ws-1/sessions/s1/000001.md").is_file()
    assert {item.name for item in service.vault.root.iterdir()} == {
        ".madi",
        "memory",
        "projects",
        "sources",
        "workstreams",
    }
    assert not any("event" in path.name.lower() for path in service.vault.root.rglob("*"))


def test_handoff_is_an_additional_fact_of_one_session_revision(service):
    """Catches handoff becoming an independently mutable command or artifact."""
    from mneme.core.registries import RegistryStore

    RegistryStore(service.vault).create_workstream("ws-1", project=None, mode="parallel")
    payload = _checkpoint_payload()
    payload["relations"] = [
        {
            "kind": "handoff",
            "target": "agent-next",
            "purpose": "Continue the approved task",
            "required_context": "Use the explicit session revision",
            "next_action": "Load current context",
            "provenance_refs": [],
        }
    ]

    result = service.execute(_command("create_session_revision", payload))

    assert result.ok is True
    assert [event.name for event in result.domain_events] == [
        "session_revision_created",
        "handoff_recorded",
    ]
    assert list(service.vault.root.glob("**/*handoff*")) == []


def test_memory_commands_use_candidate_acceptance_and_retirement_lifecycle(service):
    """Catches command handling mutating Memory outside its official CAS lifecycle."""
    submitted = service.execute(
        _command(
            "submit_memory",
            {
                "authority": "personal",
                "body": "A deliberately selected memory.",
                "kind": "lesson",
                "memory_id": "memory-1",
                "portability": "personal-vault",
                "scope": {"type": "personal-global"},
            },
        )
    )
    promoted = service.execute(
        _command(
            "promote_memory",
            {
                "expected_generation": 0,
                "memory_id": "memory-1",
                "storage_class": "portable",
            },
        )
    )
    retired = service.execute(
        _command(
            "retire_memory",
            {
                "expected_generation": 1,
                "memory_id": "memory-1",
                "reason": "The lesson is obsolete.",
                "storage_class": "portable",
            },
        )
    )

    assert submitted.result["status"] == "candidate"
    assert [event.name for event in submitted.domain_events] == ["memory_submitted"]
    assert promoted.result["status"] == "accepted"
    assert [event.name for event in promoted.domain_events] == ["memory_accepted"]
    assert retired.result["status"] == "retired"
    assert [event.name for event in retired.domain_events] == ["memory_retired"]
    assert (service.vault.root / "memory/memory-1.md").is_file()


def test_submit_memory_cannot_bypass_the_cas_supersession_api(service):
    """Catches a candidate claiming an accepted predecessor without its CAS generation."""
    initial = service.execute(
        _command(
            "submit_memory",
            {
                "authority": "personal",
                "body": "Accepted semantic body.",
                "kind": "knowledge",
                "memory_id": "memory-1",
                "portability": "personal-vault",
                "scope": {"type": "personal-global"},
            },
        )
    )
    assert initial.ok is True
    accepted = service.execute(
        _command(
            "promote_memory",
            {
                "expected_generation": 0,
                "memory_id": "memory-1",
                "storage_class": "portable",
            },
        )
    )
    assert accepted.ok is True

    bypass = service.execute(
        _command(
            "submit_memory",
            {
                "authority": "personal",
                "body": "Replacement without predecessor CAS.",
                "kind": "knowledge",
                "memory_id": "memory-2",
                "portability": "personal-vault",
                "scope": {"type": "personal-global"},
                "supersedes": "memory-1",
            },
        )
    )

    assert bypass.ok is False
    assert bypass.error.code == "invalid-artifact"
    assert not (service.vault.root / "memory/memory-2.md").exists()


def test_submit_memory_delegates_accepted_replacement_through_cas(service):
    """Catches Core making D3 supersession unreachable or bypassing its CAS guard."""
    from mneme.core.memories import MemoryStore

    original = service.execute(
        _command(
            "submit_memory",
            {
                "authority": "personal",
                "body": "The accepted semantic body.",
                "kind": "knowledge",
                "memory_id": "memory-1",
                "portability": "personal-vault",
                "scope": {"type": "personal-global"},
            },
        )
    )
    accepted = service.execute(
        _command(
            "promote_memory",
            {
                "expected_generation": 0,
                "memory_id": "memory-1",
                "storage_class": "portable",
            },
        )
    )

    replacement = service.execute(
        _command(
            "submit_memory",
            {
                "authority": "personal",
                "body": "The corrected semantic body.",
                "expected_generation": 1,
                "kind": "knowledge",
                "portability": "personal-vault",
                "scope": {"type": "personal-global"},
                "supersedes": "memory-1",
            },
        )
    )

    assert original.ok is accepted.ok is replacement.ok is True
    assert replacement.result["id"] != "memory-1"
    assert replacement.result["status"] == "candidate"
    assert [event.name for event in replacement.domain_events] == ["memory_submitted"]
    successor = MemoryStore(service.vault).read(replacement.result["id"])
    assert successor.supersedes == "memory-1"
    assert MemoryStore(service.vault).read("memory-1").superseded_by == successor.id


def test_head_and_source_commands_delegate_to_cas_registry_lifecycle(service):
    """Catches command handling replacing bounded registry mutations with direct writes."""
    from mneme.core.registries import RegistryStore

    RegistryStore(service.vault).create_workstream("ws-1", project=None, mode="parallel")
    first = service.execute(_command("create_session_revision", _checkpoint_payload()))
    second_payload = _checkpoint_payload(generation=1)
    second_payload["session_id"] = "s2"
    second = service.execute(_command("create_session_revision", second_payload))
    assert first.ok and second.ok

    selected = service.execute(
        _command(
            "set_active_heads",
            {
                "active_heads": [{"revision": "000001", "session": "s2"}],
                "expected_generation": 2,
                "storage_class": "portable",
                "workstream_id": "ws-1",
            },
        )
    )
    preferred = service.execute(
        _command(
            "set_preferred_head",
            {
                "expected_generation": 3,
                "preferred_head": {"revision": "000001", "session": "s2"},
                "storage_class": "portable",
                "workstream_id": "ws-1",
            },
        )
    )
    source = service.execute(
        _command(
            "register_source",
            {
                "authority": "external-reference",
                "kind": "filesystem",
                "source_id": "source-1",
                "storage_class": "portable",
            },
        )
    )

    registry = RegistryStore(service.vault).load_workstream("ws-1")
    assert selected.ok is True
    assert [event.name for event in selected.domain_events] == ["active_heads_changed"]
    assert preferred.ok is True
    assert [event.name for event in preferred.domain_events] == ["preferred_head_changed"]
    assert registry.mode == "preferred"
    assert registry.preferred_head.session == "s2"
    assert source.result == {
        "authority": "external-reference",
        "generation": 0,
        "id": "source-1",
        "kind": "filesystem",
        "project": None,
        "storage_class": "portable",
    }
    assert [event.name for event in source.domain_events] == ["source_registered"]


def test_set_active_heads_fails_closed_before_empty_removal_of_restricted_workstream(
    service,
):
    """Catches empty heads bypassing live workstream policy and clearing state."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import HeadRef, RegistryStore

    registries = RegistryStore(service.vault)
    registries.create_workstream("secret-workstream", project=None, mode="parallel")
    checkpoint = _checkpoint_payload()
    checkpoint["workstream_id"] = "secret-workstream"
    created = service.execute(_command("create_session_revision", checkpoint))
    restrictive = PolicyStore(service.vault).create_revision(
        "restricted-workstream",
        "1",
        {"ceiling": "local-only"},
        StorageClass.PORTABLE,
    )
    current = registries.load_workstream("secret-workstream")
    before = registries.update_workstream(
        current.with_policy_refs((restrictive,)),
        expected_generation=current.generation,
    )

    forbidden = service.execute(
        _command(
            "set_active_heads",
            {
                "active_heads": [],
                "expected_generation": before.generation,
                "workstream_id": "secret-workstream",
            },
        )
    )
    absent = service.execute(
        _command(
            "set_active_heads",
            {
                "active_heads": [],
                "expected_generation": before.generation,
                "workstream_id": "absent-workstream",
            },
        )
    )
    after = registries.load_workstream("secret-workstream")

    assert created.ok is True
    assert forbidden.ok is absent.ok is False
    assert forbidden.error.code == "policy-denied"
    assert forbidden.as_dict() == absent.as_dict()
    assert after == before
    assert after.generation == 2
    assert after.active_heads == (HeadRef("s1", "000001"),)
    encoded = json.dumps(forbidden.as_dict())
    assert "secret-workstream" not in encoded
    assert "restricted-workstream" not in encoded
    assert "local-only" not in encoded


def test_operational_source_failure_is_intrinsically_local_only(service):
    """Catches an availability observation becoming portable knowledge."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.contracts import OperationalEvent

    before = tuple(sorted(path.relative_to(service.vault.root) for path in service.vault.root.rglob("*")))
    event = OperationalEvent(
        version=1,
        name="source_unavailable",
        payload={"category": "source"},
    )

    assert event.storage_class is StorageClass.LOCAL_ONLY
    assert event.local_only is True
    assert event.as_dict() == {
        "name": "source_unavailable",
        "payload": {"category": "source"},
        "storage_class": "local-only",
        "version": 1,
    }
    assert tuple(sorted(path.relative_to(service.vault.root) for path in service.vault.root.rglob("*"))) == before


def test_policy_lifecycle_commands_delegate_to_official_store_apis(service, monkeypatch):
    """Catches command handling writing policy files or pointers around Task 10/11 APIs."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore

    calls: list[str] = []
    original_create = PolicyStore.create_revision
    original_activate = PolicyStore.activate
    original_project = RegistryStore.assign_project_policy
    original_source = RegistryStore.assign_source_policy

    def create(store, *args, **kwargs):
        calls.append("create_policy_revision")
        return original_create(store, *args, **kwargs)

    def activate(store, *args, **kwargs):
        calls.append("activate_policy_revision")
        return original_activate(store, *args, **kwargs)

    def assign_project(store, *args, **kwargs):
        calls.append("assign_project_policy")
        return original_project(store, *args, **kwargs)

    def assign_source(store, *args, **kwargs):
        calls.append("assign_source_policy")
        return original_source(store, *args, **kwargs)

    monkeypatch.setattr(PolicyStore, "create_revision", create)
    monkeypatch.setattr(PolicyStore, "activate", activate)
    monkeypatch.setattr(RegistryStore, "assign_project_policy", assign_project)
    monkeypatch.setattr(RegistryStore, "assign_source_policy", assign_source)

    registries = RegistryStore(service.vault)
    registries.register_project("project-1")
    registries.register_source("source-1", project="project-1")
    created = service.execute(
        _command(
            "create_policy_revision",
            {
                "policy_id": "project-policy",
                "revision": "1",
                "rule": {"ceiling": "personal-vault"},
                "storage_class": "portable",
            },
        )
    )
    policy_ref = created.result["policy_ref"]
    activated = service.execute(
        _command(
            "activate_policy_revision",
            {"expected_generation": 1, "policy_ref": policy_ref},
        )
    )
    assigned_project = service.execute(
        _command(
            "assign_project_policy",
            {
                "expected_generation": 0,
                "policy_ref": policy_ref,
                "project_id": "project-1",
                "storage_class": "portable",
            },
        )
    )
    assigned_source = service.execute(
        _command(
            "assign_source_policy",
            {
                "expected_generation": 0,
                "policy_ref": policy_ref,
                "source_id": "source-1",
                "storage_class": "portable",
            },
        )
    )

    assert all(result.ok for result in (created, activated, assigned_project, assigned_source))
    assert calls == [
        "create_policy_revision",
        "activate_policy_revision",
        "assign_project_policy",
        "assign_source_policy",
    ]
    assert PolicyStore(service.vault).load_active(
        "project-policy", StorageClass.PORTABLE
    ).digest == policy_ref["digest"]
    assert RegistryStore(service.vault).load_project("project-1").generation == 1
    assert RegistryStore(service.vault).load_source("source-1").generation == 1


def test_administrative_commands_preserve_cas_failure_semantics(service):
    """Catches stale policy and registry writes being flattened into success."""
    from mneme.core.registries import RegistryStore

    registries = RegistryStore(service.vault)
    registries.register_project("project-1")
    created = service.execute(
        _command(
            "create_policy_revision",
            {
                "policy_id": "project-policy",
                "revision": "1",
                "rule": {"ceiling": "personal-vault"},
                "storage_class": "portable",
            },
        )
    )
    policy_ref = created.result["policy_ref"]
    service.execute(
        _command(
            "activate_policy_revision",
            {"expected_generation": 1, "policy_ref": policy_ref},
        )
    )
    service.execute(
        _command(
            "assign_project_policy",
            {
                "expected_generation": 0,
                "policy_ref": policy_ref,
                "project_id": "project-1",
                "storage_class": "portable",
            },
        )
    )

    stale_policy = service.execute(
        _command(
            "activate_policy_revision",
            {"expected_generation": 1, "policy_ref": policy_ref},
        )
    )
    stale_project = service.execute(
        _command(
            "assign_project_policy",
            {
                "expected_generation": 0,
                "policy_ref": policy_ref,
                "project_id": "project-1",
                "storage_class": "portable",
            },
        )
    )

    assert stale_policy.ok is False
    assert stale_policy.error.code == "concurrent-write"
    assert stale_project.ok is False
    assert stale_project.error.code == "registry-conflict"
    assert stale_policy.domain_events == stale_project.domain_events == ()


def test_policy_commands_reject_raw_path_shortcuts_without_disclosure(service, tmp_path):
    """Catches a transport bypass that updates policy through a caller-supplied path."""
    secret = tmp_path / "confidential-policy.yaml"
    result = service.execute(
        _command(
            "create_policy_revision",
            {
                "path": str(secret),
                "policy_id": "project-policy",
                "revision": "1",
                "rule": {"ceiling": "personal-vault"},
                "storage_class": "portable",
            },
        )
    )

    assert result.ok is False
    assert result.error.code == "invalid-artifact"
    assert str(secret) not in json.dumps(result.as_dict())
    assert not secret.exists()
    assert not (service.vault.root / ".madi/policies/project-policy/1.yaml").exists()


def test_policy_command_preserves_canonical_json_lists_at_the_store_boundary(service):
    """Catches immutable command arrays becoming invalid non-JSON policy tuples."""
    result = service.execute(
        _command(
            "create_policy_revision",
            {
                "policy_id": "purpose-policy",
                "revision": "1",
                "rule": {
                    "ceiling": "personal-vault",
                    "purposes": ["context", "recall"],
                },
                "storage_class": "portable",
            },
        )
    )

    assert result.ok is True
    assert result.domain_events[0].name == "policy_revision_created"
    assert (service.vault.root / ".madi/policies/purpose-policy/1.yaml").is_file()


def test_policy_denial_is_closed_and_does_not_write_memory(service):
    """Catches canonical admission failure leaking content or becoming a candidate."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore

    policies = PolicyStore(service.vault)
    restrictive = policies.create_revision(
        "project-private", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries = RegistryStore(service.vault)
    project = registries.register_project("project-1")
    registries.assign_project_policy(
        project.id, restrictive, expected_generation=project.generation
    )

    result = service.execute(
        _command(
            "submit_memory",
            {
                "authority": "personal",
                "body": "confidential memory body",
                "kind": "knowledge",
                "memory_id": "memory-1",
                "portability": "personal-vault",
                "scope": {"type": "project", "project_id": "project-1"},
            },
        )
    )

    encoded = json.dumps(result.as_dict())
    assert result.ok is False
    assert result.error.code == "policy-denied"
    assert "confidential memory body" not in encoded
    assert "project-private" not in encoded
    assert list(service.vault.root.glob("memory/*.md")) == []


def test_submit_memory_fails_closed_for_an_unavailable_portable_source(service):
    """Catches a portable write skipping the policy of named unavailable evidence."""
    result = service.execute(
        _command(
            "submit_memory",
            {
                "authority": "personal",
                "body": "A lesson with source evidence.",
                "kind": "lesson",
                "memory_id": "memory-1",
                "portability": "personal-vault",
                "provenance": [
                    {
                        "kind": "id",
                        "storage_class": "portable",
                        "value": "unavailable-source",
                    }
                ],
                "scope": {"type": "personal-global"},
            },
        )
    )

    assert result.ok is False
    assert result.error.code == "invalid-artifact"
    assert list(service.vault.root.glob("memory/*.md")) == []
