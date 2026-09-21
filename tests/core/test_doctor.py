from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import threading

import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def _checkpoint(vault, *, session_id="ses-01", parent=None, generation=0):
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.sessions import CheckpointRequest, SessionBody, SessionStore, session_semantic_hash

    body = SessionBody(
        "codex", "Doctor fixture", "working", (), (), (), ("continue",), ()
    )
    policies = PolicyStore(vault)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (policies.load_rule(policies.load_active("vault-default", StorageClass.PORTABLE)),),
        session_semantic_hash(body, ()),
    )
    return SessionStore(vault).create_revision(
        CheckpointRequest(
            "ws-01", session_id, StorageClass.PORTABLE, parent, generation,
            body, (), receipt, datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )


def _workstream(vault):
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).create_workstream("ws-01", project=None, mode="single")


def _replace_session_parent(vault, revision, parent_revision):
    from mneme.core.fs import dump_frontmatter, read_frontmatter

    path = (
        vault.root
        / "workstreams"
        / "ws-01"
        / "sessions"
        / "ses-01"
        / f"{revision}.md"
    )
    metadata, body = read_frontmatter(path)
    metadata["parent"] = {
        "session": "ses-01",
        "revision": parent_revision,
    }
    path.write_text(dump_frontmatter(metadata, body), encoding="utf-8")
    return path


def _create_directory_link(link: Path, target: Path, *, junction: bool) -> None:
    if junction:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"junction unavailable: {result.stderr or result.stdout}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")


def _remove_directory_link(path: Path, *, junction: bool) -> None:
    if junction:
        path.rmdir()
    else:
        path.unlink()


def _relocate_directory(source: Path, target: Path) -> None:
    target.mkdir()
    for child in source.iterdir():
        child.rename(target / child.name)
    source.rmdir()


def test_doctor_keeps_reachable_session_ancestors_out_of_orphans(vault):
    """Catches treating valid ancestors of the active head as interruption orphans."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    _workstream(vault)
    first = _checkpoint(vault)
    second = _checkpoint(vault, parent=first, generation=1)
    third = _checkpoint(vault, parent=second, generation=2)

    report = Doctor(vault).run()

    assert RegistryStore(vault).load_workstream("ws-01").active_heads == (
        third.as_head(),
    )
    assert not any(issue.code == "session-orphan" for issue in report.issues)


def test_doctor_rejects_forward_parent_without_hiding_later_orphan(vault):
    """Catches a forward edge making a later interrupted revision reachable."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    _workstream(vault)
    first = _checkpoint(vault)
    _checkpoint(vault, parent=first, generation=1)
    registries = RegistryStore(vault)
    current = registries.load_workstream("ws-01")
    registries.update_workstream(
        replace(current, active_heads=(first.as_head(),)),
        expected_generation=current.generation,
    )
    corrupted = _replace_session_parent(vault, "000001", "000002")
    before = corrupted.read_bytes()

    report = Doctor(vault).run()

    issues = {
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    }
    assert report.status == "invalid"
    assert (
        "canonical-artifact-invalid",
        "invalid",
        "session:ws-01:ses-01:000001",
    ) in issues
    assert (
        "session-orphan",
        "degraded",
        "session:ws-01:ses-01:000002",
    ) in issues
    assert registries.load_workstream("ws-01").active_heads == (first.as_head(),)
    assert corrupted.read_bytes() == before


def test_doctor_rejects_self_parent_without_mutating_revision(vault):
    """Catches a self-cycle being accepted as a complete active lineage."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    _workstream(vault)
    first = _checkpoint(vault)
    corrupted = _replace_session_parent(vault, "000001", "000001")
    before = corrupted.read_bytes()

    report = Doctor(vault).run()

    assert report.status == "invalid"
    assert (
        "canonical-artifact-invalid",
        "invalid",
        "session:ws-01:ses-01:000001",
    ) in {
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    }
    assert RegistryStore(vault).load_workstream("ws-01").active_heads == (
        first.as_head(),
    )
    assert corrupted.read_bytes() == before


def test_doctor_rejects_skipped_parent_without_following_the_edge(vault):
    """Catches a non-contiguous edge blessing an older revision as reachable."""
    from mneme.core.doctor import Doctor

    _workstream(vault)
    first = _checkpoint(vault)
    second = _checkpoint(vault, parent=first, generation=1)
    _checkpoint(vault, parent=second, generation=2)
    _replace_session_parent(vault, "000003", "000001")

    report = Doctor(vault).run()

    issues = {
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    }
    assert (
        "canonical-artifact-invalid",
        "invalid",
        "session:ws-01:ses-01:000003",
    ) in issues
    assert (
        "session-orphan",
        "degraded",
        "session:ws-01:ses-01:000001",
    ) in issues


def test_doctor_reports_orphan_without_changing_the_declared_head(vault):
    """Catches Doctor attaching an otherwise valid orphan revision automatically."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    _workstream(vault)
    first = _checkpoint(vault)
    second = _checkpoint(vault, parent=first, generation=1)
    registries = RegistryStore(vault)
    current = registries.load_workstream("ws-01")
    registries.update_workstream(
        replace(current, active_heads=(first.as_head(),)), expected_generation=current.generation
    )

    report = Doctor(vault).run()

    assert ("session-orphan", "degraded", "session:ws-01:ses-01:000002") in [
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    ]
    assert RegistryStore(vault).load_workstream("ws-01").active_heads == (first.as_head(),)
    assert (vault.root / "workstreams" / "ws-01" / "sessions" / "ses-01" / f"{second.revision}.md").exists()


