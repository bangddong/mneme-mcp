import pytest
import sqlite3
import threading
from contextlib import contextmanager


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def test_missing_registered_mount_is_a_degraded_reindex_diagnostic(vault):
    """Catches an optional source mount preventing Vault-only recall startup."""
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService

    RegistryStore(vault).register_source("source-01", authority="external-reference")

    report = CoreService(vault).reindex()

    assert report.status == "degraded"
    assert report.diagnostics == ("source source-01 is unavailable",)


def test_missing_registered_mount_degrades_context_after_successful_reindex(vault):
    """Catches an existing generated DB hiding an unavailable optional source."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService
    from mneme.core.sessions import (
        CheckpointRequest,
        SessionBody,
        SessionStore,
        session_semantic_hash,
    )

    registries = RegistryStore(vault)
    registries.register_source("source-01", authority="external-reference")
    registries.create_workstream("ws-01", project=None, mode="single")
    body = SessionBody(
        "codex", "Retain mounted source diagnostics", "working", (), (), (), (), ()
    )
    policies = PolicyStore(vault)
    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (policies.load_rule(policies.load_active("vault-default", StorageClass.PORTABLE)),),
        session_semantic_hash(body, ()),
    )
    SessionStore(vault).create_revision(
        CheckpointRequest(
            "ws-01", "ses-01", StorageClass.PORTABLE, None, 0, body, (), receipt
        )
    )
    service = CoreService(vault)
    assert service.reindex().status == "degraded"

    view = service.context("ws-01")
    recall = service.recall("mounted source diagnostics", 10)

    assert view.status.value == "degraded"
    assert any(hit.category == "session" for hit in recall.hits)
    assert recall.status == "degraded"


def test_malformed_canonical_artifact_is_invalid_not_an_optional_degradation(vault):
    """Catches a broken canonical Vault being classified like a missing mount."""
    from mneme.core.service import CoreService

    (vault.root / "memory" / "broken.md").write_text(
        "---\nschema: invalid\n---\nbroken\n", encoding="utf-8"
    )

    report = CoreService(vault).reindex()

    assert report.status == "invalid"
    assert report.diagnostics == (
        "canonical Vault indexing failed: InvalidArtifact",
    )


def test_invalid_rebuild_never_publishes_partial_source_candidates(vault, tmp_path):
    """Catches a canonical failure publishing otherwise readable mounted-source rows."""
    from mneme.core.registries import RegistryStore
    from mneme.core.service import CoreService

    checkout = tmp_path / "project-checkout"
    checkout.mkdir()
    (checkout / "runbook.md").write_text(
        "partial-source-needle must never survive invalid canonical state",
        encoding="utf-8",
    )
    registries = RegistryStore(vault)
    source = registries.register_source("source-01", authority="external-reference")
    registries.bind_source(source.id, checkout)
    service = CoreService(vault)
    assert service.reindex().status == "resolved"
    assert service.recall("partial-source-needle", 10).hits
    (vault.root / "memory" / "broken.md").write_text(
        "---\nschema: invalid\n---\nbroken\n", encoding="utf-8"
    )

    report = service.reindex()
    warm = service.recall("partial-source-needle", 10)
    direct = service.index.search("partial-source-needle", 10)
    service.index.db_path.unlink()
    cold = CoreService(vault).recall("partial-source-needle", 10)

    assert report.status == "invalid"
    assert report.rows == report.source_rows == 0
    assert direct == []
    assert warm.hits == cold.hits == ()
    assert warm.status == cold.status == "invalid"
    assert warm.diagnostics == cold.diagnostics == report.diagnostics


def test_search_and_rebuild_serialize_the_normal_sqlite_reader(vault):
    """Catches Windows replacement of a database still held by an API search reader."""
    from mneme.core.registries import RegistryStore
    from mneme.core.search.index import GeneratedIndex

    RegistryStore(vault).register_project("project-concurrency")
    entered = threading.Event()
    release = threading.Event()

    class BlockingSearchIndex(GeneratedIndex):
        def _search_rows(self, connection, query, limit):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release the search reader")
            return GeneratedIndex._search_rows(connection, query, limit)

    index = BlockingSearchIndex(vault.local_root / "index" / "state.db")
    index.rebuild(vault, ())
    errors = []
    hits = []

    def search():
        try:
            hits.extend(index.search("concurrency", 10))
        except Exception as exc:
            errors.append(exc)

    def rebuild():
        try:
            index.rebuild(vault, ())
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=search)
    writer = threading.Thread(target=rebuild)
    reader.start()
    assert entered.wait(timeout=5)
    writer.start()
    writer.join(timeout=1)
    release.set()
    reader.join(timeout=5)
    writer.join(timeout=5)

    assert not reader.is_alive() and not writer.is_alive()
    assert errors == []
    assert hits


def test_concurrent_rebuilds_use_independent_staging_databases(vault, monkeypatch):
    """Catches concurrent rebuilds opening and deleting one fixed staging path."""
    from mneme.core.registries import RegistryStore
    from mneme.core.search import index as index_module
    from mneme.core.search.index import GeneratedIndex

    RegistryStore(vault).register_project("project-concurrency")
    db_path = vault.local_root / "index" / "state.db"
    first = GeneratedIndex(db_path)
    second = GeneratedIndex(db_path)
    real_connect = sqlite3.connect
    barrier = threading.Barrier(2)
    stage_paths = []
    stage_guard = threading.Lock()

    def synchronized_connect(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        candidate = str(path)
        if candidate != str(db_path):
            with stage_guard:
                first_open = candidate not in stage_paths
                if first_open:
                    stage_paths.append(candidate)
            if first_open:
                barrier.wait(timeout=5)
        return connection

    monkeypatch.setattr(index_module.sqlite3, "connect", synchronized_connect)
    errors = []

    def rebuild(index):
        try:
            index.rebuild(vault, ())
        except Exception as exc:
            errors.append(exc)

    left = threading.Thread(target=rebuild, args=(first,))
    right = threading.Thread(target=rebuild, args=(second,))
    left.start()
    right.start()
    left.join(timeout=10)
    right.join(timeout=10)

    assert not left.is_alive() and not right.is_alive()
    assert errors == []
    assert len(stage_paths) == 2
    assert len(set(stage_paths)) == 2
    assert first.search("concurrency", 10)


def test_separate_index_instances_serialize_before_the_windows_file_lock(
    vault, monkeypatch
):
    """Catches same-process overlap reaching Windows' bounded file-lock retry."""
    from mneme.core.registries import RegistryStore
    from mneme.core.search import index as index_module
    from mneme.core.search.index import GeneratedIndex

    RegistryStore(vault).register_project("project-process-lock")
    db_path = vault.local_root / "index" / "state.db"
    entered = threading.Event()
    release = threading.Event()
    publishing = threading.Event()
    active_guard = threading.Lock()
    active = False

    class BlockingReader(GeneratedIndex):
        def _search_rows(self, connection, query, limit):
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release the index reader")
            return GeneratedIndex._search_rows(connection, query, limit)

    class SignallingWriter(GeneratedIndex):
        def _publish(self, temporary):
            publishing.set()
            return super()._publish(temporary)

    reader_index = BlockingReader(db_path)
    writer_index = SignallingWriter(db_path)
    reader_index.rebuild(vault, ())

    @contextmanager
    def windows_style_file_lock(_path):
        nonlocal active
        with active_guard:
            if active:
                raise OSError("simulated Windows EDEADLK")
            active = True
        try:
            yield
        finally:
            with active_guard:
                active = False

    monkeypatch.setattr(index_module, "exclusive_file_lock", windows_style_file_lock)
    errors = []

    def search():
        try:
            reader_index.search("process-lock", 10)
        except Exception as exc:
            errors.append(exc)

    def rebuild():
        try:
            writer_index.rebuild(vault, ())
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=search)
    writer = threading.Thread(target=rebuild)
    reader.start()
    assert entered.wait(timeout=5)
    writer.start()
    assert publishing.wait(timeout=5)
    release.set()
    reader.join(timeout=5)
    writer.join(timeout=5)

    assert not reader.is_alive() and not writer.is_alive()
    assert errors == []
    assert writer_index.search("process-lock", 10)


def test_recall_observes_invalid_rebuild_between_setup_and_candidate_snapshot(vault):
    """Catches separate inspect/search locks reporting resolved after invalid publish."""
    from mneme.core.search.index import GeneratedIndex
    from mneme.core.service import CoreService

    entered = threading.Event()
    release = threading.Event()

    class PausingService(CoreService):
        pause = False

        def _bound_sources(self):
            result = super()._bound_sources()
            if self.pause:
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("test did not release recall setup")
            return result

    service = PausingService(vault)
    assert service.reindex().status == "resolved"
    (vault.root / "memory" / "broken.md").write_text(
        "---\nschema: invalid\n---\nbroken\n", encoding="utf-8"
    )
    service.pause = True
    results = []
    errors = []

    def recall():
        try:
            results.append(service.recall("anything", 10))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=recall)
    worker.start()
    assert entered.wait(timeout=5)
    separate = GeneratedIndex(service.index.db_path)
    assert separate.rebuild(vault, ()).status == "invalid"
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1
    assert results[0].status == "invalid"
    assert results[0].hits == ()
