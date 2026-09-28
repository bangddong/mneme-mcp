"""Public continuation projections and cache consistency regressions."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from mneme.core.artifacts import StorageClass
from mneme.core.contracts import CoreCommand
from mneme.core.registries import RegistryStore
from mneme.core.service import CoreService
from mneme.core.vault import Vault
from mneme.core.fs import read_yaml


@pytest.fixture
def service(tmp_path):
    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-1")
    RegistryStore(vault).create_workstream("ws", project=None, mode="parallel")
    return CoreService(vault)


def checkpoint(service, session="session", workstream="ws", storage="portable", generation=0):
    result = service.execute(CoreCommand(1, "create_session_revision", {
        "session_id": session, "workstream_id": workstream, "storage_class": storage,
        "expected_parent": None, "expected_registry_generation": generation,
        "body": {"adapter_id": "host-agent", "objective": f"Objective {session}",
            "current_state": "Ready to continue", "verified_facts": ["Verified decision"],
            "completed_work": ["Completed verification"], "blockers": ["Remaining blocker"],
            "next_actions": ["Concrete next action"], "source_refs": []},
        "relations": [{"kind": "handoff", "target": "next-agent", "purpose": "Review result",
            "required_context": "Context to transfer", "next_action": "Inspect evidence", "provenance_refs": []}]}))
    assert result.ok, result


def test_current_renders_resume_fields_and_authorized_accepted_memory(service):
    checkpoint(service)
    remembered = service.execute(CoreCommand(1, "submit_memory", {"memory_id": "memory", "kind": "lesson",
        "scope": {"type": "personal-global"}, "authority": "personal", "portability": "personal-vault",
        "body": "Applicable accepted lesson"}))
    assert remembered.ok
    assert service.execute(CoreCommand(1, "promote_memory", {"memory_id": "memory", "expected_generation": 0})).ok
    service.reindex()
    view = service.context("ws")
    for value in ("session", "000001", "host-agent", "Verified decision", "Completed verification",
                  "Remaining blocker", "Concrete next action", "Context to transfer", "Inspect evidence",
                  "Applicable accepted lesson", "vault-default", "semantic_hash", "timestamp"):
        assert value in view.text
    assert len(view.text) <= 65536


def test_current_bounds_large_policy_provenance():
    from mneme.core.context import ContextReaders, render_current
    from mneme.core.policy import PolicyEvaluation, PolicyRef, Portability
    from mneme.core.registries import HeadRef, WorkstreamRegistry
    from mneme.core.resolver import SessionRevision
    from mneme.core.sessions import CheckpointRequest, SessionBody, session_semantic_hash

    head = HeadRef("session", "000001")
    body = SessionBody("host-agent", "Objective", "Current", (), (), (), (), ())
    receipt = PolicyEvaluation(
        Portability.PERSONAL_VAULT,
        Portability.PERSONAL_VAULT,
        True,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        "test",
        tuple(
            sorted(
                (
                    PolicyRef(f"policy-{index}", "1", f"{index:064x}")
                    for index in range(1000)
                ),
                key=lambda ref: ref.policy_id,
            )
        ),
        session_semantic_hash(body, ()),
    )
    revision = SessionRevision(
        head,
        CheckpointRequest(
            "ws",
            head.session,
            StorageClass.PORTABLE,
            None,
            0,
            body,
            (),
            receipt,
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    registry = WorkstreamRegistry(
        "ws", 0, None, "active", "single", (head,)
    )
    readers = ContextReaders(
        lambda _workstream: registry,
        lambda _workstream, _head: revision,
        authorize_revision=lambda *_args: True,
    )

    view = render_current(readers, "ws", "portable")

    assert len(view.text) <= 65536


def test_current_bounds_large_adapter_identity():
    from mneme.core.context import ContextReaders, render_current
    from mneme.core.policy import PolicyEvaluation, Portability
    from mneme.core.registries import HeadRef, WorkstreamRegistry
    from mneme.core.resolver import SessionRevision
    from mneme.core.sessions import CheckpointRequest, SessionBody, session_semantic_hash

    adapter = "a" * 70000
    body = SessionBody(adapter, "Objective", "Current", (), (), (), (), ())
    head = HeadRef("session", "000001")
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    receipt = PolicyEvaluation(
        Portability.PERSONAL_VAULT, Portability.PERSONAL_VAULT, True,
        timestamp, "test", (), session_semantic_hash(body, ()),
    )
    revision = SessionRevision(
        head,
        CheckpointRequest(
            "ws", head.session, StorageClass.PORTABLE, None, 0, body, (), receipt, timestamp,
        ),
    )
    registry = WorkstreamRegistry("ws", 0, None, "active", "single", (head,))
    readers = ContextReaders(
        lambda _workstream: registry,
        lambda _workstream, _head: revision,
        authorize_revision=lambda *_args: True,
    )
    views = [render_current(readers, "ws", mode) for mode in ("portable", "effective-local")]

    assert all(len(view.text) <= 65536 for view in views), [len(view.text) for view in views]
    for view in views:
        assert adapter not in view.text
        assert "adapter: " + "a" * 256 + " … [bounded; inspect artifact]; timestamp:" in view.text
        for section in (
            "Objective: Objective", "Current state: Current", "### Verified facts / decisions",
            "### Completed work / verification", "### Blockers / risks", "### Next actions",
            "### Source references", "### Relations / handoffs", "provenance / policy_receipt:",
        ):
            assert section in view.text
        assert render_current(readers, "ws", view.mode).text == view.text


def test_preferred_head_change_binds_authorization_and_text(service):
    checkpoint(service, "first")
    checkpoint(service, "second", generation=1)
    store = RegistryStore(service.vault)
    registry = store.load_workstream("ws")
    registry = store.update_workstream(replace(registry, mode="preferred", preferred_head=registry.active_heads[0]), expected_generation=2)
    service.reindex()
    first_key = service.authorizer.authorize_context_view("ws").authorization_fingerprint
    first_text = service.context("ws").text
    store.update_workstream(replace(registry, preferred_head=registry.active_heads[1]), expected_generation=3)
    second_key = service.authorizer.authorize_context_view("ws").authorization_fingerprint
    second = service.context("ws")
    assert first_key != second_key
    assert first_text != second.text
    assert second.portable.selected_head.session == "second"


@pytest.mark.parametrize("forge_hash", [False, True])
def test_tampered_cached_text_cannot_replace_fresh_projection(service, forge_hash):
    from hashlib import sha256
    from mneme.core.fs import dump_yaml

    checkpoint(service)
    service.reindex()
    expected = service.context("ws").text
    target = service.vault.local_root / "views/CURRENT.md"
    metadata_path = service.vault.local_root / "views/CURRENT.md.authorization.yaml"
    metadata = read_yaml(metadata_path)
    target.write_text("UNAUTHORIZED_CACHE_CONTENT", encoding="utf-8")
    if forge_hash:
        metadata["content_hash"] = sha256(target.read_bytes()).hexdigest()
        metadata_path.write_text(dump_yaml(metadata), encoding="utf-8")
    else:
        assert service.vault.views.load("CURRENT.md", authorization_fingerprint=metadata["authorization_fingerprint"]) is None
    assert service.context("ws").text == expected


def test_interrupted_cache_write_fails_closed_on_old_metadata(service, monkeypatch):
    from mneme.core import storage
    from mneme.core.security import PolicyDecision

    checkpoint(service)
    service.reindex()
    service.context("ws")
    metadata = read_yaml(service.vault.local_root / "views/CURRENT.md.authorization.yaml")
    decision = PolicyDecision(True, (), metadata["authorization_fingerprint"])
    original = storage.replace_text

    def fail_metadata(path, text):
        if path.name.endswith(".authorization.yaml"):
            raise OSError("interrupted metadata write")
        return original(path, text)

    monkeypatch.setattr(storage, "replace_text", fail_metadata)
    with pytest.raises(OSError, match="interrupted"):
        service.vault.views.write("CURRENT.md", "Unpaired replacement", authorization=decision)
    assert service.vault.views.load("CURRENT.md", authorization=decision) is None


def test_divergent_current_keeps_each_resume_projection(service):
    checkpoint(service, "first")
    checkpoint(service, "second", generation=1)
    view = service.context("ws")
    assert view.portable.selected_head is None
    assert "Objective first" in view.text and "Objective second" in view.text
    assert view.text.count("Concrete next action") == 2


def test_local_project_binding_selects_matching_workstream(service, tmp_path, monkeypatch):
    registries = RegistryStore(service.vault)
    registries.register_project("bound-project")
    registries.register_source("bound-source", project="bound-project")
    registries.create_workstream("bound-ws", project="bound-project", mode="parallel")
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    registries.bind_source("bound-source", mounted)
    monkeypatch.chdir(mounted)
    assert service.context().workstream_id == "bound-ws"


def test_public_effective_local_omits_invalid_policy_overlay(service, capsys):
    from mneme.cli import main
    from mneme.core.policy import PolicyStore
    from mneme.transports.mcp_stdio import create_app

    checkpoint(service)
    policies = PolicyStore(service.vault)
    ref = policies.create_revision("overlay-policy", "1", {"ceiling": "local-only"}, StorageClass.LOCAL_ONLY)
    local = RegistryStore(service.vault, StorageClass.LOCAL_ONLY)
    local.create_workstream("local-ws", project=None, mode="parallel", overlay_of="ws", policy_refs=(ref,))
    checkpoint(service, "private-session", "local-ws", "local-only")
    service.reindex()
    assert "Objective private-session" in service.context("ws", "effective-local").text
    policy_path = service.vault.local_root / "overlays/.madi/policies/overlay-policy/1.yaml"
    policy_path.write_text("invalid policy", encoding="utf-8")

    view = service.context("ws", "effective-local")
    assert view.overlay_status.value == "invalid"
    assert view.status.value == "degraded"
    assert "Objective private-session" not in view.text
    assert "Objective session" in view.text
    tool = asyncio.run(create_app(service).get_tool("madi_context"))
    assert "private-session" not in tool.fn("ws", "effective-local")["result"]["text"]
    assert main(["--vault", str(service.vault.root), "--state-home", str(service.vault.state_home),
        "context", "--workstream", "ws", "--mode", "effective-local"]) == 0
    assert "private-session" not in json.loads(capsys.readouterr().out)["result"]["text"]


def test_optional_index_change_does_not_restore_stale_status(service):
    checkpoint(service)
    service.reindex()
    assert service.context("ws").status.value == "resolved"
    service.index.db_path.unlink()
    degraded = service.context("ws")
    assert degraded.status.value == "degraded"
    assert "status: degraded" in degraded.text


def test_cache_fingerprint_tracks_optional_index_state(service):
    checkpoint(service)
    service.reindex()
    service.context("ws")
    metadata = service.vault.local_root / "views/CURRENT.md.authorization.yaml"
    before = read_yaml(metadata)["authorization_fingerprint"]
    service.index.db_path.unlink()
    service.context("ws")
    after = read_yaml(metadata)["authorization_fingerprint"]
    assert after != before


def test_cache_fingerprint_tracks_rendered_accepted_memories(service):
    checkpoint(service)
    service.reindex()
    service.context("ws")
    metadata = service.vault.local_root / "views/CURRENT.md.authorization.yaml"
    before = read_yaml(metadata)["authorization_fingerprint"]
    remembered = service.execute(CoreCommand(1, "submit_memory", {
        "memory_id": "memory", "kind": "lesson", "scope": {"type": "personal-global"},
        "authority": "personal", "portability": "personal-vault", "body": "New lesson"}))
    assert remembered.ok
    assert service.execute(CoreCommand(1, "promote_memory", {
        "memory_id": "memory", "expected_generation": 0})).ok
    service.context("ws")
    after = read_yaml(metadata)["authorization_fingerprint"]
    assert after != before


def test_cache_fingerprint_tracks_memory_live_authorization_inputs(service):
    from mneme.core.artifacts import ArtifactReference, ReferenceKind
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability

    checkpoint(service)
    service.reindex()
    registries = RegistryStore(service.vault)
    source = registries.register_source("memory-source")
    policies = PolicyStore(service.vault)
    source_policy = policies.create_revision(
        "memory-source-policy",
        "1",
        {"ceiling": "personal-vault"},
        StorageClass.PORTABLE,
    )
    registries.assign_source_policy(source.id, source_policy, expected_generation=0)
    provenance = (
        ArtifactReference(
            ReferenceKind.ID,
            StorageClass.PORTABLE,
            source.id,
            target_family="source",
        ),
    )
    body = "Source-bound accepted lesson"
    semantic = memory_semantic_hash(body=body, provenance=provenance)
    default = policies.load_rule(
        policies.load_active("vault-default", StorageClass.PORTABLE)
    )
    evaluation = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (default, policies.load_rule(source_policy)),
        semantic,
    )
    memories = MemoryStore(service.vault)
    record = memories.submit_candidate(
        "knowledge",
        {"type": "personal-global"},
        "personal",
        "personal-vault",
        body,
        evaluation,
        provenance=provenance,
        memory_id="source-bound-memory",
    )
    memories.promote(record.id, 0, evaluation)
    service.context("ws")
    metadata = service.vault.local_root / "views/CURRENT.md.authorization.yaml"
    before = read_yaml(metadata)["authorization_fingerprint"]

    replacement = policies.create_revision(
        source_policy.policy_id,
        "2",
        {"ceiling": "personal-vault", "note": "still allowed"},
        StorageClass.PORTABLE,
    )
    registries.assign_source_policy(source.id, replacement, expected_generation=1)
    service.context("ws")

    assert read_yaml(metadata)["authorization_fingerprint"] != before


def test_effective_local_cache_fingerprint_tracks_overlay_relation(service):
    checkpoint(service)
    service.reindex()
    service.context("ws", "effective-local")
    metadata = service.vault.local_root / "views/CURRENT.md.authorization.yaml"
    before = read_yaml(metadata)["authorization_fingerprint"]
    local = RegistryStore(service.vault, StorageClass.LOCAL_ONLY)
    local.create_workstream("local-ws", project=None, mode="parallel", overlay_of="ws")
    checkpoint(service, "local-session", "local-ws", "local-only")
    service.context("ws", "effective-local")
    after = read_yaml(metadata)["authorization_fingerprint"]
    assert after != before


def test_public_overlay_survives_reopen_and_is_invisible_to_portable(service, capsys):
    from mneme.cli import main
    from mneme.transports.mcp_stdio import create_app

    checkpoint(service)
    local = RegistryStore(service.vault, StorageClass.LOCAL_ONLY)
    local.create_workstream("local-ws", project=None, mode="parallel", overlay_of="ws")
    checkpoint(service, "local-session", "local-ws", "local-only")
    service.reindex()
    reopened = CoreService(Vault.open(service.vault.root, service.vault.state_home))
    view = reopened.context("ws", "effective-local")
    assert view.overlay is not None
    assert "Objective local-session" in view.text
    assert "Portable base" in view.text and "Local foreground" in view.text
    assert "local-session" not in reopened.context("ws", "portable").text
    assert not any("local-ws" in path.read_text(encoding="utf-8") for path in service.vault.root.rglob("*.yaml"))
    app = create_app(reopened)
    tool = asyncio.run(app.get_tool("madi_context"))
    assert "Objective local-session" in tool.fn("ws", "effective-local")["result"]["text"]
    assert main(["--vault", str(service.vault.root), "--state-home", str(service.vault.state_home),
        "context", "--workstream", "ws", "--mode", "effective-local"]) == 0
    assert "Objective local-session" in json.loads(capsys.readouterr().out)["result"]["text"]


def test_no_id_context_selects_sole_workstream_through_core_cli_mcp(service, capsys):
    from mneme.cli import main
    from mneme.transports.mcp_stdio import create_app

    checkpoint(service)
    assert service.context().workstream_id == "ws"
    assert service.execute(CoreCommand(1, "get_context", {})).result["workstream_id"] == "ws"
    tool = asyncio.run(create_app(service).get_tool("madi_context"))
    assert tool.fn()["result"]["workstream_id"] == "ws"
    assert main(["--vault", str(service.vault.root), "--state-home", str(service.vault.state_home), "context"]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["workstream_id"] == "ws"


def test_ambiguous_context_returns_deterministic_overview(service):
    RegistryStore(service.vault).create_workstream("another", project=None, mode="parallel")
    view = service.context()
    assert view.workstream_id == "overview"
    assert "explicit" in view.text.lower()
    assert view.text.index("another") < view.text.index("ws")


def test_cli_json_remains_utf8_decodable_under_windows_pipe_encoding(service):
    import os
    from pathlib import Path
    import subprocess
    import sys

    checkpoint(service)
    submitted = service.execute(CoreCommand(1, "submit_memory", {
        "memory_id": "unicode-memory", "kind": "lesson", "scope": {"type": "personal-global"},
        "authority": "personal", "portability": "personal-vault", "body": "한글 continuation",
    }))
    assert submitted.ok
    assert service.execute(CoreCommand(1, "promote_memory", {"memory_id": "unicode-memory", "expected_generation": 0})).ok
    environment = dict(os.environ, PYTHONIOENCODING="cp949")
    completed = subprocess.run([
        sys.executable, "-m", "mneme.cli", "--vault", str(service.vault.root),
        "--state-home", str(service.vault.state_home), "context", "--workstream", "ws",
    ], cwd=Path(__file__).parents[2], env=environment, capture_output=True)

    assert completed.returncode == 0
    payload = json.loads(completed.stdout.decode("utf-8"))
    assert "Objective session" in payload["result"]["text"]
    assert "한글 continuation" in payload["result"]["text"]