def test_doctor_reports_unregistered_session_revision_as_orphan(vault):
    """Catches hiding a disconnected revision whose session is no longer registered."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).create_workstream("ws-01", project=None, mode="parallel")
    first = _checkpoint(vault)
    disconnected = _checkpoint(vault, session_id="ses-disconnected", generation=1)
    registries = RegistryStore(vault)
    current = registries.load_workstream("ws-01")
    registries.update_workstream(
        replace(current, active_heads=(first.as_head(),)),
        expected_generation=current.generation,
    )

    report = Doctor(vault).run()

    assert ("session-orphan", "degraded", f"session:ws-01:ses-disconnected:{disconnected.revision}") in [
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    ]


def test_doctor_marks_a_missing_active_head_invalid(vault):
    """Catches a missing registry-selected revision being hidden as a cache problem."""
    from mneme.core.doctor import Doctor
    from mneme.core.fs import dump_yaml, read_yaml

    _workstream(vault)
    _checkpoint(vault)
    path = vault.root / "workstreams" / "ws-01" / "workstream.yaml"
    metadata = read_yaml(path)
    metadata["active_heads"][0]["revision"] = "000099"
    path.write_text(dump_yaml(metadata), encoding="utf-8")

    report = Doctor(vault).run()

    assert report.status == "invalid"
    assert ("missing-active-head", "invalid", "workstream:ws-01") in [
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    ]


def test_doctor_marks_unbound_optional_source_degraded(vault):
    """Catches a missing read-only mount being treated as canonical corruption."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    RegistryStore(vault).register_source("source-01", authority="external-reference")

    report = Doctor(vault).run()

    assert report.status == "degraded"
    assert ("source-unavailable", "degraded", "source:source-01") in [
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    ]


