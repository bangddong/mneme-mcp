from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest


def test_portability_values_are_ordered_from_local_to_shareable():
    from mneme.core.policy import Portability

    assert Portability.LOCAL_ONLY < Portability.PERSONAL_VAULT < Portability.SHAREABLE


def test_agent_can_reduce_but_not_raise_source_ceiling():
    from mneme.core.policy import PolicyRule, Portability, evaluate_portability

    source = PolicyRule("source", "7", Portability.LOCAL_ONLY, "digest-source")

    denied = evaluate_portability(Portability.PERSONAL_VAULT, (source,), "body-hash")
    lowered = evaluate_portability(Portability.LOCAL_ONLY, (source,), "body-hash")

    assert denied.allowed is False
    assert denied.effective_ceiling is Portability.LOCAL_ONLY
    assert denied.violations[0].code == "requested-exceeds-effective-ceiling"
    assert lowered.allowed is True


def test_unknown_policy_fails_closed_for_portable_write():
    from mneme.core.policy import Portability, UnknownPolicy, evaluate_portability

    with pytest.raises(UnknownPolicy) as raised:
        evaluate_portability(Portability.PERSONAL_VAULT, (), "body-hash")

    assert raised.value.violations[0].code == "unknown-policy"


def test_local_only_request_does_not_require_an_available_policy():
    from mneme.core.policy import Portability, evaluate_portability

    result = evaluate_portability(Portability.LOCAL_ONLY, (), "body-hash")

    assert result.allowed is True
    assert result.effective_ceiling is Portability.LOCAL_ONLY
    assert result.refs == ()


def test_opaque_attestation_replaces_confidential_policy_reference_in_receipt():
    from mneme.core.policy import (
        OpaquePolicyAttestation,
        PolicyRule,
        Portability,
        evaluate_portability,
    )

    rule = PolicyRule(
        "customer-source-42",
        "7",
        Portability.PERSONAL_VAULT,
        "digest-source",
        opaque_attestation=OpaquePolicyAttestation("attestation-7", "audit-digest"),
    )

    receipt = evaluate_portability(Portability.PERSONAL_VAULT, (rule,), "body-hash")

    assert receipt.allowed is True
    assert receipt.refs == (OpaquePolicyAttestation("attestation-7", "audit-digest"),)
    assert "customer-source-42" not in repr(receipt)


def test_reevaluation_creates_a_new_denied_receipt_without_mutating_admission():
    from mneme.core.policy import (
        OpaquePolicyAttestation,
        PolicyRule,
        Portability,
        evaluate_portability,
        reevaluate_portability,
    )

    attestation = OpaquePolicyAttestation("attestation-7", "audit-digest")
    admitted = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (
            PolicyRule(
                "customer-source-42",
                "7",
                Portability.PERSONAL_VAULT,
                "digest-source-v7",
                opaque_attestation=attestation,
            ),
        ),
        "body-hash",
    )

    reevaluated = reevaluate_portability(
        admitted,
        (
            PolicyRule(
                "customer-source-42",
                "8",
                Portability.LOCAL_ONLY,
                "digest-source-v8",
                opaque_attestation=attestation,
            ),
        ),
    )

    assert admitted.allowed is True
    assert admitted.effective_ceiling is Portability.PERSONAL_VAULT
    assert admitted.refs == (attestation,)
    assert reevaluated is not admitted
    assert reevaluated.allowed is False
    assert reevaluated.violations[0].code == "requested-exceeds-effective-ceiling"
    assert reevaluated.semantic_hash == "body-hash"


def test_policy_receipt_refs_are_deduplicated_and_sorted_deterministically():
    from mneme.core.policy import PolicyRef, PolicyRule, Portability, evaluate_portability

    rule_b = PolicyRule("vault", "2", Portability.SHAREABLE, "digest-vault")
    rule_a = PolicyRule("project", "1", Portability.PERSONAL_VAULT, "digest-project")

    result = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (rule_b, rule_a, rule_b),
        "body-hash",
    )

    assert result.refs == (
        PolicyRef("project", "1", "digest-project"),
        PolicyRef("vault", "2", "digest-vault"),
    )


@pytest.mark.parametrize(
    ("requested", "inputs", "semantic_hash"),
    [
        ("personal-vault", (), "body-hash"),
        (None, (), "body-hash"),
        (None, [], "body-hash"),
        (None, (), ""),
    ],
)
def test_evaluation_rejects_malformed_runtime_values(requested, inputs, semantic_hash):
    from mneme.core.policy import InvalidPolicy, evaluate_portability

    with pytest.raises(InvalidPolicy):
        evaluate_portability(requested, inputs, semantic_hash)


def test_policy_rule_and_receipt_validate_runtime_values_and_are_immutable():
    from mneme.core.policy import (
        InvalidPolicy,
        PolicyEvaluation,
        PolicyRef,
        PolicyRule,
        Portability,
        evaluate_portability,
    )

    with pytest.raises(InvalidPolicy):
        PolicyRule("source", "7", "local-only", "digest-source")
    with pytest.raises(InvalidPolicy):
        PolicyRef("source", "7", "")
    with pytest.raises(InvalidPolicy):
        PolicyEvaluation(
            Portability.LOCAL_ONLY,
            Portability.LOCAL_ONLY,
            True,
            datetime.now(),
            "madi.policy.v1",
            (),
            "body-hash",
        )

    receipt = evaluate_portability(
        Portability.LOCAL_ONLY,
        (PolicyRule("source", "7", Portability.LOCAL_ONLY, "digest-source"),),
        "body-hash",
    )
    assert receipt.evaluated_at.tzinfo is timezone.utc
    assert receipt.evaluator_version == "madi.policy.v1"
    with pytest.raises(FrozenInstanceError):
        receipt.allowed = False
