import json
from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def _arguments(vault, *command: str) -> list[str]:
    return [
        "--vault-root",
        str(vault.root),
        "--state-home",
        str(vault.state_home),
        *command,
    ]


def _invoke(capsys, arguments: list[str]):
    from mneme.cli import main

    exit_code = main(arguments)
    captured = capsys.readouterr()
    stdout = json.loads(captured.out) if captured.out else None
    stderr = json.loads(captured.err) if captured.err else None
    return exit_code, stdout, stderr


def _checkpoint_payload(path: Path, *, generation: int = 0) -> None:
    path.write_text(
        json.dumps(
            {
                "workstream_id": "ws-1",
                "session_id": "session-1",
                "storage_class": "portable",
                "expected_parent": None,
                "expected_registry_generation": generation,
                "body": {
                    "adapter_id": "host-agent",
                    "objective": "한글 목표를 보존한다",
                    "current_state": "검증 가능한 상태",
                    "verified_facts": ["UTF-8 입력"],
                    "completed_work": [],
                    "blockers": [],
                    "next_actions": ["계속 진행"],
                    "source_refs": [],
                },
                "relations": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_status_returns_one_json_success_envelope(vault, capsys):
    exit_code, stdout, stderr = _invoke(capsys, _arguments(vault, "status"))

    assert exit_code == 0
    assert stderr is None
    assert stdout == {
        "command": "status",
        "ok": True,
        "result": {
            "issue_counts": {"degraded": 1, "invalid": 0},
            "vault_id": vault.id,
        },
        "status": "degraded",
    }


def test_doctor_is_a_successful_diagnosis_even_when_state_is_invalid(vault, capsys):
    (vault.root / "memory/unexpected.txt").write_text("invalid", encoding="utf-8")

    exit_code, stdout, stderr = _invoke(capsys, _arguments(vault, "doctor"))

    assert exit_code == 0
    assert stderr is None
    assert stdout["ok"] is True
    assert stdout["command"] == "doctor"
    assert stdout["status"] == "invalid"
    assert any(issue["code"] == "canonical-artifact-invalid" for issue in stdout["result"]["issues"])


def test_context_keeps_valid_canonical_state_usable_when_source_is_unavailable(
    vault, capsys
):
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import (
        CheckpointRequest,
        SessionBody,
        SessionStore,
        session_semantic_hash,
    )

    registries = RegistryStore(vault)
    registries.create_workstream("ws-1", project=None, mode="single")
    registries.register_source("source-1", authority="external-reference")
    body = SessionBody(
        "host-agent", "objective", "current", (), (), (), (), ()
    )
    policies = PolicyStore(vault)
    default = policies.load_active("vault-default", StorageClass.PORTABLE)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (policies.load_rule(default),),
        session_semantic_hash(body, ()),
    )
    SessionStore(vault).create_revision(
        CheckpointRequest(
            "ws-1", "session-1", StorageClass.PORTABLE, None, 0, body, (), receipt
        )
    )

    exit_code, stdout, stderr = _invoke(
        capsys,
        _arguments(vault, "context", "--workstream", "ws-1"),
    )

    assert exit_code == 0
    assert stderr is None
    assert stdout["ok"] is True
    assert stdout["status"] == "degraded"
    assert stdout["result"]["workstream_id"] == "ws-1"
    assert stdout["result"]["effective_status"] == "degraded"


def test_recall_returns_structured_hits_without_an_optional_model(vault, capsys):
    exit_code, stdout, stderr = _invoke(
        capsys, _arguments(vault, "recall", "checkpoint", "--limit", "5")
    )

    assert exit_code == 0
    assert stderr is None
    assert stdout["ok"] is True
    assert stdout["command"] == "recall"
    assert stdout["result"]["hits"] == []
    assert stdout["status"] in {"resolved", "degraded"}


@pytest.mark.parametrize(
    "missing_option",
    ["--kind", "--scope", "--authority", "--portability"],
)
def test_remember_rejects_each_missing_independent_axis(
    vault, capsys, missing_option
):
    command = [
        "remember",
        "--kind",
        "knowledge",
        "--scope",
        "personal-global",
        "--authority",
        "personal",
        "--portability",
        "personal-vault",
        "--body",
        "명시적인 기억",
    ]
    index = command.index(missing_option)
    del command[index : index + 2]

    exit_code, stdout, stderr = _invoke(capsys, _arguments(vault, *command))

    assert exit_code == 2
    assert stdout is None
    assert stderr["ok"] is False
    assert stderr["command"] == "remember"
    assert stderr["status"] == "error"
    assert stderr["error"]["code"] == "cli-usage"
    assert list((vault.root / "memory").glob("*.md")) == []


def test_remember_delegates_explicit_axes_to_canonical_memory_store(vault, capsys):
    exit_code, stdout, stderr = _invoke(
        capsys,
        _arguments(
            vault,
            "remember",
            "--kind",
            "knowledge",
            "--scope",
            "personal-global",
            "--authority",
            "personal",
            "--portability",
            "personal-vault",
            "--body",
            "한글 기억",
            "--memory-id",
            "memory-1",
        ),
    )

    assert exit_code == 0
    assert stderr is None
    assert stdout["status"] == "resolved"
    assert stdout["result"] == {
        "authority": "personal",
        "generation": 0,
        "id": "memory-1",
        "kind": "knowledge",
        "portability": "personal-vault",
        "scope": {"type": "personal-global"},
        "status": "candidate",
    }
    assert "한글 기억" in (vault.root / "memory/memory-1.md").read_text(encoding="utf-8")


def test_policy_failure_is_json_stderr_and_exit_two(vault, capsys):
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore

    policies = PolicyStore(vault)
    restrictive = policies.create_revision(
        "project-private", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries = RegistryStore(vault)
    registries.register_project("project-1")
    registries.assign_project_policy("project-1", restrictive, expected_generation=0)

    exit_code, stdout, stderr = _invoke(
        capsys,
        _arguments(
            vault,
            "remember",
            "--kind",
            "lesson",
            "--scope",
            "project",
            "--scope-id",
            "project-1",
            "--authority",
            "personal",
            "--portability",
            "personal-vault",
            "--body",
            "must remain local",
        ),
    )

    assert exit_code == 2
    assert stdout is None
    assert stderr["ok"] is False
    assert stderr["command"] == "remember"
    assert stderr["error"]["code"] == "policy-denied"
    assert list((vault.root / "memory").glob("*.md")) == []


def test_checkpoint_reads_host_composed_utf8_payload_and_advances_head(
    vault, tmp_path, capsys
):
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).create_workstream("ws-1", project=None, mode="single")
    payload = tmp_path / "checkpoint.json"
    _checkpoint_payload(payload)

    exit_code, stdout, stderr = _invoke(
        capsys, _arguments(vault, "checkpoint", "--payload", str(payload))
    )

    assert exit_code == 0
    assert stderr is None
    assert stdout["status"] == "resolved"
    assert stdout["result"] == {
        "revision": "000001",
        "session_id": "session-1",
        "workstream_id": "ws-1",
    }
    revision = vault.root / "workstreams/ws-1/sessions/session-1/000001.md"
    assert "한글 목표를 보존한다" in revision.read_text(encoding="utf-8")


def test_checkpoint_conflict_is_json_stderr_and_exit_two(vault, tmp_path, capsys):
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).create_workstream("ws-1", project=None, mode="single")
    payload = tmp_path / "checkpoint.json"
    _checkpoint_payload(payload, generation=7)

    exit_code, stdout, stderr = _invoke(
        capsys, _arguments(vault, "checkpoint", "--payload", str(payload))
    )

    assert exit_code == 2
    assert stdout is None
    assert stderr["ok"] is False
    assert stderr["command"] == "checkpoint"
    assert stderr["error"]["code"] == "registry-conflict"
    assert not (vault.root / "workstreams/ws-1/sessions").exists()


def test_project_and_reindex_delegate_to_core_stores(vault, capsys):
    exit_code, project, stderr = _invoke(
        capsys,
        _arguments(vault, "project", "--id", "project-1", "--authority", "project"),
    )
    assert exit_code == 0
    assert stderr is None
    assert project["result"] == {
        "authority": "project",
        "generation": 0,
        "id": "project-1",
        "locator": None,
        "storage_class": "portable",
    }

    exit_code, reindex, stderr = _invoke(capsys, _arguments(vault, "reindex"))
    assert exit_code == 0
    assert stderr is None
    assert reindex["result"]["rows"] >= 1
    assert reindex["result"]["source_rows"] == 0


def test_cli_schema_error_is_json_stderr_and_exit_two(vault, capsys):
    exit_code, stdout, stderr = _invoke(
        capsys, _arguments(vault, "project", "--id", "../unsafe")
    )

    assert exit_code == 2
    assert stdout is None
    assert stderr["status"] == "error"
    assert stderr["error"]["code"] == "invalid-artifact"


def test_distribution_keeps_legacy_name_and_adds_only_vault_cli():
    import tomllib

    metadata = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["name"] == "mneme-mcp"
    assert metadata["project"]["scripts"] == {
        "mneme": "mneme.server:main",
        "mneme-vault": "mneme.cli:main",
    }
    assert metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "mneme"
    ]
    assert not Path("madi").exists()
