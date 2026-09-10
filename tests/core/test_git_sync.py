"""Explicit Git transport boundaries for portable Vault artifacts."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

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


def _advance_remote(
    remote: Path,
    tmp_path: Path,
    text: str = "remote update",
    relative_path: str = "README.md",
) -> str:
    writer = tmp_path / f"writer-{len(tuple(tmp_path.glob('writer-*')))}"
    _git(tmp_path, "clone", "--branch", "main", str(remote), str(writer))
    _git(writer, "config", "user.name", "Remote Test")
    _git(writer, "config", "user.email", "remote@example.invalid")
    target = writer / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(writer, "add", "--", relative_path)
    _git(writer, "commit", "-m", "advance remote")
    _git(writer, "push", "origin", "main")
    return _git(writer, "rev-parse", "HEAD").stdout.strip()


def _file_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")


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


def test_fast_forward_validates_a_verified_ancestor_then_requires_manual_apply(
    vault, tmp_path
):
    """Catches direct ref/worktree mutation after a nominal fast-forward."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    remote_head = _advance_remote(remote, tmp_path, "verified fast-forward")
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()

    result = fast_forward(vault)

    assert result.status == "manual-fast-forward-required"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "refs/remotes/origin/main").stdout.strip() == remote_head
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before
    assert not (vault.root / "README.md").exists()


def test_fast_forward_refuses_an_invalid_fetched_target_without_mutating_checkout(
    vault, tmp_path
):
    """Catches a fetched target being trusted because the current checkout is valid."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    remote_head = _advance_remote(
        remote,
        tmp_path,
        "not a canonical memory artifact",
        "memory/unsafe.txt",
    )
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()
    schema_before = (vault.root / ".madi/schema-version").read_text(encoding="utf-8")

    result = fast_forward(vault)

    assert result.status == "target-invalid"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "refs/remotes/origin/main").stdout.strip() == remote_head
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before
    assert (vault.root / ".madi/schema-version").read_text(encoding="utf-8") == schema_before
    assert not (vault.root / "memory/unsafe.txt").exists()


def test_fast_forward_refuses_an_invalid_target_hidden_by_a_replacement_ref(
    vault, tmp_path
):
    """Catches Git object replacement making an invalid fetched commit look valid."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    valid_head = _git(vault.root, "rev-parse", "HEAD").stdout.strip()
    invalid_head = _advance_remote(
        remote,
        tmp_path,
        "unsafe canonical artifact",
        "memory/unsafe.txt",
    )
    _git(vault.root, "fetch", "origin")
    _git(vault.root, "replace", invalid_head, valid_head)
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()

    result = fast_forward(vault)

    assert result.status == "target-invalid"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before
    assert not (vault.root / "memory/unsafe.txt").exists()


@pytest.mark.parametrize("dangerous_command", ("update-ref", "restore"))
def test_fast_forward_never_reaches_fallible_ref_or_restore_mutation(
    vault, tmp_path, monkeypatch, dangerous_command
):
    """Catches a ref CAS or restore failure after an unsafe partial apply."""
    import mneme.core.git_sync as git_sync

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    _advance_remote(remote, tmp_path)
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()
    original = git_sync._run_git

    def injected(root, arguments, **kwargs):
        if arguments[0] == dangerous_command:
            raise git_sync.GitSyncError("injected mutation failure")
        return original(root, arguments, **kwargs)

    monkeypatch.setattr(git_sync, "_run_git", injected)

    result = git_sync.fast_forward(vault)

    assert result.status == "manual-fast-forward-required"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before


def test_fast_forward_preserves_a_late_worktree_edit_after_target_validation(
    vault, tmp_path, monkeypatch
):
    """Catches a target apply overwriting a write that arrives after validation."""
    import mneme.core.git_sync as git_sync

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    _advance_remote(remote, tmp_path)
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()
    original = getattr(git_sync, "_validate_commit_tree", None)

    def validate_then_edit(root, commit):
        valid = True
        if original is not None:
            valid = original(root, commit)
        (vault.root / "late-edit.txt").write_text("keep me", encoding="utf-8")
        return valid

    monkeypatch.setattr(git_sync, "_validate_commit_tree", validate_then_edit, raising=False)

    result = git_sync.fast_forward(vault)

    assert result.status == "dirty"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before
    assert (vault.root / "late-edit.txt").read_text(encoding="utf-8") == "keep me"


def test_fast_forward_returns_a_structured_refusal_when_head_changes_during_validation(
    vault, tmp_path, monkeypatch
):
    """Catches a ref CAS race being reported as a generic Git transport error."""
    import mneme.core.git_sync as git_sync

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    _advance_remote(remote, tmp_path)
    original = getattr(git_sync, "_validate_commit_tree", None)

    def validate_then_advance(root, commit):
        valid = True
        if original is not None:
            valid = original(root, commit)
        (vault.root / "concurrent.md").write_text("new head", encoding="utf-8")
        _git(vault.root, "add", "--", "concurrent.md")
        _git(vault.root, "commit", "-m", "concurrent local commit")
        return valid

    monkeypatch.setattr(
        git_sync, "_validate_commit_tree", validate_then_advance, raising=False
    )

    result = git_sync.fast_forward(vault)

    assert result.status == "concurrent-ref-changed"
    assert result.changed is False
    assert (vault.root / "concurrent.md").read_text(encoding="utf-8") == "new head"


