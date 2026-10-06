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


def _portable_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


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


def test_doctor_bootstraps_fresh_machine_local_state_without_portable_writes(
    vault, tmp_path, capsys
):
    new_state_home = tmp_path / "new-machine-state"
    new_state_home.mkdir()
    portable_before = _portable_files(vault.root)

    exit_code, stdout, stderr = _invoke(
        capsys,
        [
            "--vault-root",
            str(vault.root),
            "--state-home",
            str(new_state_home),
            "doctor",
        ],
    )

    assert exit_code == 0
    assert stderr is None
    assert stdout["ok"] is True
    assert stdout["command"] == "doctor"
    assert _portable_files(vault.root) == portable_before
    local_root = new_state_home / "vaults" / vault.id
    assert {
        path.relative_to(local_root).as_posix()
        for path in local_root.rglob("*")
        if path.is_dir()
    } == {
        "bindings",
        "overlays",
        "evidence",
        "pending",
        "views",
        "index",
        "cache",
        "locks",
        "logs",
    }
    assert not any(path.is_file() for path in local_root.rglob("*"))


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
    assert stderr["error"]["message"] == (
        "Policy evaluation rejected the request; reduce portability or review "
        "the active policy."
    )
    assert "must remain local" not in json.dumps(stderr)
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
    assert stderr["error"]["message"] == (
        "Canonical state changed; reload it and retry with explicit expected versions."
    )
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
    assert stderr["error"]["message"] == (
        "The request or Vault artifact failed validation; run doctor and retry "
        "with valid input."
    )
    assert "../unsafe" not in json.dumps(stderr)


def test_missing_vault_schema_does_not_disclose_its_local_path(tmp_path, capsys):
    vault_root = tmp_path / "local-secret-vault"
    state_home = tmp_path / "local-secret-state"

    exit_code, stdout, stderr = _invoke(
        capsys,
        [
            "--vault-root",
            str(vault_root),
            "--state-home",
            str(state_home),
            "status",
        ],
    )

    encoded = json.dumps(stderr)
    assert exit_code == 2
    assert stdout is None
    assert stderr["error"] == {
        "code": "invalid-artifact",
        "message": (
            "The request or Vault artifact failed validation; run doctor and retry "
            "with valid input."
        ),
    }
    assert str(vault_root) not in encoded
    assert "local-secret" not in encoded


def test_artifact_conflict_does_not_disclose_identity_or_storage_path(vault, capsys):
    project_id = "confidential-customer-project"
    arguments = _arguments(vault, "project", "--id", project_id)
    first_exit, _first_stdout, first_stderr = _invoke(capsys, arguments)
    assert first_exit == 0
    assert first_stderr is None

    exit_code, stdout, stderr = _invoke(capsys, arguments)

    encoded = json.dumps(stderr)
    assert exit_code == 2
    assert stdout is None
    assert stderr["error"] == {
        "code": "artifact-conflict",
        "message": (
            "The request conflicts with canonical state; choose a new identity or "
            "reload current state."
        ),
    }
    assert project_id not in encoded
    assert str(vault.root) not in encoded


def test_cli_usage_error_does_not_echo_secret_url_from_argv(vault, capsys):
    secret_url = "https://alice:top-secret@example.invalid/private?token=hidden"

    exit_code, stdout, stderr = _invoke(
        capsys,
        _arguments(
            vault,
            "remember",
            "--kind",
            secret_url,
            "--scope",
            "personal-global",
            "--authority",
            "personal",
            "--portability",
            "personal-vault",
            "--body",
            "confidential body",
        ),
    )

    encoded = json.dumps(stderr)
    assert exit_code == 2
    assert stdout is None
    assert stderr["error"] == {
        "code": "cli-usage",
        "message": "Command arguments are invalid; review --help and retry.",
    }
    assert secret_url not in encoded
    assert "top-secret" not in encoded
    assert "token" not in encoded
    assert "confidential body" not in encoded


