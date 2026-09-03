import sys
import sqlite3
from hashlib import sha256

import pytest


@pytest.fixture
def vault_with_memory(tmp_path):
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.vault import Vault

    vault = Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")
    body = "Checkpoint recall remains deterministic without a generation model."
    default = PolicyStore(vault).load_active(
        "vault-default", __import__("mneme.core.artifacts", fromlist=["StorageClass"]).StorageClass.PORTABLE
    )
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (PolicyStore(vault).load_rule(default),),
        memory_semantic_hash(body=body),
    )
    record = MemoryStore(vault).submit_candidate(
        "knowledge", {"type": "personal-global"}, "personal", "personal-vault", body, receipt
    )
    MemoryStore(vault).promote(record.id, 0, receipt)
    return vault


def test_generated_db_can_be_deleted_and_rebuilt(vault_with_memory, monkeypatch):
    """Catches generated recall state becoming a durable or LLM-dependent authority."""
    from mneme.core.search.index import GeneratedIndex

    monkeypatch.setitem(sys.modules, "mneme.llm", None)
    index = GeneratedIndex(vault_with_memory.local_root / "index/state.db")
    index.rebuild(vault_with_memory, ())
    first = index.search("checkpoint", 10)
    index.db_path.unlink()
    index.rebuild(vault_with_memory, ())

    assert index.search("checkpoint", 10) == first


def test_core_recall_reloads_the_canonical_memory_before_exposure(vault_with_memory):
    """Catches a stale FTS row exposing a memory whose canonical artifact changed."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    first = service.recall("checkpoint", 10)
    memory = next((vault_with_memory.root / "memory").glob("*.md"))
    memory.write_text("---\nschema: madi.memory.v1\n---\ntampered", encoding="utf-8")

    second = service.recall("checkpoint", 10)

    assert first.hits
    assert second.hits == ()
    assert second.status == "degraded"


def test_core_recall_withholds_a_stale_registry_candidate(vault_with_memory):
    """Catches a cache row exposing source metadata after its registry changes."""
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService

    RegistryStore(vault_with_memory).register_source("source-01", authority="external-reference")
    service = CoreService(vault_with_memory)
    service.reindex()
    source = vault_with_memory.root / "sources" / "source-01.yaml"
    source.write_text(
        source.read_text(encoding="utf-8").replace("external-reference", "project"),
        encoding="utf-8",
    )

    result = service.recall("external-reference", 10)

    assert result.hits == ()
    assert result.status == "degraded"


def test_core_recall_withholds_tampered_source_provenance(vault_with_memory, tmp_path):
    """Catches generated source metadata granting provenance without canonical agreement."""
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService

    checkout = tmp_path / "project-checkout"
    checkout.mkdir()
    (checkout / "runbook.md").write_text("Source checkpoint procedure.", encoding="utf-8")
    registries = RegistryStore(vault_with_memory)
    source = registries.register_source("source-01", authority="external-reference")
    registries.bind_source(source.id, checkout)
    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            "UPDATE recall_meta SET authority = 'personal' WHERE category = 'source'"
        )

    result = service.recall("source checkpoint", 10)

    assert result.hits == ()
    assert result.status == "degraded"


def test_core_recall_withholds_tampered_memory_provenance(vault_with_memory):
    """Catches generated memory metadata replacing the canonical authority axes."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            """
            UPDATE recall_meta
            SET authority = 'external-reference', portability = 'local-only',
                scope = '{"type": "workstream", "workstream_id": "forged"}'
            WHERE category = 'memory'
            """
        )

    result = service.recall("checkpoint", 10)

    assert result.hits == ()
    assert result.status == "degraded"


def test_core_recall_withholds_tampered_registry_provenance(vault_with_memory):
    """Catches a generated registry row overriding its canonical authority."""
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService

    RegistryStore(vault_with_memory).register_source(
        "source-01", authority="external-reference"
    )
    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            "UPDATE recall_meta SET authority = 'personal' WHERE category = 'source-registry'"
        )

    result = service.recall("external-reference", 10)

    assert result.hits == ()
    assert result.status == "degraded"


def test_withheld_candidate_does_not_consume_the_authorized_result_limit(
    vault_with_memory,
):
    """Catches an untrusted top-ranked cache row hiding a valid canonical result."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            "INSERT INTO recall_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "memory:forged",
                "memory",
                "forged",
                None,
                None,
                None,
                "personal",
                "personal-vault",
                "recorded",
                sha256(b"checkpoint").hexdigest(),
                '{"type": "personal-global"}',
                0,
            ),
        )
        connection.execute(
            "INSERT INTO recall_fts VALUES (?, ?)", ("memory:forged", "checkpoint")
        )

    result = service.recall("checkpoint", 1)

    assert len(result.hits) == 1
    assert result.hits[0].category == "memory"
    assert result.status == "degraded"


