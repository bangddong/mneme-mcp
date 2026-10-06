"""End-to-end conflict policy for concurrent agents and immutable lineages."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Lock, local

import pytest


def _revision_payload(session_id: str, state: str) -> dict[str, object]:
    return {
        "workstream_id": "parallel-work",
        "session_id": session_id,
        "storage_class": "portable",
        "expected_parent": None,
        "expected_registry_generation": 0,
        "body": {
            "adapter_id": "agent",
            "objective": "Preserve both concurrent agent checkpoints.",
            "current_state": state,
            "verified_facts": ["The checkpoint has one logical writer."],
            "completed_work": [],
            "blockers": [],
            "next_actions": ["Resolve preference explicitly if needed."],
            "source_refs": [],
        },
        "relations": [],
    }


def test_disjoint_checkpoint_race_retries_and_preference_race_conflicts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Dropping a disjoint head or silently choosing a preferred head breaks D3."""
    from mneme.core.contracts import CoreCommand
    from mneme.core.doctor import Doctor
    from mneme.core.registries import HeadRef, RegistryStore
    from mneme.core.service import CoreService
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    RegistryStore(vault).create_workstream(
        "parallel-work", project=None, mode="parallel"
    )
    checkpoint_barrier = Barrier(2)
    preference_barrier = Barrier(2)
    attempt_lock = Lock()
    update_attempts: list[tuple[str, int]] = []
    preference_attempts: list[tuple[str, int]] = []
    thread_state = local()
    original_update = RegistryStore.update_workstream

    def synchronized_update(self, registry, *, expected_generation):
        session_id = getattr(thread_state, "session_id", None)
        if session_id is not None and registry.id == "parallel-work":
            with attempt_lock:
                update_attempts.append((session_id, expected_generation))
            if (
                expected_generation == 0
                and not getattr(thread_state, "synchronized", False)
            ):
                thread_state.synchronized = True
                checkpoint_barrier.wait(timeout=10)
        if getattr(thread_state, "preference_race", False):
            assert registry.preferred_head is not None
            with attempt_lock:
                preference_attempts.append(
                    (registry.preferred_head.session, expected_generation)
                )
            preference_barrier.wait(timeout=10)
        return original_update(
            self, registry, expected_generation=expected_generation
        )

    monkeypatch.setattr(RegistryStore, "update_workstream", synchronized_update)

    def checkpoint(session_id: str):
        thread_state.session_id = session_id
        return CoreService(vault).execute(
            CoreCommand(
                1,
                "create_session_revision",
                _revision_payload(session_id, f"{session_id} independent state"),
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(checkpoint, ("agent-alpha", "agent-beta"))
        )

    assert all(result.ok for result in results), (
        results, update_attempts, checkpoint_barrier.broken
    )
    assert {result.result["session_id"] for result in results} == {
        "agent-alpha",
        "agent-beta",
    }
    first_attempts = [attempt for attempt in update_attempts if attempt[1] == 0]
    retry_attempts = [attempt for attempt in update_attempts if attempt[1] != 0]
    assert set(first_attempts) == {
        ("agent-alpha", 0),
        ("agent-beta", 0),
    }
    assert len(first_attempts) == 2
    assert len(retry_attempts) == 1
    assert retry_attempts[0][0] in {"agent-alpha", "agent-beta"}
    assert retry_attempts[0][1] == 1
    registry = RegistryStore(vault).load_workstream("parallel-work")
    assert registry.generation == 2
    assert set(registry.active_heads) == {
        HeadRef("agent-alpha", "000001"),
        HeadRef("agent-beta", "000001"),
    }
    assert registry.mode == "parallel"
    assert registry.preferred_head is None
    assert not any(issue.code == "session-orphan" for issue in Doctor(vault).run().issues)

    service = CoreService(vault)
    service.reindex()
    view = service.context("parallel-work", "portable")
    assert view.effective_status.value == "divergent"
    assert "agent-alpha independent state" in view.text
    assert "agent-beta independent state" in view.text

    def choose_preferred_head(session_id: str):
        thread_state.preference_race = True
        try:
            return CoreService(vault).execute(
                CoreCommand(
                    1,
                    "set_preferred_head",
                    {
                        "workstream_id": "parallel-work",
                        "preferred_head": {
                            "session": session_id,
                            "revision": "000001",
                        },
                        "expected_generation": registry.generation,
                        "storage_class": "portable",
                    },
                )
            )
        finally:
            del thread_state.preference_race

    with ThreadPoolExecutor(max_workers=2) as executor:
        preference_results = tuple(
            executor.map(choose_preferred_head, ("agent-alpha", "agent-beta"))
        )

    successful_preferences = tuple(result for result in preference_results if result.ok)
    conflicted_preferences = tuple(
        result for result in preference_results if not result.ok
    )
    assert len(successful_preferences) == 1
    assert len(conflicted_preferences) == 1
    assert conflicted_preferences[0].error.code == "registry-conflict"
    assert set(preference_attempts) == {
        ("agent-alpha", registry.generation),
        ("agent-beta", registry.generation),
    }
    final = RegistryStore(vault).load_workstream("parallel-work")
    assert final.preferred_head == HeadRef(
        successful_preferences[0].result["preferred_head"]["session"],
        "000001",
    )


def test_same_lineage_race_never_overwrites_or_merges_semantic_bodies(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Two writers targeting one revision must leave exactly one immutable body."""
    from mneme.core.contracts import CoreCommand
    from mneme.core.registries import HeadRef, RegistryStore
    from mneme.core.service import CoreService
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    RegistryStore(vault).create_workstream(
        "parallel-work", project=None, mode="parallel"
    )
    first_waiting = Event()
    release_first = Event()
    original_add = RegistryStore.add_active_head

    def hold_first_head_advance(self, workstream_id, head, observed_base, validator):
        if head == HeadRef("shared-lineage", "000001"):
            first_waiting.set()
            assert release_first.wait(timeout=10)
        return original_add(self, workstream_id, head, observed_base, validator)

    monkeypatch.setattr(RegistryStore, "add_active_head", hold_first_head_advance)
    first_command = CoreCommand(
        1,
        "create_session_revision",
        _revision_payload("shared-lineage", "first writer semantic body"),
    )
    second_command = CoreCommand(
        1,
        "create_session_revision",
        _revision_payload("shared-lineage", "second writer semantic body"),
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        first_future = executor.submit(CoreService(vault).execute, first_command)
        assert first_waiting.wait(timeout=10)
        revision_path = (
            vault.root
            / "workstreams/parallel-work/sessions/shared-lineage/000001.md"
        )
        before = revision_path.read_bytes()
        second = CoreService(vault).execute(second_command)
        after_second = revision_path.read_bytes()
        release_first.set()
        first = first_future.result(timeout=10)

    assert first.ok is True
    assert second.ok is False
    assert second.error.code == "invalid-artifact"
    assert after_second == before == revision_path.read_bytes()
    text = before.decode("utf-8")
    assert "first writer semantic body" in text
    assert "second writer semantic body" not in text
    assert list(revision_path.parent.glob("*.md")) == [revision_path]
    registry = RegistryStore(vault).load_workstream("parallel-work")
    assert registry.active_heads == (HeadRef("shared-lineage", "000001"),)


def test_structural_retry_rejects_removal_and_non_head_field_changes(tmp_path):
    """The bounded retry may union additions only; lifecycle/removal changes conflict."""
    from mneme.core.registries import HeadRef, RegistryConflict, RegistryStore
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    store = RegistryStore(vault)
    status_base = store.create_workstream(
        "status-work", project=None, mode="parallel"
    )
    store.update_workstream(
        status_base.with_status("paused"),
        expected_generation=status_base.generation,
    )
    with pytest.raises(RegistryConflict, match="outside proven-disjoint"):
        store.add_active_head(
            "status-work",
            HeadRef("new-agent", "000001"),
            status_base,
            lambda _head: True,
        )

    removal_base = store.create_workstream(
        "removal-work", project=None, mode="parallel"
    )
    with_head = store.add_active_head(
        "removal-work",
        HeadRef("existing-agent", "000001"),
        removal_base,
        lambda _head: True,
    )
    store.update_workstream(
        replace(with_head, active_heads=()),
        expected_generation=with_head.generation,
    )
    with pytest.raises(RegistryConflict, match="outside proven-disjoint"):
        store.add_active_head(
            "removal-work",
            HeadRef("new-agent", "000001"),
            with_head,
            lambda _head: True,
        )
