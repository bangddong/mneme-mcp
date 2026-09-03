from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
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


def test_doctor_rebuilds_only_generated_index_when_explicitly_requested(vault):
    """Catches repair deleting or changing durable state while restoring an index."""
    from mneme.core.doctor import Doctor
    from mneme.core.registries import RegistryStore

    _workstream(vault)
    head = _checkpoint(vault)
    session_path = vault.root / "workstreams" / "ws-01" / "sessions" / "ses-01" / "000001.md"
    before = session_path.read_text(encoding="utf-8")
    report = Doctor(vault).run(repair=True)

    assert report.status == "resolved"
    assert report.repairs == ("generated-index-rebuilt",)
    assert (vault.local_root / "index" / "state.db").is_file()
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


def test_doctor_rebuilds_persisted_invalid_index_when_canonical_state_is_valid(vault):
    """Catches an invalid generated snapshot being reported as resolved or left unrepaired."""
    import sqlite3

    from mneme.core.doctor import Doctor
    from mneme.core.service import CoreService

    CoreService(vault).reindex()
    db = vault.local_root / "index" / "state.db"
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE index_state SET status = 'invalid'")
        connection.commit()

    report = Doctor(vault).run(repair=True)

    assert report.status == "resolved"
    assert "generated-index-rebuilt" in report.repairs