def test_core_recall_withholds_excerpt_not_present_in_canonical_content(
    vault_with_memory,
):
    """Catches forged FTS text passing via an untampered metadata hash."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    forged = "forged checkpoint disclosure"
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute(
            "UPDATE recall_fts SET content = ? "
            "WHERE key IN (SELECT key FROM recall_meta WHERE category = 'memory')",
            (forged,),
        )
        connection.execute(
            "UPDATE recall_meta SET content_hash = ? WHERE category = 'memory'",
            (sha256(forged.encode("utf-8")).hexdigest(),),
        )

    result = service.recall("checkpoint", 10)

    assert result.hits == ()
    assert result.status == "degraded"


def test_candidate_successor_does_not_displace_an_accepted_recall_hit(
    vault_with_memory,
):
    """Catches an unaccepted successor hiding the current accepted memory."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.service import CoreService

    store = MemoryStore(vault_with_memory)
    original = next(record for record in store.iter_records() if record.status.value == "accepted")
    successor_body = "Candidate replacement remains under review."
    policies = PolicyStore(vault_with_memory)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (policies.load_rule(policies.load_active("vault-default", StorageClass.PORTABLE)),),
        memory_semantic_hash(body=successor_body, supersedes=original.id),
    )
    store.supersede(
        original.id,
        original.generation,
        body=successor_body,
        receipt=receipt,
    )
    service = CoreService(vault_with_memory)
    service.reindex()

    result = service.recall("checkpoint", 10)

    assert len(result.hits) == 1
    assert result.hits[0].artifact_id == original.id


def test_core_recall_requires_current_registry_policy_reference(vault_with_memory):
    """Catches registry metadata exposure after its assigned policy disappears."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService

    registries = RegistryStore(vault_with_memory)
    source = registries.register_source("source-01", authority="external-reference")
    policy = PolicyStore(vault_with_memory).create_revision(
        "source-policy", "v1", {"ceiling": "personal-vault"}, StorageClass.PORTABLE
    )
    registries.assign_source_policy(
        source.id, policy, expected_generation=source.generation
    )
    service = CoreService(vault_with_memory)
    service.reindex()
    (vault_with_memory.root / ".madi/policies/source-policy/v1.yaml").unlink()

    result = service.recall("external-reference", 10)

    assert result.hits == ()
    assert result.status == "degraded"


def test_non_sqlite_cache_is_rebuilt_instead_of_returning_resolved_empty(
    vault_with_memory,
):
    """Catches a corrupt generated database being treated as a successful empty search."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    service.index.db_path.write_bytes(b"this is not sqlite")

    result = service.recall("checkpoint", 10)

    assert result.status == "resolved"
    assert len(result.hits) == 1
    assert result.hits[0].category == "memory"


def test_malformed_cached_scope_is_rebuilt_without_escaping_json_error(
    vault_with_memory,
):
    """Catches malformed generated row JSON escaping the recall boundary."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute("UPDATE recall_meta SET scope = '{malformed'")

    result = service.recall("checkpoint", 10)

    assert result.status == "resolved"
    assert len(result.hits) == 1
    assert result.hits[0].scope == {"type": "personal-global"}


def test_malformed_cached_excerpt_base_is_rebuilt_before_candidates(
    vault_with_memory,
):
    """Catches a non-integer offset becoming an indistinguishable empty result."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute("UPDATE recall_meta SET excerpt_base = 'bad'")

    result = service.recall("checkpoint", 10)

    assert result.status == "resolved"
    assert len(result.hits) == 1
    assert result.hits[0].category == "memory"


def test_malformed_cached_fts_content_is_rebuilt_without_decoder_exception(
    vault_with_memory,
):
    """Catches a non-text FTS value escaping candidate decoding as AttributeError."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute("UPDATE recall_fts SET content = 42")

    result = service.recall("42", 10)
    with sqlite3.connect(service.index.db_path) as connection:
        stored_type, content = connection.execute(
            "SELECT typeof(content), content FROM recall_fts"
        ).fetchone()

    assert result.status == "resolved"
    assert result.hits == ()
    assert stored_type == "text"
    assert "Checkpoint recall" in content


def test_malformed_cached_identifier_is_rebuilt_without_domain_exception(
    vault_with_memory,
):
    """Catches generated-row domain validation escaping the cache boundary."""
    from mneme.core.service import CoreService

    service = CoreService(vault_with_memory)
    service.reindex()
    with sqlite3.connect(service.index.db_path) as connection:
        connection.execute("UPDATE recall_meta SET artifact_id = 'bad/id'")

    result = service.recall("checkpoint", 10)

    assert result.status == "resolved"
    assert len(result.hits) == 1
    assert result.hits[0].category == "memory"


def test_sqlite_candidate_error_is_a_structured_degradation(vault_with_memory):
    """Catches a non-DatabaseError SQLite failure escaping the recall boundary."""
    from mneme.core.search.index import GeneratedIndex
    from mneme.core.service import CoreService

    class FailingIndex(GeneratedIndex):
        def _search_rows(self, connection, query, limit):
            raise sqlite3.InterfaceError("malformed generated row")

    index = FailingIndex(vault_with_memory.local_root / "index" / "state.db")
    service = CoreService(vault_with_memory, index)
    service.reindex()

    result = service.recall("checkpoint", 10)

    assert result.status == "degraded"
    assert result.hits == ()
    assert result.diagnostics == (
        "generated index is corrupt: InterfaceError",
    )