def test_unexpected_error_uses_a_closed_message_without_sensitive_details(
    vault, capsys, monkeypatch
):
    sensitive = (
        r"C:\Users\alice\private\vault "
        "/home/alice/private/vault "
        "https://alice:password@example.invalid/api?token=hidden "
        "api_key=credential-value "
        f"sha256={'a' * 64} count=47 confidential-overlay exists"
    )

    def fail_unexpectedly(_vault, _arguments):
        raise RuntimeError(sensitive)

    monkeypatch.setattr("mneme.cli._dispatch", fail_unexpectedly)

    exit_code, stdout, stderr = _invoke(capsys, _arguments(vault, "status"))

    encoded = json.dumps(stderr)
    assert exit_code == 2
    assert stdout is None
    assert stderr["error"] == {
        "code": "internal-error",
        "message": "The command failed safely; run doctor and retry.",
    }
    for forbidden in (
        r"C:\Users\alice\private\vault",
        "/home/alice/private/vault",
        "https://",
        "password",
        "token",
        "api_key",
        "credential-value",
        "a" * 64,
        "count=47",
        "confidential-overlay",
        "exists",
    ):
        assert forbidden not in encoded


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


def test_sync_status_is_one_safe_json_envelope_when_git_is_unavailable(vault, capsys):
    """Catches nested sync status bypassing the Task 17 JSON boundary."""
    exit_code, stdout, stderr = _invoke(
        capsys, _arguments(vault, "sync", "status")
    )

    assert exit_code == 0
    assert stderr is None
    assert stdout == {
        "command": "sync",
        "ok": True,
        "result": {
            "ahead": 0,
            "behind": 0,
            "changed": False,
            "clean": True,
            "has_upstream": False,
            "repository": False,
            "status": "unavailable",
        },
        "status": "unavailable",
    }


def test_sync_rejects_a_remote_url_without_disclosing_it(vault, capsys):
    """Catches a Git error reflecting a credential-bearing remote URL."""
    secret_url = "https://alice:secret@example.invalid/repo?token=hidden"

    exit_code, stdout, stderr = _invoke(
        capsys,
        _arguments(vault, "sync", "fetch", "--remote", secret_url),
    )

    encoded = json.dumps(stderr)
    assert exit_code == 2
    assert stdout is None
    assert stderr == {
        "command": "sync",
        "error": {
            "code": "sync-error",
            "message": "Git sync failed safely; inspect sync status and retry.",
        },
        "ok": False,
        "status": "error",
    }
    assert secret_url not in encoded
    assert "alice" not in encoded
    assert "secret" not in encoded
    assert "token" not in encoded


@pytest.mark.parametrize(
    ("subcommand", "arguments"),
    (
        ("fetch", ()),
        (
            "commit",
            ("--path", ".madi/vault.yaml", "--message", "explicit sync batch"),
        ),
        ("fast-forward", ()),
        ("push", ()),
    ),
)
def test_sync_mutation_subcommands_use_the_safe_sync_boundary(
    vault, capsys, subcommand, arguments
):
    """Catches a declared sync verb bypassing the fixed Git error boundary."""
    exit_code, stdout, stderr = _invoke(
        capsys,
        _arguments(vault, "sync", subcommand, *arguments),
    )

    assert exit_code == 2
    assert stdout is None
    assert stderr == {
        "command": "sync",
        "error": {
            "code": "sync-error",
            "message": "Git sync failed safely; inspect sync status and retry.",
        },
        "ok": False,
        "status": "error",
    }


def test_checkpoint_cli_has_no_commit_on_checkpoint_setting(vault, tmp_path, capsys):
    """Catches re-coupling durable checkpoint persistence to Git policy."""
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).create_workstream("ws-1", project=None, mode="single")
    payload = tmp_path / "checkpoint.json"
    _checkpoint_payload(payload)

    exit_code, stdout, stderr = _invoke(
        capsys,
        _arguments(
            vault,
            "checkpoint",
            "--payload",
            str(payload),
            "--commit-on-checkpoint",
        ),
    )

    assert exit_code == 2
    assert stdout is None
    assert stderr["error"]["code"] == "cli-usage"
    assert not (vault.root / "workstreams/ws-1/sessions").exists()
