"""Privacy tightening and fail-open adapter behavior at the release boundary."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sqlite3


def _command(service, name: str, payload: dict[str, object]):
    from mneme.core.contracts import CoreCommand

    result = service.execute(CoreCommand(1, name, payload))
    assert result.ok is True, result.as_dict()
    return result


def _portable_text(vault) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(vault.root.rglob("*"))
        if path.is_file()
    )


def test_tightening_with_stale_allowed_index_withholds_every_output(tmp_path):
    """A historical receipt or stale cache must never authorize current distribution."""
    from mneme.core.git_sync import sync_preflight
    from mneme.core.registries import RegistryStore
    from mneme.core.security import PolicyArtifactRef
    from mneme.core.service import CoreService
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    local_values = (
        "API_KEY=LOCAL_EVIDENCE_ONLY_7F3C",
        "customer-9472-private",
        "https://internal.invalid/customer/runbook",
        "SELECT private_customer_column FROM production_records",
        "request-log private trace 82d6",
    )
    local_id = "local-evidence-7f3c"
    local_count = "local-count-five"
    evidence_path = vault.local_root / "evidence" / "privacy-input.txt"
    evidence_path.write_text(
        "\n".join((local_id, local_count, *local_values)), encoding="utf-8"
    )
    local_digest = sha256(evidence_path.read_bytes()).hexdigest()

    registries = RegistryStore(vault)
    project = registries.register_project("release-project")
    service = CoreService(vault)
    registered = _command(
        service,
        "register_source",
        {
            "source_id": "release-source",
            "authority": "external-reference",
            "kind": "filesystem",
            "project": project.id,
            "storage_class": "portable",
        },
    )
    assert registered.domain_events[0].name == "source_registered"
    registries.create_workstream(
        "privacy-work", project=project.id, mode="single"
    )

    created = _command(
        service,
        "create_policy_revision",
        {
            "policy_id": "source-policy",
            "revision": "1",
            "rule": {"ceiling": "personal-vault"},
            "storage_class": "portable",
        },
    )
    permitted_ref = created.result["policy_ref"]
    activated = _command(
        service,
        "activate_policy_revision",
        {"expected_generation": 1, "policy_ref": permitted_ref},
    )
    assert activated.result["generation"] == 2
    _command(
        service,
        "assign_project_policy",
        {
            "project_id": project.id,
            "policy_ref": permitted_ref,
            "expected_generation": 0,
            "storage_class": "portable",
        },
    )
    _command(
        service,
        "assign_source_policy",
        {
            "source_id": "release-source",
            "policy_ref": permitted_ref,
            "expected_generation": 0,
            "storage_class": "portable",
        },
    )

    source_reference = {
        "kind": "id",
        "storage_class": "portable",
        "value": "release-source",
    }
    session = _command(
        service,
        "create_session_revision",
        {
            "workstream_id": "privacy-work",
            "session_id": "privacy-session",
            "storage_class": "portable",
            "expected_parent": None,
            "expected_registry_generation": 0,
            "body": {
                "adapter_id": "codex",
                "objective": "Exercise live policy gates.",
                "current_state": "policy-gated session marker",
                "verified_facts": ["Only sanitized semantics entered the Vault."],
                "completed_work": [],
                "blockers": [],
                "next_actions": ["Re-evaluate before every use."],
                "source_refs": [source_reference],
            },
            "relations": [],
        },
    )
    submitted = _command(
        service,
        "submit_memory",
        {
            "memory_id": "policy-preference",
            "kind": "preference",
            "scope": {
                "type": "project",
                "project_id": project.id,
            },
            "authority": "personal",
            "portability": "personal-vault",
            "storage_class": "portable",
            "body": "policy-gated preference marker",
            "provenance": [source_reference],
        },
    )
    assert submitted.result["generation"] == 0
    _command(
        service,
        "promote_memory",
        {
            "memory_id": "policy-preference",
            "expected_generation": 0,
            "storage_class": "portable",
        },
    )

    reindex = service.reindex()
    assert reindex.rows > 0
    assert service.recall("policy-gated", 10).hits
    allowed_current = service.context("privacy-work", "portable")
    allowed_profile = service.profile(
        {"type": "project", "project_id": project.id}
    )
    assert "policy-gated session marker" in allowed_current.text
    assert "policy-gated preference marker" in allowed_profile.text
    assert "policy-gated session marker" in (
        vault.local_root / "views" / "CURRENT.md"
    ).read_text(encoding="utf-8")
    assert "policy-gated preference marker" in (
        vault.local_root / "views" / "PROFILE.md"
    ).read_text(encoding="utf-8")

    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            "UPDATE recall_meta SET policy_status = 'allowed' "
            "WHERE category IN ('session', 'memory')"
        )
        connection.commit()
    stale_index_digest = sha256(service.index.db_path.read_bytes()).hexdigest()

    before_tightening = _portable_text(vault)
    for private_value in (*local_values, local_id, local_count, local_digest):
        assert private_value not in before_tightening

    restrictive = _command(
        service,
        "create_policy_revision",
        {
            "policy_id": "source-policy",
            "revision": "2",
            "rule": {"ceiling": "local-only"},
            "storage_class": "portable",
        },
    )
    restrictive_ref = restrictive.result["policy_ref"]
    _command(
        service,
        "activate_policy_revision",
        {"expected_generation": 2, "policy_ref": restrictive_ref},
    )
    _command(
        service,
        "assign_project_policy",
        {
            "project_id": project.id,
            "policy_ref": restrictive_ref,
            "expected_generation": 1,
            "storage_class": "portable",
        },
    )
    _command(
        service,
        "assign_source_policy",
        {
            "source_id": "release-source",
            "policy_ref": restrictive_ref,
            "expected_generation": 1,
            "storage_class": "portable",
        },
    )

    assert sha256(service.index.db_path.read_bytes()).hexdigest() == stale_index_digest
    with sqlite3.connect(service.index.db_path) as connection:
        stale_hints = {
            row[0]
            for row in connection.execute(
                "SELECT DISTINCT policy_status FROM recall_meta "
                "WHERE category IN ('session', 'memory')"
            )
        }
    assert stale_hints == {"allowed"}

    recall = service.recall("policy-gated", 10)
    current = service.context("privacy-work", "portable")
    profile = service.profile({"type": "project", "project_id": project.id})
    current_authorization = service.authorizer.authorize_context_view(
        "privacy-work", mode="portable"
    )
    profile_authorization = service.authorizer.authorize_profile_view(
        {"type": "project", "project_id": project.id}
    )
    session_ref = PolicyArtifactRef.session(
        "privacy-work",
        session.result["session_id"],
        session.result["revision"],
    )
    export = service.authorize_export(session_ref)
    preflight = sync_preflight(
        vault,
        (
            "workstreams/privacy-work/sessions/privacy-session/000001.md",
            "memory/policy-preference.md",
        ),
    )

    assert recall.hits == ()
    assert "policy-gated session marker" not in current.text
    assert "policy-gated preference marker" not in profile.text
    assert current_authorization.allowed is False
    assert profile_authorization.allowed is False
    assert vault.views.load(
        "CURRENT.md", authorization=current_authorization
    ) is None
    assert vault.views.load(
        "PROFILE.md", authorization=profile_authorization
    ) is None
    assert export.allowed is False
    assert export.issue_codes == ("policy-current-denied",)
    assert preflight.allowed is False
    assert "policy-current-denied" in preflight.blocker_codes

    retirement = _command(
        service,
        "retire_memory",
        {
            "memory_id": "policy-preference",
            "expected_generation": 1,
            "reason": "Current source policy is more restrictive",
            "privacy_remediation": True,
            "storage_class": "portable",
        },
    )
    assert retirement.result["status"] == "retired"
    remediation_record = (
        vault.root / "memory" / "policy-preference.md"
    ).read_text(encoding="utf-8")
    assert "Prior Git propagation cannot be erased." in remediation_record

    after_tightening = _portable_text(vault)
    for private_value in (*local_values, local_id, local_count, local_digest):
        assert private_value not in after_tightening


def test_unavailable_core_never_blocks_claude_or_codex(tmp_path):
    """Stopping Core must warn both host adapters without blocking ordinary work."""
    from mneme.adapters.claude import ClaudeAdapter
    from mneme.adapters.codex import CodexAdapter

    class StoppedCore:
        def observe(self, _event):
            raise ConnectionError("local Core is stopped")

    project = tmp_path / "project"
    project.mkdir()
    claude = ClaudeAdapter(StoppedCore(), project_root=project).handle(
        {
            "version": 1,
            "event": "pre_compact",
            "adapter": "claude",
            "session_id": "claude-1",
            "workstream_id": "privacy-work",
        }
    )
    native_fixture = (
        Path(__file__).parents[1] / "fixtures" / "codex" / "pre_compact.native.json"
    )
    native = json.loads(native_fixture.read_text(encoding="utf-8"))
    codex = CodexAdapter(StoppedCore(), project_root=project).handle_native(
        native, {"workstream_id": "privacy-work"}
    )

    assert claude.ok is codex.ok is False
    assert claude.block_host is codex.block_host is False
    assert claude.warning == "Madi is unavailable; continue ordinary Claude work."
    assert codex.warning == "Madi is unavailable; continue ordinary Codex work."
