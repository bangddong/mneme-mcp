"""Release-gate coverage for current-tree reconstruction from a shallow clone."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


FIXTURE = Path(__file__).parents[1] / "fixtures" / "vault-clean"
_LOCAL_DIRECTORIES = {
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


def _git(cwd: Path, *arguments: str) -> str:
    result = _git_result(cwd, *arguments)
    result.check_returncode()
    return result.stdout.strip()


def _git_result(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_EMAIL": "release-gate@example.invalid",
            "GIT_AUTHOR_NAME": "Madi release gate",
            "GIT_COMMITTER_EMAIL": "release-gate@example.invalid",
            "GIT_COMMITTER_NAME": "Madi release gate",
        }
    )
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )


def _cli(vault_root: Path, state_home: Path, *arguments: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mneme.cli",
            "--vault-root",
            str(vault_root),
            "--state-home",
            str(state_home),
            *arguments,
        ],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        cwd=Path(__file__).parents[2],
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _portable_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def _remove_generated_state(vault) -> None:
    for name in ("index", "views"):
        target = vault.local_root / name
        shutil.rmtree(target)
        target.mkdir()


def _portable_semantics(vault) -> dict[str, object]:
    from mneme.core.service import CoreService

    service = CoreService(vault)
    report = service.reindex()
    current = service.context("fixture-work", "portable")
    profile = service.profile(
        {"type": "project", "project_id": "fixture-project"}
    )
    recall = service.recall("deterministic", 20)
    return {
        "current": current.text,
        "current_status": current.effective_status.value,
        "profile": profile.text,
        "profile_status": profile.effective_status.value,
        "recall": tuple(
            (
                hit.category,
                hit.artifact_id,
                hit.revision,
                hit.excerpt,
                hit.scope,
            )
            for hit in recall.hits
        ),
        "reindex_rows": report.rows,
        "reindex_source_rows": report.source_rows,
    }


def test_shallow_clone_rebuilds_portable_semantics_without_source_overlay(tmp_path):
    """A missing current-tree artifact or a local/history dependency breaks clone recovery."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore
    from mneme.core.vault import Vault

    assert FIXTURE.is_dir(), "the deterministic portable Vault fixture is required"
    fixture_files = tuple(path for path in FIXTURE.rglob("*") if path.is_file())
    assert fixture_files
    assert not any(path.suffix in {".db", ".sqlite", ".sqlite3"} for path in fixture_files)
    assert not any("overlays" in path.parts for path in fixture_files)

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "README.md").write_text(
        "Parent commit before the portable fixture state.\n", encoding="utf-8"
    )
    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture: parent before portable state")
    parent_commit = _git(source, "rev-parse", "HEAD")
    shutil.copytree(FIXTURE, source, dirs_exist_ok=True)
    source_state = tmp_path / "source-state"
    source_vault = Vault.open_or_bootstrap_local(source, source_state)

    RegistryStore(source_vault, StorageClass.LOCAL_ONLY).create_workstream(
        "source-overlay", project=None, mode="parallel"
    )
    assert any(
        "source-overlay" in path.read_text(encoding="utf-8")
        for path in source_vault.local_root.rglob("*.yaml")
    )
    assert not any(
        "source-overlay" in path.read_text(encoding="utf-8")
        for path in source.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and path.suffix in {".md", ".yaml"}
    )

    _remove_generated_state(source_vault)
    source_doctor = Doctor(source_vault).run()
    assert source_doctor.status != "invalid"
    source_semantics = _portable_semantics(source_vault)

    _git(source, "add", ".")
    _git(source, "commit", "-m", "fixture: portable current tree")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-u", "origin", "main")

    clone = tmp_path / "shallow-clone"
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
    assert _git(clone, "rev-list", "--count", "HEAD") == "1"

    clone_state = tmp_path / "clone-state"
    portable_before = _portable_files(clone)
    doctor_result = _cli(clone, clone_state, "doctor")
    assert doctor_result["ok"] is True
    assert doctor_result["status"] != "invalid"
    assert not any(
        issue["code"] == "session-orphan"
        for issue in doctor_result["result"]["issues"]
    )
    from mneme.core.fs import read_yaml

    vault_id = read_yaml(clone / ".madi" / "vault.yaml")["id"]
    fresh_local_root = clone_state / "vaults" / vault_id
    assert {
        path.relative_to(fresh_local_root).as_posix()
        for path in fresh_local_root.rglob("*")
        if path.is_dir()
    } == _LOCAL_DIRECTORIES
    assert not any(path.is_file() for path in fresh_local_root.rglob("*"))
    reindex_result = _cli(clone, clone_state, "reindex")
    assert reindex_result["ok"] is True
    context_result = _cli(
        clone, clone_state, "context", "--workstream", "fixture-work"
    )
    assert context_result["ok"] is True
    assert (
        "Portable checkpoint 3 is current-tree state."
        in context_result["result"]["text"]
    )
    assert _portable_files(clone) == portable_before
    cloned_vault = Vault.open(clone, clone_state)
    assert not any(
        "source-overlay" in path.read_text(encoding="utf-8")
        for root in (clone, cloned_vault.local_root)
        for path in root.rglob("*")
        if path.is_file() and path.suffix in {".md", ".yaml"}
    )

    clone_doctor = Doctor(cloned_vault).run()
    assert clone_doctor.status != "invalid"
    assert not any(issue.code == "session-orphan" for issue in source_doctor.issues)
    assert not any(issue.code == "session-orphan" for issue in clone_doctor.issues)
    clone_semantics = _portable_semantics(cloned_vault)

    assert clone_semantics == source_semantics
    assert _git(clone, "rev-parse", "--is-shallow-repository") == "true"
    assert "Portable checkpoint 3 is current-tree state." in clone_semantics["current"]
    assert "Prefer deterministic, inspectable release evidence." in clone_semantics["profile"]
    assert "Prefer deterministic release evidence." not in clone_semantics["profile"]
    assert len(list(clone.glob("workstreams/*/sessions/*/*.md"))) == 3
    assert len(list(clone.glob("memory/*.md"))) == 2
    assert (clone / ".madi/policies/fixture-policy/2.yaml").is_file()
    assert (
        _git_result(clone, "cat-file", "-e", f"{parent_commit}^{{commit}}").returncode
        != 0
    )
