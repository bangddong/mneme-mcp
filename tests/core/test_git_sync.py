"""Explicit Git transport boundaries for portable Vault artifacts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )


def _initialize_git(vault) -> None:
    _git(vault.root, "init", "--initial-branch=main")
    _git(vault.root, "config", "user.name", "Madi Test")
    _git(vault.root, "config", "user.email", "madi@example.invalid")
    _git(vault.root, "add", "--", ".")
    _git(vault.root, "commit", "-m", "initial portable vault")


def _bare_remote(vault, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(vault.root, "remote", "add", "origin", str(remote))
    _git(vault.root, "push", "--set-upstream", "origin", "main")
    return remote


def _advance_remote(remote: Path, tmp_path: Path, text: str = "remote update") -> str:
    writer = tmp_path / f"writer-{len(tuple(tmp_path.glob('writer-*')))}"
    _git(tmp_path, "clone", "--branch", "main", str(remote), str(writer))
    _git(writer, "config", "user.name", "Remote Test")
    _git(writer, "config", "user.email", "remote@example.invalid")
    (writer / "README.md").write_text(text, encoding="utf-8")
    _git(writer, "add", "--", "README.md")
    _git(writer, "commit", "-m", "advance remote")
    _git(writer, "push", "origin", "main")
    return _git(writer, "rev-parse", "HEAD").stdout.strip()


def _checkpoint(vault):
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.sessions import (
        CheckpointRequest,
        SessionBody,
        SessionStore,
        session_semantic_hash,
    )

    RegistryStore(vault).create_workstream("ws-1", project=None, mode="single")
    body = SessionBody(
        "host-agent",
        "portable checkpoint",
        "ready to sync",
        ("validated",),
        (),
        (),
        ("commit explicitly",),
        (),
    )
    policies = PolicyStore(vault)
    active = policies.load_active("vault-default", StorageClass.PORTABLE)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (policies.load_rule(active),),
        session_semantic_hash(body, ()),
    )
    return SessionStore(vault).create_revision(
        CheckpointRequest(
            "ws-1", "session-1", StorageClass.PORTABLE, None, 0, body, (), receipt
        )
    )


def test_fast_forward_fetches_then_refuses_a_dirty_worktree(vault, tmp_path):
    """Catches fast-forward applying remote state over uncommitted local work."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    remote_head = _advance_remote(remote, tmp_path)
    (vault.root / "local-draft.txt").write_text("not committed", encoding="utf-8")

    result = fast_forward(vault)

    assert result.status == "dirty"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "refs/remotes/origin/main").stdout.strip() == remote_head
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() != remote_head


def test_fast_forward_reports_divergence_without_merging(vault, tmp_path):
    """Catches divergent histories being merged, rebased, reset, or overwritten."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    _advance_remote(remote, tmp_path, "remote branch")
    (vault.root / "README.md").write_text("local branch", encoding="utf-8")
    _git(vault.root, "add", "--", "README.md")
    _git(vault.root, "commit", "-m", "advance local")
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()

    result = fast_forward(vault)

    assert result.status == "divergent"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before
    assert "<<<<<<<" not in (vault.root / "README.md").read_text(encoding="utf-8")


def test_fast_forward_updates_only_when_local_head_is_a_verified_ancestor(
    vault, tmp_path
):
    """Catches a nominal fast-forward that does not update HEAD and the clean tree."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    remote_head = _advance_remote(remote, tmp_path, "verified fast-forward")

    result = fast_forward(vault)

    assert result.status == "fast-forwarded"
    assert result.changed is True
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == remote_head
    assert (vault.root / "README.md").read_text(encoding="utf-8") == "verified fast-forward"