def test_fast_forward_refuses_when_upstream_changes_during_target_validation(
    vault, tmp_path, monkeypatch
):
    """Catches returning an approval after the validated tracking ref has changed."""
    import mneme.core.git_sync as git_sync

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    _advance_remote(remote, tmp_path, "validated remote advance")
    attacker = tmp_path / "attacker"
    _git(tmp_path, "clone", str(remote), str(attacker))
    _git(attacker, "config", "user.name", "Attacker Test")
    _git(attacker, "config", "user.email", "attacker@example.invalid")
    unsafe_path = attacker / "memory" / "unsafe.txt"
    unsafe_path.parent.mkdir(parents=True)
    unsafe_path.write_text("unsafe", encoding="utf-8")
    _git(attacker, "add", "--", "memory/unsafe.txt")
    _git(attacker, "commit", "-m", "invalid replacement target")
    invalid_target = _git(attacker, "rev-parse", "HEAD").stdout.strip()
    original = git_sync._validate_commit_tree

    def validate_then_replace_upstream(root, commit):
        valid = original(root, commit)
        _git(root, "fetch", str(attacker), "main:refs/heads/attacker-target")
        _git(root, "update-ref", "refs/remotes/origin/main", invalid_target, commit)
        return valid

    monkeypatch.setattr(git_sync, "_validate_commit_tree", validate_then_replace_upstream)

    result = git_sync.fast_forward(vault)

    assert result.status == "concurrent-upstream-changed"
    assert result.changed is False
    assert (
        _git(vault.root, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        == invalid_target
    )
    assert not (vault.root / "memory/unsafe.txt").exists()


def test_fast_forward_uses_the_configured_differently_named_upstream(vault, tmp_path):
    """Catches synthesis of origin/current-branch instead of @{upstream}."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    _advance_remote(remote, tmp_path, "release update")
    writer = tmp_path / "release-writer"
    _git(tmp_path, "clone", "--branch", "main", str(remote), str(writer))
    _git(writer, "config", "user.name", "Remote Test")
    _git(writer, "config", "user.email", "remote@example.invalid")
    (writer / "release.txt").write_text("release", encoding="utf-8")
    _git(writer, "add", "--", "release.txt")
    _git(writer, "commit", "-m", "release branch")
    release_head = _git(writer, "rev-parse", "HEAD").stdout.strip()
    _git(writer, "push", "origin", "HEAD:refs/heads/release")
    _git(vault.root, "config", "branch.main.merge", "refs/heads/release")

    result = fast_forward(vault)

    assert result.status == "manual-fast-forward-required"
    assert _git(vault.root, "rev-parse", "refs/remotes/origin/release").stdout.strip() == release_head


def test_fast_forward_refuses_a_requested_remote_that_is_not_the_upstream(
    vault, tmp_path
):
    """Catches fetching an arbitrary named remote and treating it as the upstream."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    _git(vault.root, "remote", "add", "mirror", str(remote))

    result = fast_forward(vault, remote="mirror")

    assert result.status == "upstream-mismatch"
    assert result.changed is False
    assert _git(
        vault.root,
        "show-ref",
        "--verify",
        "--quiet",
        "refs/remotes/mirror/main",
        check=False,
    ).returncode == 1


def test_fast_forward_refuses_a_missing_upstream_before_fetching(vault):
    """Catches a missing tracking configuration becoming a generic fetch failure."""
    from mneme.core.git_sync import fast_forward

    _initialize_git(vault)
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()

    result = fast_forward(vault)

    assert result.status == "no-upstream"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before


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


@pytest.mark.parametrize("mutation", ("delete", "symlink"))
def test_commit_paths_refuses_a_file_swap_after_preflight_before_staging(
    vault, mutation, monkeypatch
):
    """Catches a deletion or local-state symlink entering after path preflight."""
    import mneme.core.git_sync as git_sync

    _initialize_git(vault)
    target = vault.root / ".madi" / "vault.yaml"
    generated = vault.local_root / "views" / "CURRENT.md"
    generated.write_text("generated local state", encoding="utf-8")
    before = _git(vault.root, "rev-parse", "HEAD").stdout.strip()
    original = git_sync._run_git
    injected = False

    def swap_before_add(root, arguments, **kwargs):
        nonlocal injected
        if arguments[0] == "add" and not injected:
            injected = True
            target.unlink()
            if mutation == "delete":
                pass
            else:
                _file_symlink(target, generated)
        return original(root, arguments, **kwargs)

    monkeypatch.setattr(git_sync, "_run_git", swap_before_add)

    result = git_sync.commit_paths(vault, (".madi/vault.yaml",), "unsafe race")

    assert result.status == "staged-content-invalid"
    assert result.changed is False
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(vault.root, "diff", "--cached", "--name-only").stdout == ""


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


def test_sync_preflight_authorizes_the_vault_root_when_no_data_artifacts_exist(vault):
    """Catches an empty current tree allowing .madi metadata past a tightened vault policy."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.git_sync import sync_preflight
    from mneme.core.policy import PolicyStore

    policies = PolicyStore(vault)
    restrictive = policies.create_revision(
        "vault-default", "2", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    policies.activate(restrictive, expected_generation=1)

    preflight = sync_preflight(vault, (".madi/vault.yaml",))

    assert preflight.allowed is False
    assert "policy-current-denied" in preflight.blocker_codes


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


def test_push_uses_the_captured_validated_head_not_a_late_head_advance(
    vault, tmp_path, monkeypatch
):
    """Catches a dynamic HEAD refspec pushing a commit that preflight never saw."""
    import mneme.core.git_sync as git_sync

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    (vault.root / "README.md").write_text("validated", encoding="utf-8")
    _git(vault.root, "add", "--", "README.md")
    _git(vault.root, "commit", "-m", "validated local commit")
    validated_head = _git(vault.root, "rev-parse", "HEAD").stdout.strip()
    original = git_sync._run_git
    injected = False

    def advance_head_before_push(root, arguments, **kwargs):
        nonlocal injected
        if arguments[0] == "push" and not injected:
            injected = True
            (vault.root / "late.md").write_text("late", encoding="utf-8")
            _git(vault.root, "add", "--", "late.md")
            _git(vault.root, "commit", "-m", "late local commit")
        return original(root, arguments, **kwargs)

    monkeypatch.setattr(git_sync, "_run_git", advance_head_before_push)

    result = git_sync.push(vault)

    assert result.status == "pushed"
    assert result.changed is True
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == validated_head
    assert _git(vault.root, "rev-parse", "HEAD").stdout.strip() != validated_head


def test_push_uses_the_differently_named_tracked_destination(vault, tmp_path):
    """Catches pushing to the current local branch rather than its upstream branch."""
    from mneme.core.git_sync import push

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    initial = _git(vault.root, "rev-parse", "HEAD").stdout.strip()
    _git(vault.root, "push", "origin", "main:refs/heads/release")
    _git(vault.root, "fetch", "origin")
    _git(vault.root, "config", "branch.main.merge", "refs/heads/release")
    (vault.root / "README.md").write_text("tracked destination", encoding="utf-8")
    _git(vault.root, "add", "--", "README.md")
    _git(vault.root, "commit", "-m", "advance tracked destination")
    head = _git(vault.root, "rev-parse", "HEAD").stdout.strip()

    result = push(vault)

    assert result.status == "pushed"
    assert result.changed is True
    assert _git(remote, "rev-parse", "refs/heads/release").stdout.strip() == head
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == initial


def test_push_returns_a_structured_refusal_when_the_upstream_has_advanced(
    vault, tmp_path
):
    """Catches a non-fast-forward rejection escaping as a generic Git error."""
    from mneme.core.git_sync import push

    _initialize_git(vault)
    remote = _bare_remote(vault, tmp_path)
    remote_head = _advance_remote(remote, tmp_path, "remote advance")
    (vault.root / "README.md").write_text("local advance", encoding="utf-8")
    _git(vault.root, "add", "--", "README.md")
    _git(vault.root, "commit", "-m", "advance local")

    result = push(vault)

    assert result.status == "remote-conflict"
    assert result.changed is False
    assert _git(remote, "rev-parse", "refs/heads/main").stdout.strip() == remote_head


def test_materialize_canonical_tree_rejects_duplicate_entries_before_writing(
    tmp_path, monkeypatch
):
    """Catches a malformed tree creating a partial snapshot before duplicate refusal."""
    import mneme.core.git_sync as git_sync
    from mneme.core.errors import InvalidArtifact

    destination = tmp_path / "snapshot"
    destination.mkdir()
    duplicate = (
        "100644",
        "blob",
        "a" * 40,
        PurePosixPath(".madi/vault.yaml"),
    )
    monkeypatch.setattr(
        git_sync,
        "_tree_entries",
        lambda _root, _commit: (duplicate, duplicate),
    )

    def unexpected_blob_read(*_args, **_kwargs):
        raise AssertionError("duplicate tree entries must fail before blob materialization")

    monkeypatch.setattr(git_sync, "_run_git_bytes", unexpected_blob_read)

    with pytest.raises(InvalidArtifact):
        git_sync._materialize_canonical_tree(tmp_path, "b" * 40, destination)

    assert list(destination.iterdir()) == []