def test_doctor_requires_explicit_manual_index_repair(vault):
    """Catches Doctor invoking path-based index publication during diagnosis repair."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    _workstream(vault)
    head = _checkpoint(vault)
    session_path = vault.root / "workstreams" / "ws-01" / "sessions" / "ses-01" / "000001.md"
    before = session_path.read_text(encoding="utf-8")
    report = Doctor(vault).run(repair=True)

    assert report.status == "degraded"
    assert report.repairs == ("generated-index-manual-repair-required",)
    assert not (vault.local_root / "index" / "state.db").exists()
    assert session_path.read_text(encoding="utf-8") == before
    assert RegistryStore(vault).load_workstream("ws-01").active_heads == (head.as_head(),)


def test_doctor_reports_stale_policy_receipt_without_reclassification(vault):
    """Catches policy tightening mutating a historic receipt or being ignored."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.doctor import Doctor
    from mneme.core.policy import PolicyStore

    _workstream(vault)
    _checkpoint(vault)
    session_path = vault.root / "workstreams" / "ws-01" / "sessions" / "ses-01" / "000001.md"
    before = session_path.read_text(encoding="utf-8")
    policies = PolicyStore(vault)
    tightened = policies.create_revision(
        "vault-default", "2", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    policies.activate(tightened, expected_generation=1)

    report = Doctor(vault).run()

    assert ("policy-receipt-stale", "degraded", "session:ws-01:ses-01:000001") in [
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    ]
    assert session_path.read_text(encoding="utf-8") == before


def test_doctor_marks_an_unreferenced_malformed_policy_invalid(vault):
    """Catches an unused current-tree policy bypassing canonical validation."""
    from mneme.core.doctor import Doctor

    revision = vault.root / ".madi" / "policies" / "unused-policy" / "1.yaml"
    revision.parent.mkdir()
    revision.write_text("schema: invalid\n", encoding="utf-8")

    report = Doctor(vault).run()

    assert ("canonical-policy-invalid", "invalid", "policy:unused-policy:1") in [
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    ]


def test_doctor_rejects_portable_reference_to_local_state_without_disclosure(vault):
    """Catches a local-only source ID leaking through a portable diagnostic."""
    from mneme.core.doctor import Doctor
    from mneme.core.fs import dump_frontmatter, read_frontmatter

    _workstream(vault)
    _checkpoint(vault)
    path = vault.root / "workstreams" / "ws-01" / "sessions" / "ses-01" / "000001.md"
    metadata, body = read_frontmatter(path)
    metadata["source_refs"] = [
        {"kind": "id", "storage_class": "local-only", "value": "local-secret-id"}
    ]
    path.write_text(dump_frontmatter(metadata, body), encoding="utf-8")

    report = Doctor(vault).run()

    issue = next(issue for issue in report.issues if issue.code == "portable-local-reference")
    assert issue.severity == "invalid"
    assert issue.artifact == "session:ws-01:ses-01:000001"
    assert "local-secret-id" not in issue.detail


def test_doctor_reports_unreadable_mounted_markdown_without_a_local_path(vault, tmp_path):
    """Catches optional source decoding failures being silently skipped by indexing."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "broken.md").write_bytes(b"\xff\xfe")
    registries = RegistryStore(vault)
    registries.register_source("source-01", authority="external-reference")
    registries.bind_source("source-01", checkout)

    report = Doctor(vault).run()

    issue = next(issue for issue in report.issues if issue.code == "source-markdown-unreadable")
    assert issue.severity == "degraded"
    assert issue.artifact == "source:source-01"
    assert str(checkout) not in issue.detail


def test_doctor_removes_only_old_local_locks_when_repairing(vault):
    """Catches repair deleting a lock merely because its timestamp is old."""
    from mneme.core.doctor import Doctor
    from mneme.core.fs import exclusive_file_lock

    stale = vault.local_root / "locks" / "old-operation.lock"
    stale.write_bytes(b"\0")
    os.utime(stale, (1, 1))

    entered = threading.Event()
    release = threading.Event()

    def hold_lock():
        with exclusive_file_lock(stale):
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert entered.wait(timeout=5)
    report = Doctor(vault, stale_lock_age_seconds=1).run(repair=True)
    release.set()
    holder.join(timeout=5)

    assert stale.exists()
    assert "stale-local-lock-removed" not in report.repairs


def test_doctor_never_repairs_an_injected_external_index_path(vault, tmp_path):
    """Catches a caller-supplied GeneratedIndex overwriting a mounted source."""
    from mneme.core.doctor import Doctor
    from mneme.core.search.index import GeneratedIndex

    external = tmp_path / "mounted-source.md"
    external.write_text("must remain Markdown\n", encoding="utf-8")

    report = Doctor(vault, GeneratedIndex(external)).run(repair=True)

    assert external.read_text(encoding="utf-8") == "must remain Markdown\n"
    assert "generated-index-rebuilt" not in report.repairs


def test_doctor_rejects_a_symlinked_generated_index_before_repair(vault, tmp_path):
    """Catches a state.db symlink redirecting SQLite publication outside local state."""
    from mneme.core.doctor import Doctor

    external = tmp_path / "external.md"
    external.write_text("outside content\n", encoding="utf-8")
    index = vault.local_root / "index" / "state.db"
    try:
        index.symlink_to(external)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    report = Doctor(vault).run(repair=True)

    assert external.read_text(encoding="utf-8") == "outside content\n"
    assert report.status == "invalid"
    assert "generated-index-rebuilt" not in report.repairs


def test_doctor_plain_run_does_not_create_generated_lock_files(vault):
    """Catches diagnosis creating .state.db.lock as an operational side effect."""
    from mneme.core.doctor import Doctor

    before = sorted(path.relative_to(vault.local_root) for path in vault.local_root.rglob("*"))
    Doctor(vault).run()
    after = sorted(path.relative_to(vault.local_root) for path in vault.local_root.rglob("*"))

    assert after == before


@pytest.mark.parametrize("contents", [None, "2\n"])
def test_doctor_fails_closed_for_missing_or_wrong_schema_version(vault, contents):
    """Catches foundational Vault corruption becoming an empty optional collection."""
    from mneme.core.doctor import Doctor

    schema = vault.root / ".madi" / "schema-version"
    if contents is None:
        schema.unlink()
    else:
        schema.write_text(contents, encoding="utf-8")

    report = Doctor(vault).run(repair=True)

    assert report.status == "invalid"
    assert "generated-index-rebuilt" not in report.repairs


def test_doctor_reevaluates_session_against_new_project_policy_assignment(vault):
    """Catches Doctor comparing only global active policy IDs after a project tightens."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.doctor import Doctor
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore

    registries = RegistryStore(vault)
    project = registries.register_project("project-01")
    registries.create_workstream("ws-01", project=project.id, mode="single")
    _checkpoint(vault)
    policies = PolicyStore(vault)
    restricted = policies.create_revision(
        "project-policy", "1", {"ceiling": "local-only"}, StorageClass.PORTABLE
    )
    registries.assign_project_policy(project.id, restricted, expected_generation=0)

    report = Doctor(vault).run()

    assert ("policy-reevaluation-noncompliant", "invalid", "session:ws-01:ses-01:000001") in [
        (issue.code, issue.severity, issue.artifact) for issue in report.issues
    ]


def test_doctor_reports_persisted_invalid_index_for_manual_rebuild(vault):
    """Catches an invalid generated snapshot being reported as resolved."""
    import sqlite3

    from mneme.core.doctor import Doctor
    from mneme.core.service import CoreService

    CoreService(vault).reindex()
    db = vault.local_root / "index" / "state.db"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE index_state SET status = 'invalid'")
        connection.commit()

    report = Doctor(vault).run(repair=True)

    assert report.status == "invalid"
    assert "generated-index-manual-repair-required" in report.repairs


def test_doctor_accepts_a_fresh_registry_only_workstream(vault):
    """Catches Doctor requiring sessions before the first checkpoint creates it."""
    from mneme.core.doctor import Doctor

    _workstream(vault)

    report = Doctor(vault).run()

    assert [(issue.code, issue.severity) for issue in report.issues] == [
        ("generated-index-rebuildable", "degraded")
    ]


def test_doctor_rejects_symlinked_sessions_without_reading_target(vault, tmp_path):
    """Catches canonical scanning following a sessions-directory symlink."""
    from mneme.core.doctor import Doctor

    _workstream(vault)
    _checkpoint(vault)
    sessions = vault.root / "workstreams" / "ws-01" / "sessions"
    target = tmp_path / "external-sessions"
    sessions.rename(target)
    _create_directory_link(sessions, target, junction=False)
    try:
        report = Doctor(vault).run()
    finally:
        _remove_directory_link(sessions, junction=False)

    assert report.status == "invalid"
    assert not any(issue.artifact.startswith("session:") for issue in report.issues)


def test_doctor_rejects_a_canonical_root_beneath_a_symlink_ancestor(
    vault, tmp_path
):
    """Catches strict diagnosis traversing an alias before checking its leaf root."""
    from mneme.core.doctor import Doctor

    _workstream(vault)
    _checkpoint(vault)
    alias = tmp_path / "vault-parent-alias"
    _create_directory_link(alias, vault.root.parent, junction=False)
    injected = replace(vault)
    object.__setattr__(injected, "root", alias / vault.root.name)
    try:
        report = Doctor(injected).run()
    finally:
        _remove_directory_link(alias, junction=False)

    assert report.status == "invalid"
    assert "canonical-artifact-invalid" in {issue.code for issue in report.issues}
    assert not any(issue.artifact.startswith("session:") for issue in report.issues)


def test_doctor_does_not_traverse_local_locks_beneath_a_symlink_ancestor(
    vault, tmp_path
):
    """Catches generated-index and stale-lock diagnosis following local aliases."""
    from mneme.core.doctor import Doctor

    stale = vault.local_root / "locks" / "old-operation.lock"
    stale.write_bytes(b"\0")
    os.utime(stale, (1, 1))
    alias = tmp_path / "state-alias"
    _create_directory_link(alias, vault.state_home, junction=False)
    injected = replace(vault)
    object.__setattr__(injected, "local_root", alias / "vaults" / vault.id)
    try:
        report = Doctor(injected, stale_lock_age_seconds=1).run()
    finally:
        _remove_directory_link(alias, junction=False)

    codes = {issue.code for issue in report.issues}
    assert "generated-index-unsafe" in codes
    assert "stale-local-lock" not in codes


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_doctor_rejects_junctioned_sessions_without_reading_target(vault, tmp_path):
    """Catches Windows junctions bypassing Path.is_symlink canonical checks."""
    from mneme.core.doctor import Doctor

    _workstream(vault)
    _checkpoint(vault)
    sessions = vault.root / "workstreams" / "ws-01" / "sessions"
    target = tmp_path / "external-sessions"
    sessions.rename(target)
    _create_directory_link(sessions, target, junction=True)
    try:
        report = Doctor(vault).run()
    finally:
        _remove_directory_link(sessions, junction=True)

    assert report.status == "invalid"
    assert not any(issue.artifact.startswith("session:") for issue in report.issues)


def test_doctor_rejects_symlinked_index_directory_without_inspecting_target(vault):
    """Catches generated-index diagnosis following a directory symlink."""
    import sqlite3

    from mneme.core.doctor import Doctor
    from mneme.core.service import CoreService

    CoreService(vault).reindex()
    index = vault.local_root / "index"
    connection = sqlite3.connect(index / "state.db")
    try:
        connection.execute("UPDATE index_state SET status = 'invalid'")
        connection.commit()
    finally:
        connection.close()
    target = vault.local_root / "linked-index-target"
    _relocate_directory(index, target)
    _create_directory_link(index, target, junction=False)
    try:
        report = Doctor(vault).run(repair=True)
    finally:
        _remove_directory_link(index, junction=False)

    codes = {issue.code for issue in report.issues}
    assert "canonical-vault-invalid" in codes
    assert "generated-index-unsafe" in codes
    assert "generated-index-invalid" not in codes


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_doctor_rejects_junctioned_index_directory_without_inspecting_target(vault):
    """Catches a contained Windows junction being accepted as an index target."""
    import sqlite3

    from mneme.core.doctor import Doctor
    from mneme.core.service import CoreService

    CoreService(vault).reindex()
    index = vault.local_root / "index"
    connection = sqlite3.connect(index / "state.db")
    try:
        connection.execute("UPDATE index_state SET status = 'invalid'")
        connection.commit()
    finally:
        connection.close()
    target = vault.local_root / "junction-index-target"
    _relocate_directory(index, target)
    _create_directory_link(index, target, junction=True)
    try:
        report = Doctor(vault).run(repair=True)
    finally:
        _remove_directory_link(index, junction=True)

    codes = {issue.code for issue in report.issues}
    assert "canonical-vault-invalid" in codes
    assert "generated-index-unsafe" in codes
    assert "generated-index-invalid" not in codes


def test_doctor_rejects_unknown_file_beside_workstream_registry(vault):
    """Catches permissive workstream traversal ignoring an unknown sibling file."""
    from mneme.core.doctor import Doctor

    _workstream(vault)
    (vault.root / "workstreams" / "ws-01" / "unexpected.txt").write_text("x", encoding="utf-8")

    assert Doctor(vault).run().status == "invalid"


def test_doctor_rejects_symlinked_workstream_registry(vault, tmp_path):
    """Catches Path.is_file accepting a symlinked canonical registry."""
    from mneme.core.doctor import Doctor

    _workstream(vault)
    registry = vault.root / "workstreams" / "ws-01" / "workstream.yaml"
    target = tmp_path / "registry.yaml"
    registry.rename(target)
    try:
        registry.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    assert Doctor(vault).run().status == "invalid"


@pytest.mark.parametrize("kind", ["unknown-session-entry", "symlink-session-dir", "unknown-revision", "symlink-revision", "missing-sessions"])
def test_doctor_rejects_invalid_session_tree_entries(vault, tmp_path, kind):
    """Catches session descendant grammar being filtered rather than rejected."""
    from mneme.core.doctor import Doctor

    _workstream(vault)
    _checkpoint(vault)
    workstream = vault.root / "workstreams" / "ws-01"
    sessions = workstream / "sessions"
    session = sessions / "ses-01"
    revision = session / "000001.md"
    if kind == "unknown-session-entry":
        (sessions / "junk.txt").write_text("x", encoding="utf-8")
    elif kind == "symlink-session-dir":
        target = tmp_path / "session"
        session.rename(target)
        try:
            session.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    elif kind == "unknown-revision":
        (session / "junk.txt").write_text("x", encoding="utf-8")
    elif kind == "symlink-revision":
        target = tmp_path / "revision.md"
        revision.rename(target)
        try:
            revision.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlink unavailable: {exc}")
    else:
        target = tmp_path / "sessions"
        sessions.rename(target)

    assert Doctor(vault).run(repair=True).status == "invalid"
