from pathlib import Path

import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def test_project_source_recall_retains_external_authority_and_has_no_write_api(vault, tmp_path):
    """Catches recall treating a mounted project file as Madi-owned content."""
    from mneme.core.registries import RegistryStore
    from mneme.core.search.index import GeneratedIndex
    from mneme.core.service import CoreService

    checkout = tmp_path / "project-checkout"
    checkout.mkdir()
    document = "# Deployment checkpoint\n\nThe project remains authoritative for this decision."
    (checkout / "runbook.md").write_text(document, encoding="utf-8")
    registries = RegistryStore(vault)
    registries.register_project("project-01")
    source = registries.register_source(
        "source-01", project="project-01", authority="external-reference"
    )
    binding = registries.bind_source(source.id, checkout)

    index = GeneratedIndex(vault.local_root / "index" / "state.db")
    index.rebuild(vault, (binding,))
    hits = index.search("deployment checkpoint", 10)

    assert hits
    assert {(hit.source_id, hit.path, hit.authority, hit.policy_status) for hit in hits} == {
        ("source-01", "runbook.md", "external-reference", "available")
    }
    assert {hit.portability for hit in hits} == {"local-only"}
    assert all(hit.excerpt and hit.excerpt_start >= 0 and hit.excerpt_end >= hit.excerpt_start for hit in hits)
    assert all(document[hit.excerpt_start : hit.excerpt_end] == hit.excerpt for hit in hits)
    assert {"write", "write_file", "delete", "delete_file", "save"}.isdisjoint(dir(CoreService(vault)))


def test_korean_expansion_reports_offsets_for_the_actual_source_excerpt(vault, tmp_path):
    """Catches transliterated matches returning an unrelated first-character offset."""
    from mneme.core.registries import RegistryStore
    from mneme.core.search.index import GeneratedIndex

    checkout = tmp_path / "project-checkout"
    checkout.mkdir()
    document = "Operations use Grafana dashboards for deployment evidence."
    (checkout / "observability.md").write_text(document, encoding="utf-8")
    registries = RegistryStore(vault)
    source = registries.register_source("source-01", authority="external-reference")
    binding = registries.bind_source(source.id, checkout)
    index = GeneratedIndex(vault.local_root / "index" / "state.db")
    index.rebuild(vault, (binding,))

    hits = index.search("\uadf8\ub77c\ud30c\ub098", 10)

    assert hits
    assert all("Grafana" in hit.excerpt for hit in hits)
    assert all(document[hit.excerpt_start : hit.excerpt_end] == hit.excerpt for hit in hits)


def test_scoped_search_applies_limit_after_scope_filtering(vault, tmp_path):
    """Catches higher-ranked out-of-scope rows consuming the requested result limit."""
    from mneme.core.registries import RegistryStore
    from mneme.core.search.index import GeneratedIndex

    registries = RegistryStore(vault)
    registries.register_project("project-a")
    registries.register_project("project-b")
    bindings = []
    for number in range(5):
        checkout = tmp_path / f"checkout-a-{number}"
        checkout.mkdir()
        (checkout / "runbook.md").write_text("shared checkpoint term", encoding="utf-8")
        source = registries.register_source(
            f"source-a-{number}",
            project="project-a",
            authority="external-reference",
        )
        bindings.append(registries.bind_source(source.id, checkout))
    target_checkout = tmp_path / "checkout-b"
    target_checkout.mkdir()
    (target_checkout / "runbook.md").write_text("shared checkpoint term", encoding="utf-8")
    target = registries.register_source(
        "source-z", project="project-b", authority="external-reference"
    )
    bindings.append(registries.bind_source(target.id, target_checkout))
    index = GeneratedIndex(vault.local_root / "index" / "state.db")
    index.rebuild(vault, tuple(bindings))

    hits = index.search("checkpoint", 1, {"project_id": "project-b"})

    assert len(hits) == 1
    assert hits[0].source_id == "source-z"
