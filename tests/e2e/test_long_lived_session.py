"""End-to-end continuity across repeated compaction and an agent handoff."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


_LOCAL_DIRECTORIES = (
    "bindings",
    "overlays",
    "evidence",
    "pending",
    "views",
    "index",
    "cache",
    "locks",
    "logs",
)


def _git(cwd: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_EMAIL": "continuity@example.invalid",
            "GIT_AUTHOR_NAME": "Madi continuity gate",
            "GIT_COMMITTER_EMAIL": "continuity@example.invalid",
            "GIT_COMMITTER_NAME": "Madi continuity gate",
        }
    )
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )
    return completed.stdout.strip()


def _prepare_state_home(vault_root: Path, state_home: Path) -> None:
    from mneme.core.fs import read_yaml

    vault_id = read_yaml(vault_root / ".madi" / "vault.yaml")["id"]
    local_root = state_home / "vaults" / vault_id
    for relative in _LOCAL_DIRECTORIES:
        (local_root / relative).mkdir(parents=True, exist_ok=True)


def _checkpoint_payload(
    *,
    adapter: str,
    session_id: str,
    generation: int,
    parent: str | None,
    state: str,
    relations: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "workstream_id": "long-lived",
        "session_id": session_id,
        "storage_class": "portable",
        "expected_parent": (
            None
            if parent is None
            else {"session": session_id, "revision": parent}
        ),
        "expected_registry_generation": generation,
        "body": {
            "adapter_id": adapter,
            "objective": "Carry explicit semantic state across compaction and agents.",
            "current_state": state,
            "verified_facts": ["Every checkpoint is self-contained."],
            "completed_work": [f"Persisted {state}."],
            "blockers": [],
            "next_actions": ["Resume from the explicit active head."],
            "source_refs": [],
        },
        "relations": [] if relations is None else relations,
    }


def _write_checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _envelope(
    event: str, adapter: str, session_id: str, checkpoint: Path
) -> dict[str, object]:
    return {
        "version": 1,
        "event": event,
        "adapter": adapter,
        "session_id": session_id,
        "workstream_id": "long-lived",
        "checkpoint_file": str(checkpoint),
    }


def test_three_compacts_handoff_and_clone_resume_without_session_end(tmp_path):
    """Losing revisions, inferring preference, or requiring history breaks continuity."""
    from mneme.adapters.claude import ClaudeAdapter
    from mneme.adapters.codex import CodexAdapter
    from mneme.core.contracts import CoreCommand, LifecycleEvent
    from mneme.core.registries import HeadRef, RegistryStore
    from mneme.core.service import CoreService
    from mneme.core.sessions import SessionRevisionRef, SessionStore
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    registries = RegistryStore(vault)
    project_registry = registries.register_project("continuity-project")
    registries.register_source(
        "continuity-source", project=project_registry.id
    )
    registries.create_workstream(
        "long-lived", project=project_registry.id, mode="parallel"
    )
    project = tmp_path / "project"
    project.mkdir()
    service = CoreService(vault)
    claude = ClaudeAdapter(service, project_root=project)

    first_path = project / ".madi" / "checkpoints" / "claude-000001.json"
    _write_checkpoint(
        first_path,
        _checkpoint_payload(
            adapter="claude",
            session_id="claude-main",
            generation=0,
            parent=None,
            state="Claude revision 000001 before any compact.",
        ),
    )
    first = claude.handle(
        _envelope("milestone_reached", "claude", "claude-main", first_path)
    )
    assert first.ok is True
    assert first.command_result.result["revision"] == "000001"

    compact_results = []
    for number in range(2, 5):
        relations: list[dict[str, object]] = []
        if number == 4:
            relations.append(
                {
                    "kind": "handoff",
                    "target": "codex",
                    "purpose": "Continue the same work in a Codex session.",
                    "required_context": "Use Claude revision 000004.",
                    "next_action": "Open a distinct Codex lineage.",
                    "provenance_refs": [],
                }
            )
        checkpoint = (
            project / ".madi" / "checkpoints" / f"claude-{number:06d}.json"
        )
        _write_checkpoint(
            checkpoint,
            _checkpoint_payload(
                adapter="claude",
                session_id="claude-main",
                generation=number - 1,
                parent=f"{number - 1:06d}",
                state=f"Claude revision {number:06d} after compact {number - 1}.",
                relations=relations,
            ),
        )
        result = claude.handle(
            _envelope("pre_compact", "claude", "claude-main", checkpoint)
        )
        assert result.ok is True
        assert result.block_host is False
        compact_results.append(result)

    assert [result.event for result in compact_results] == ["pre_compact"] * 3
    assert [
        result.command_result.result["revision"] for result in compact_results
    ] == ["000002", "000003", "000004"]
    assert compact_results[-1].domain_events[-1].name == "handoff_recorded"

    codex_path = project / ".madi" / "checkpoints" / "codex-000001.json"
    _write_checkpoint(
        codex_path,
        _checkpoint_payload(
            adapter="codex",
            session_id="codex-main",
            generation=4,
            parent=None,
            state="Codex continued from the explicit Claude handoff.",
            relations=[
                {
                    "kind": "continues_from",
                    "target": "claude-main@000004",
                    "purpose": "Continue the same work with Codex.",
                    "required_context": "Use the selected Claude revision.",
                    "next_action": "Resume in the distinct Codex session.",
                    "provenance_refs": [],
                }
            ],
        ),
    )
    switched = CodexAdapter(service, project_root=project).handle(
        _envelope("agent_switched", "codex", "codex-main", codex_path)
    )
    assert switched.ok is True
    assert switched.block_host is False

    before_preference = RegistryStore(vault).load_workstream("long-lived")
    assert before_preference.mode == "parallel"
    assert before_preference.preferred_head is None
    assert set(before_preference.active_heads) == {
        HeadRef("claude-main", "000004"),
        HeadRef("codex-main", "000001"),
    }
    preferred = service.execute(
        CoreCommand(
            1,
            "set_preferred_head",
            {
                "workstream_id": "long-lived",
                "preferred_head": {
                    "session": "codex-main",
                    "revision": "000001",
                },
                "expected_generation": before_preference.generation,
                "storage_class": "portable",
            },
        )
    )
    assert preferred.ok is True

    submitted = service.execute(
        CoreCommand(
            1,
            "submit_memory",
            {
                "memory_id": "handoff-preference",
                "kind": "preference",
                "scope": {
                    "type": "workstream",
                    "workstream_id": "long-lived",
                },
                "authority": "personal",
                "portability": "personal-vault",
                "storage_class": "portable",
                "body": "Prefer explicit cross-agent handoffs.",
            },
        )
    )
    assert submitted.ok is True
    promoted = service.execute(
        CoreCommand(
            1,
            "promote_memory",
            {
                "memory_id": "handoff-preference",
                "expected_generation": 0,
                "storage_class": "portable",
            },
        )
    )
    assert promoted.ok is True

    claude_final = SessionStore(vault).read_revision(
        SessionRevisionRef("claude-main", "000004"),
        workstream_id="long-lived",
    )
    codex_first = SessionStore(vault).read_revision(
        SessionRevisionRef("codex-main", "000001"),
        workstream_id="long-lived",
    )
    assert claude_final.relations[0].kind == "handoff"
    assert codex_first.relations[0].kind == "continues_from"
    assert codex_first.relations[0].target == "claude-main@000004"

    (vault.root / ".gitattributes").write_text(
        ".madi/** text eol=lf\n"
        "projects/** text eol=lf\n"
        "sources/** text eol=lf\n"
        "workstreams/** text eol=lf\n"
        "memory/** text eol=lf\n",
        encoding="utf-8",
    )
    _git(vault.root, "init", "-b", "main")
    _git(vault.root, "add", ".")
    _git(vault.root, "commit", "-m", "checkpoint: synced portable continuity")
    remote = tmp_path / "continuity.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(vault.root, "remote", "add", "origin", str(remote))
    _git(vault.root, "push", "-u", "origin", "main")
    clone = tmp_path / "second-machine"
    _git(
        tmp_path,
        "clone",
        "--depth",
        "1",
        "--branch",
        "main",
        remote.as_uri(),
        str(clone),
    )

    second_state = tmp_path / "second-state"
    _prepare_state_home(clone, second_state)
    second_vault = Vault.open(clone, second_state)
    second_service = CoreService(second_vault)
    second_service.reindex()
    observed = second_service.observe(
        LifecycleEvent(1, "session_resumed", "codex", "codex-main", "long-lived")
    )
    resumed = second_service.execute(
        CoreCommand(
            1,
            "open_session",
            {
                "session_id": "codex-main",
                "workstream_id": "long-lived",
                "storage_class": "portable",
            },
        )
    )
    context = second_service.execute(
        CoreCommand(
            1,
            "get_context",
            {"workstream_id": "long-lived", "mode": "portable"},
        )
    )
    profile = second_service.profile(
        {"type": "workstream", "workstream_id": "long-lived"}
    )

    assert _git(clone, "rev-list", "--count", "HEAD") == "1"
    assert observed.context_required is True
    assert observed.checkpoint_required is False
    assert resumed.ok is True
    assert resumed.result["active_head"] == {
        "session": "codex-main",
        "revision": "000001",
    }
    assert context.ok is True
    assert "Codex continued from the explicit Claude handoff." in context.result["text"]
    assert "Prefer explicit cross-agent handoffs." in profile.text
    assert len(list(clone.glob("workstreams/long-lived/sessions/claude-main/*.md"))) == 4
    assert len(list(clone.glob("workstreams/long-lived/sessions/codex-main/*.md"))) == 1
    canonical_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in clone.rglob("*")
        if path.is_file() and path.suffix in {".md", ".yaml"}
    )
    assert "session_ended" not in canonical_text
