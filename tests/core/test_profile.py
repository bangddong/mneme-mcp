import pytest


@pytest.fixture
def vault(tmp_path):
    from mneme.core.vault import Vault

    return Vault.initialize(tmp_path / "vault", tmp_path / "state", "person-01")


def _receipt(vault, body, **semantic):
    from mneme.core.artifacts import StorageClass
    from mneme.core.memories import memory_semantic_hash
    from mneme.core.policy import PolicyStore, Portability, evaluate_portability

    policies = PolicyStore(vault)
    ref = policies.load_active("vault-default", StorageClass.PORTABLE)
    return evaluate_portability(Portability.PERSONAL_VAULT, (policies.load_rule(ref),), memory_semantic_hash(body=body, **semantic))


def test_profile_is_read_only_and_contains_active_accepted_preferences_only(vault):
    from mneme.core.artifacts import StorageClass
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile

    store = MemoryStore(vault)
    active = store.submit_candidate("preference", {"type": "personal-global"}, "personal", "personal-vault", "Use evidence.", _receipt(vault, "Use evidence.", kind="preference"))
    active = store.promote(active.id, 0, _receipt(vault, active.body, kind="preference"))
    lesson = store.submit_candidate("lesson", {"type": "personal-global"}, "personal", "personal-vault", "Test first.", _receipt(vault, "Test first.", kind="lesson"))
    store.promote(lesson.id, 0, _receipt(vault, lesson.body, kind="lesson"))
    retired = store.submit_candidate("preference", {"type": "personal-global"}, "personal", "personal-vault", "Old preference.", _receipt(vault, "Old preference.", kind="preference"))
    retired = store.promote(retired.id, 0, _receipt(vault, retired.body, kind="preference"))
    store.retire(retired.id, 1, "obsolete")

    view = render_profile(MemoryReaders(store), {"type": "personal-global"})
    assert active.id in view.text
    assert "Use evidence." in view.text
    assert lesson.id not in view.text
    assert retired.id not in view.text
    assert not (vault.local_root / "views" / "PROFILE.md").exists()


def test_profile_surfaces_conflicts_without_choosing(vault):
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile

    store = MemoryStore(vault)
    first = store.submit_candidate("preference", {"type": "personal-global"}, "personal", "personal-vault", "Prefer tabs.", _receipt(vault, "Prefer tabs.", kind="preference"))
    second = store.submit_candidate("preference", {"type": "personal-global"}, "personal", "personal-vault", "Prefer spaces.", _receipt(vault, "Prefer spaces.", kind="preference"))
    store.promote(first.id, 0, _receipt(vault, first.body, kind="preference"))
    store.promote(second.id, 0, _receipt(vault, second.body, kind="preference"))

    view = render_profile(MemoryReaders(store), {"type": "personal-global"})
    assert "Conflicts" in view.text
    assert first.id in view.text and second.id in view.text
    assert "chosen" not in view.text.lower()


def test_portable_profile_refuses_a_local_only_reader(vault):
    from mneme.core.artifacts import StorageClass
    from mneme.core.errors import PortabilityViolation
    from mneme.core.memories import MemoryReaders, MemoryStore, memory_semantic_hash, render_profile
    from mneme.core.policy import Portability, evaluate_portability

    local = MemoryStore(vault, StorageClass.LOCAL_ONLY)
    body = "Private local preference."
    local.submit_candidate(
        "preference", {"type": "personal-global"}, "personal", "local-only", body,
        evaluate_portability(Portability.LOCAL_ONLY, (), memory_semantic_hash(
            body=body, kind="preference", portability="local-only"
        )),
    )

    with pytest.raises(PortabilityViolation, match="local-only"):
        render_profile(MemoryReaders(local), {"type": "personal-global"})