def test_commit_paths_commits_only_explicit_canonical_portable_artifacts(vault):
    """Catches broad staging of unrelated canonical or machine-local state."""
    from mneme.core.git_sync import commit_paths
    from mneme.core.registries import RegistryStore

    _initialize_git(vault)
    ref = _checkpoint(vault)
    RegistryStore(vault).register_project("unlisted-project")
    local_state = vault.local_root / "views" / "CURRENT.md"
    local_state.write_text("generated local state", encoding="utf-8")
    _git(vault.root, "add", "--", "projects/unlisted-project.yaml")

    result = commit_paths(
        vault,
        (
            "workstreams/ws-1/workstream.yaml",
            f"workstreams/ws-1/sessions/{ref.session}/{ref.revision}.md",
        ),
        "checkpoint batch",
    )

    committed = set(
        _git(
            vault.root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            "HEAD",
        ).stdout.splitlines()
    )
    assert result.status == "committed"
    assert result.changed is True
    assert committed == {
        "workstreams/ws-1/workstream.yaml",
        f"workstreams/ws-1/sessions/{ref.session}/{ref.revision}.md",
    }
    assert _git(vault.root, "diff", "--cached", "--name-only").stdout.splitlines() == [
        "projects/unlisted-project.yaml"
    ]
    assert not _git(
        vault.root, "ls-tree", "-r", "--name-only", "HEAD"
    ).stdout.__contains__("CURRENT.md")


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "README.md",
        ".git/config",
        "views/CURRENT.md",
        "memory",
        "../outside.md",
    ),
)
def test_commit_paths_rejects_noncanonical_or_nonartifact_paths(vault, unsafe_path):
    """Catches caller-selected documentation, Git metadata, views, or escapes entering a commit."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.git_sync import commit_paths

    _initialize_git(vault)

    with pytest.raises(InvalidArtifact):
        commit_paths(vault, (unsafe_path,), "unsafe batch")


def test_commit_paths_rejects_an_absolute_machine_local_path(vault):
    """Catches a local state path being translated into a portable Git path."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.git_sync import commit_paths

    _initialize_git(vault)

    with pytest.raises(InvalidArtifact):
        commit_paths(vault, (vault.local_root / "config.toml",), "unsafe batch")


def test_commit_paths_rejects_a_missing_canonical_artifact(vault):
    """Catches a pathspec staging deletion outside an explicit remediation flow."""
    from mneme.core.errors import InvalidArtifact
    from mneme.core.git_sync import commit_paths

    _initialize_git(vault)

    with pytest.raises(InvalidArtifact):
        commit_paths(
            vault,
            ("workstreams/ws-1/sessions/session-1/000001.md",),
            "remove a missing artifact",
        )


def test_checkpoint_succeeds_without_git_and_never_calls_a_sync_primitive(
    vault, monkeypatch
):
    """Catches checkpoint persistence acquiring an implicit Git dependency."""
    import mneme.core.git_sync as git_sync

    def unexpected(*_args, **_kwargs):
        raise AssertionError("checkpoint must remain independent of Git")

    for name in ("fetch", "commit_paths", "fast_forward", "push"):
        monkeypatch.setattr(git_sync, name, unexpected)

    ref = _checkpoint(vault)

    assert ref.revision == "000001"
    assert not (vault.root / ".git").exists()


def test_sync_preflight_combines_doctor_with_a_live_authorization_seam(vault):
    """Catches generated state or a historic receipt overriding a live policy denial."""
    from mneme.core.git_sync import LivePolicyDecision, sync_preflight

    (vault.local_root / "index" / "state.db").write_text(
        "generated hint says allowed", encoding="utf-8"
    )

    preflight = sync_preflight(
        vault,
        live_authorizer=lambda _vault, _paths: LivePolicyDecision(
            False, ("live-policy-denied",)
        ),
    )

    assert preflight.allowed is False
    assert preflight.status == "blocked"
    assert preflight.blocker_codes == ("live-policy-denied",)


def test_push_is_explicit_and_exposes_no_force_mode(vault, tmp_path):
    """Catches an API option allowing callers to turn a safe push into a force push."""
    from mneme.core.git_sync import push

    _initialize_git(vault)
    _bare_remote(vault, tmp_path)

    with pytest.raises(TypeError):
        push(vault, force=True)

    result = push(vault)
    assert result.status == "pushed"
    assert result.changed is True