def test_service_profile_reads_local_only_preferences_with_the_matching_gate(vault):
    """Catches a portable MemoryStore being hard-coded into all PROFILE authorization."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.memories import MemoryStore, memory_semantic_hash
    from mneme.core.policy import Portability, evaluate_portability
    from mneme.core.service import CoreService

    body = "Local preference must remain available locally."
    receipt = evaluate_portability(
        Portability.LOCAL_ONLY,
        (),
        memory_semantic_hash(
            body=body,
            kind="preference",
            portability="local-only",
        ),
    )
    local = MemoryStore(vault, StorageClass.LOCAL_ONLY)
    record = local.submit_candidate(
        "preference",
        {"type": "personal-global"},
        "personal",
        "local-only",
        body,
        receipt,
    )
    local.promote(record.id, 0, receipt)

    view = CoreService(vault).profile(
        {"type": "personal-global"}, storage_class=StorageClass.LOCAL_ONLY
    )

    assert view.mode == "effective-local"
    assert body in view.text


def test_profile_with_unreadable_canonical_receipt_returns_closed_invalid_view(vault):
    """Catches raw canonical-read paths or exceptions escaping the PROFILE boundary."""
    from mneme.core.artifacts import StorageClass
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile
    from mneme.core.policy import PolicyStore

    body = "Preference that must not escape a broken policy read."
    store = MemoryStore(vault)
    record = store.submit_candidate(
        "preference",
        {"type": "personal-global"},
        "personal",
        "personal-vault",
        body,
        _receipt(vault, body, kind="preference"),
    )
    store.promote(record.id, 0, _receipt(vault, body, kind="preference"))
    active = PolicyStore(vault).load_active("vault-default", StorageClass.PORTABLE)
    policy_path = vault.root / f".madi/policies/{active.policy_id}/{active.revision}.yaml"
    policy_path.write_text("not a valid policy revision\n", encoding="utf-8")

    view = render_profile(MemoryReaders(MemoryStore(vault)), {"type": "personal-global"})

    assert view.status.value == "invalid"
    assert "policy-current-unavailable" in view.text
    assert body not in view.text
    assert str(policy_path) not in view.text


def test_profile_excludes_accepted_superseded_preferences_and_shows_audit_metadata(vault):
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile

    store = MemoryStore(vault)
    original = store.submit_candidate("preference", {"type": "personal-global"}, "personal", "personal-vault", "Prefer short output.", _receipt(vault, "Prefer short output.", kind="preference"))
    original = store.promote(original.id, 0, _receipt(vault, original.body, kind="preference"))
    successor = store.supersede(
        original.id, 1, body="Prefer short output with citations.",
        receipt=_receipt(vault, "Prefer short output with citations.", kind="preference", supersedes=original.id),
    )
    successor = store.promote(successor.id, 0, _receipt(vault, successor.body, kind="preference", supersedes=original.id))

    view = render_profile(MemoryReaders(store), {"type": "personal-global"})

    assert original.id not in view.text
    assert successor.id in view.text
    assert "provenance: none" in view.text
    assert "policy_receipt:" in view.text


def test_retired_accepted_successor_keeps_predecessor_superseded_and_can_be_replaced(vault):
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile

    store = MemoryStore(vault)
    original = store.submit_candidate("preference", {"type": "personal-global"}, "personal", "personal-vault", "Original.", _receipt(vault, "Original.", kind="preference"))
    original = store.promote(original.id, 0, _receipt(vault, original.body, kind="preference"))
    successor = store.supersede(
        original.id, 1, body="Accepted successor.",
        receipt=_receipt(vault, "Accepted successor.", kind="preference", supersedes=original.id),
    )
    successor = store.promote(successor.id, 0, _receipt(vault, successor.body, kind="preference", supersedes=original.id))
    retired = store.retire(successor.id, 1, "obsolete successor")

    retired_view = render_profile(MemoryReaders(store), {"type": "personal-global"})
    assert original.id not in retired_view.text
    assert successor.id not in retired_view.text
    assert retired.accepted_semantic_hash == retired.semantic_hash

    replacement = store.supersede(
        retired.id, 2, body="Later replacement.",
        receipt=_receipt(vault, "Later replacement.", kind="preference", supersedes=retired.id),
    )
    replacement = store.promote(replacement.id, 0, _receipt(vault, replacement.body, kind="preference", supersedes=retired.id))
    final_view = render_profile(MemoryReaders(store), {"type": "personal-global"})

    assert replacement.id in final_view.text
    assert original.id not in final_view.text and successor.id not in final_view.text
    assert store.read(original.id).superseded_by == successor.id
    assert store.read(successor.id).superseded_by == replacement.id


@pytest.mark.parametrize("retire_successor", [False, True])
def test_candidate_successor_does_not_displace_an_active_preference(vault, retire_successor):
    from mneme.core.memories import MemoryReaders, MemoryStore, render_profile

    store = MemoryStore(vault)
    original = store.submit_candidate("preference", {"type": "personal-global"}, "personal", "personal-vault", "Still active.", _receipt(vault, "Still active.", kind="preference"))
    original = store.promote(original.id, 0, _receipt(vault, original.body, kind="preference"))
    candidate = store.supersede(
        original.id, 1, body="Candidate successor only.",
        receipt=_receipt(vault, "Candidate successor only.", kind="preference", supersedes=original.id),
    )
    if retire_successor:
        candidate = store.retire(candidate.id, 0, "never accepted")
        assert candidate.accepted_semantic_hash is None

    view = render_profile(MemoryReaders(store), {"type": "personal-global"})
    assert original.id in view.text
    assert candidate.id not in view.text
