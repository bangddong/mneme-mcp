from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timezone

import pytest


HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
ATTESTATION_A = "attest-123e4567-e89b-42d3-a456-426614174000"
ATTESTATION_B = "attest-123e4567-e89b-42d3-a456-426614174001"
DERIVATIVE_RULE = "rule-123e4567-e89b-42d3-a456-426614174002"


def test_portability_values_are_ordered_from_local_to_shareable():
    from mneme.core.policy import Portability

    assert Portability.LOCAL_ONLY < Portability.PERSONAL_VAULT < Portability.SHAREABLE


def test_agent_can_reduce_but_not_raise_source_ceiling():
    from mneme.core.policy import PolicyRule, Portability, evaluate_portability

    source = PolicyRule("source", "7", Portability.LOCAL_ONLY, HASH_A)

    denied = evaluate_portability(Portability.PERSONAL_VAULT, (source,), HASH_D)
    lowered = evaluate_portability(Portability.LOCAL_ONLY, (source,), HASH_D)

    assert denied.allowed is False
    assert denied.effective_ceiling is Portability.LOCAL_ONLY
    assert denied.violations[0].code == "requested-exceeds-effective-ceiling"
    assert lowered.allowed is True


def test_unknown_policy_fails_closed_for_portable_write():
    from mneme.core.policy import Portability, UnknownPolicy, evaluate_portability

    with pytest.raises(UnknownPolicy) as raised:
        evaluate_portability(Portability.PERSONAL_VAULT, (), HASH_D)

    assert raised.value.violations[0].code == "unknown-policy"


def test_local_only_request_does_not_require_an_available_policy():
    from mneme.core.policy import Portability, evaluate_portability

    result = evaluate_portability(Portability.LOCAL_ONLY, (), HASH_D)

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
        HASH_A,
        opaque_attestation=OpaquePolicyAttestation.create(
            ATTESTATION_A, "7", Portability.PERSONAL_VAULT
        ),
    )

    receipt = evaluate_portability(Portability.PERSONAL_VAULT, (rule,), HASH_D)

    assert receipt.allowed is True
    assert receipt.refs == (
        OpaquePolicyAttestation.create(ATTESTATION_A, "7", Portability.PERSONAL_VAULT),
    )
    assert "customer-source-42" not in repr(receipt)


def test_reevaluation_creates_a_new_denied_receipt_without_mutating_admission():
    from mneme.core.policy import (
        OpaquePolicyAttestation,
        PolicyRule,
        Portability,
        evaluate_portability,
        reevaluate_portability,
    )

    attestation = OpaquePolicyAttestation.create(
        ATTESTATION_A, "7", Portability.PERSONAL_VAULT
    )
    admitted = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (
            PolicyRule(
                "customer-source-42",
                "7",
                Portability.PERSONAL_VAULT,
                HASH_A,
                opaque_attestation=attestation,
            ),
        ),
        HASH_D,
    )

    reevaluated = reevaluate_portability(
        admitted,
        (
            PolicyRule(
                "customer-source-42",
                "8",
                Portability.LOCAL_ONLY,
                HASH_C,
                opaque_attestation=OpaquePolicyAttestation.create(
                    ATTESTATION_B, "8", Portability.LOCAL_ONLY
                ),
            ),
        ),
    )

    assert admitted.allowed is True
    assert admitted.effective_ceiling is Portability.PERSONAL_VAULT
    assert admitted.refs == (attestation,)
    assert reevaluated is not admitted
    assert reevaluated.allowed is False
    assert reevaluated.violations[0].code == "requested-exceeds-effective-ceiling"
    assert reevaluated.semantic_hash == HASH_D


def test_policy_receipt_refs_are_deduplicated_and_sorted_deterministically():
    from mneme.core.policy import PolicyRef, PolicyRule, Portability, evaluate_portability

    rule_b = PolicyRule("vault", "2", Portability.SHAREABLE, HASH_A)
    rule_a = PolicyRule("project", "1", Portability.PERSONAL_VAULT, HASH_B)

    result = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (rule_b, rule_a, rule_b),
        HASH_D,
    )

    assert result.refs == (
        PolicyRef("project", "1", HASH_B),
        PolicyRef("vault", "2", HASH_A),
    )


@pytest.mark.parametrize("requested", ["personal-vault", None])
def test_evaluation_rejects_non_enum_requested_portability(requested):
    from mneme.core.policy import InvalidPolicy, evaluate_portability

    with pytest.raises(InvalidPolicy):
        evaluate_portability(requested, (), HASH_D)


def test_evaluation_rejects_non_tuple_policy_inputs_before_evaluating_ceiling():
    from mneme.core.policy import InvalidPolicy, Portability, evaluate_portability

    with pytest.raises(InvalidPolicy, match="tuple"):
        evaluate_portability(Portability.LOCAL_ONLY, [], HASH_D)


def test_evaluation_rejects_non_rule_tuple_entries_before_evaluating_ceiling():
    from mneme.core.policy import InvalidPolicy, Portability, evaluate_portability

    with pytest.raises(InvalidPolicy, match="PolicyRule"):
        evaluate_portability(Portability.LOCAL_ONLY, (object(),), HASH_D)


@pytest.mark.parametrize(
    "unsafe_value",
    ["customer-source-42", "C:/customer/source", "https://internal.example/source", "attest secret"],
)
def test_opaque_attestation_rejects_free_text_and_source_identifier_leaks(unsafe_value):
    from mneme.core.policy import InvalidPolicy, OpaquePolicyAttestation, Portability

    with pytest.raises(InvalidPolicy):
        OpaquePolicyAttestation.create(unsafe_value, "7", Portability.PERSONAL_VAULT)


def test_opaque_attestation_accepts_generated_token_and_sha256_digest():
    from mneme.core.policy import OpaquePolicyAttestation, Portability

    attestation = OpaquePolicyAttestation.create(
        ATTESTATION_A, "7", Portability.PERSONAL_VAULT
    )

    assert attestation.attestation_id == ATTESTATION_A
    assert attestation.digest != HASH_A


def test_opaque_attestation_recomputes_portable_digest_and_rejects_source_hash():
    from mneme.core.policy import InvalidPolicy, OpaquePolicyAttestation, Portability

    attestation = OpaquePolicyAttestation.create(
        ATTESTATION_A, "7", Portability.PERSONAL_VAULT
    )

    assert attestation.digest != HASH_A
    with pytest.raises(InvalidPolicy):
        OpaquePolicyAttestation.from_receipt(
            ATTESTATION_A, "7", Portability.PERSONAL_VAULT, HASH_A
        )


def test_receipt_owns_validated_attestation_copy_after_caller_mutation():
    from mneme.core.policy import OpaquePolicyAttestation, PolicyRule, Portability, evaluate_portability

    caller_attestation = OpaquePolicyAttestation.create(
        ATTESTATION_A, "7", Portability.PERSONAL_VAULT
    )
    caller_rule = PolicyRule(
        "customer-source-42",
        "7",
        Portability.PERSONAL_VAULT,
        HASH_A,
        opaque_attestation=caller_attestation,
    )
    receipt = evaluate_portability(Portability.PERSONAL_VAULT, (caller_rule,), HASH_D)
    serialized_before = asdict(receipt)

    object.__setattr__(caller_attestation, "digest", HASH_A)
    object.__setattr__(caller_rule, "opaque_attestation", None)

    assert asdict(receipt) == serialized_before
    assert receipt.refs[0] is not caller_attestation
    assert HASH_A not in repr(receipt)


def test_approved_rule_provenance_is_bound_to_the_authorizing_policy_reference():
    from mneme.core.policy import (
        ApprovedRuleProvenance,
        PolicyRef,
        PolicyRule,
        Portability,
        evaluate_portability,
    )

    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (
            PolicyRule(
                "source",
                "7",
                Portability.PERSONAL_VAULT,
                HASH_A,
                approved_rule_id=DERIVATIVE_RULE,
            ),
        ),
        HASH_D,
    )

    assert receipt.approved_rules == (
        ApprovedRuleProvenance(
            DERIVATIVE_RULE,
            PolicyRef("source", "7", HASH_A),
        ),
    )
    assert receipt.approved_rule_ids == (DERIVATIVE_RULE,)


def test_receipt_rejects_unbound_approved_rule_identifier():
    from mneme.core.policy import InvalidPolicy, PolicyEvaluation, Portability

    with pytest.raises(InvalidPolicy):
        PolicyEvaluation(
            Portability.LOCAL_ONLY,
            Portability.LOCAL_ONLY,
            True,
            datetime.now(timezone.utc),
            "madi.policy.v1",
            (),
            HASH_D,
            approved_rules=(DERIVATIVE_RULE,),
        )


def test_same_approved_rule_with_conflicting_authorizers_fails_closed():
    from mneme.core.policy import InvalidPolicy, PolicyRule, Portability, evaluate_portability

    first = PolicyRule(
        "source-a",
        "7",
        Portability.PERSONAL_VAULT,
        HASH_A,
        approved_rule_id=DERIVATIVE_RULE,
    )
    second = PolicyRule(
        "source-b",
        "7",
        Portability.PERSONAL_VAULT,
        HASH_B,
        approved_rule_id=DERIVATIVE_RULE,
    )

    with pytest.raises(InvalidPolicy, match="authorizer"):
        evaluate_portability(Portability.PERSONAL_VAULT, (first, second), HASH_D)


def test_direct_receipt_keeps_copied_raw_approved_rule_authorizer_bound_to_refs():
    from mneme.core.policy import (
        ApprovedRuleProvenance,
        PolicyEvaluation,
        PolicyRef,
        Portability,
    )

    caller_ref = PolicyRef("source-a", "7", HASH_A)
    receipt = PolicyEvaluation(
        Portability.PERSONAL_VAULT,
        Portability.PERSONAL_VAULT,
        True,
        datetime.now(timezone.utc),
        "madi.policy.v1",
        (caller_ref,),
        HASH_D,
        approved_rules=(ApprovedRuleProvenance(DERIVATIVE_RULE, caller_ref),),
    )
    expected = asdict(receipt)

    object.__setattr__(caller_ref, "policy_id", "source-b")

    assert asdict(receipt) == expected
    assert receipt.approved_rules[0].authorizer == receipt.refs[0]
    assert receipt.approved_rules[0].authorizer is not caller_ref


def test_direct_receipt_rejects_raw_approved_rule_authorizer_absent_from_refs():
    from mneme.core.policy import (
        ApprovedRuleProvenance,
        InvalidPolicy,
        PolicyEvaluation,
        PolicyRef,
        Portability,
    )

    with pytest.raises(InvalidPolicy, match="authorizer.*refs"):
        PolicyEvaluation(
            Portability.PERSONAL_VAULT,
            Portability.PERSONAL_VAULT,
            True,
            datetime.now(timezone.utc),
            "madi.policy.v1",
            (PolicyRef("source-a", "7", HASH_A),),
            HASH_D,
            approved_rules=(
                ApprovedRuleProvenance(
                    DERIVATIVE_RULE, PolicyRef("source-b", "7", HASH_B)
                ),
            ),
        )


def test_direct_receipt_keeps_copied_opaque_approved_rule_authorizer_bound_to_refs():
    from mneme.core.policy import (
        ApprovedRuleProvenance,
        OpaquePolicyAttestation,
        PolicyEvaluation,
        Portability,
    )

    caller_attestation = OpaquePolicyAttestation.create(
        ATTESTATION_A, "7", Portability.PERSONAL_VAULT
    )
    receipt = PolicyEvaluation(
        Portability.PERSONAL_VAULT,
        Portability.PERSONAL_VAULT,
        True,
        datetime.now(timezone.utc),
        "madi.policy.v1",
        (caller_attestation,),
        HASH_D,
        approved_rules=(
            ApprovedRuleProvenance(DERIVATIVE_RULE, caller_attestation),
        ),
    )
    expected = asdict(receipt)

    object.__setattr__(caller_attestation, "digest", HASH_A)

    assert asdict(receipt) == expected
    assert receipt.approved_rules[0].authorizer == receipt.refs[0]
    assert receipt.approved_rules[0].authorizer is not caller_attestation


def test_direct_receipt_rejects_opaque_approved_rule_authorizer_absent_from_refs():
    from mneme.core.policy import (
        ApprovedRuleProvenance,
        InvalidPolicy,
        OpaquePolicyAttestation,
        PolicyEvaluation,
        Portability,
    )

    in_receipt = OpaquePolicyAttestation.create(
        ATTESTATION_A, "7", Portability.PERSONAL_VAULT
    )
    absent = OpaquePolicyAttestation.create(
        ATTESTATION_B, "7", Portability.PERSONAL_VAULT
    )

    with pytest.raises(InvalidPolicy, match="authorizer.*refs"):
        PolicyEvaluation(
            Portability.PERSONAL_VAULT,
            Portability.PERSONAL_VAULT,
            True,
            datetime.now(timezone.utc),
            "madi.policy.v1",
            (in_receipt,),
            HASH_D,
            approved_rules=(ApprovedRuleProvenance(DERIVATIVE_RULE, absent),),
        )


@pytest.mark.parametrize("unsafe_revision", ["../7", "C:/policy", "https://policy", "revision 7"])
def test_policy_ref_and_rule_reject_unsafe_policy_revision_tokens(unsafe_revision):
    from mneme.core.policy import InvalidPolicy, PolicyRef, PolicyRule, Portability

    with pytest.raises(InvalidPolicy):
        PolicyRef("source", unsafe_revision, HASH_A)
    with pytest.raises(InvalidPolicy):
        PolicyRule("source", unsafe_revision, Portability.LOCAL_ONLY, HASH_A)


@pytest.mark.parametrize("opaque_first", [False, True])
def test_same_policy_cannot_be_supplied_as_both_opaque_and_raw_provenance(opaque_first):
    from mneme.core.policy import (
        InvalidPolicy,
        OpaquePolicyAttestation,
        PolicyRule,
        Portability,
        evaluate_portability,
    )

    raw = PolicyRule("source", "7", Portability.PERSONAL_VAULT, HASH_A)
    opaque = PolicyRule(
        "source",
        "7",
        Portability.PERSONAL_VAULT,
        HASH_A,
        opaque_attestation=OpaquePolicyAttestation.create(
            ATTESTATION_A, "7", Portability.PERSONAL_VAULT
        ),
    )
    rules = (opaque, raw) if opaque_first else (raw, opaque)

    with pytest.raises(InvalidPolicy, match="opaque and raw"):
        evaluate_portability(Portability.PERSONAL_VAULT, rules, HASH_D)


@pytest.mark.parametrize("conflict", ["digest", "ceiling", "attestation"])
def test_conflicting_policy_identity_fields_fail_closed(conflict):
    from mneme.core.policy import (
        InvalidPolicy,
        OpaquePolicyAttestation,
        PolicyRule,
        Portability,
        evaluate_portability,
    )

    if conflict == "digest":
        conflicting_rules = (
            PolicyRule("source", "7", Portability.PERSONAL_VAULT, HASH_A),
            PolicyRule("source", "7", Portability.PERSONAL_VAULT, HASH_B),
        )
    elif conflict == "ceiling":
        conflicting_rules = (
            PolicyRule("source", "7", Portability.PERSONAL_VAULT, HASH_A),
            PolicyRule("source", "7", Portability.LOCAL_ONLY, HASH_A),
        )
    else:
        conflicting_rules = (
            PolicyRule(
                "source",
                "7",
                Portability.PERSONAL_VAULT,
                HASH_A,
                OpaquePolicyAttestation.create(
                    ATTESTATION_A, "7", Portability.PERSONAL_VAULT
                ),
            ),
            PolicyRule(
                "source",
                "7",
                Portability.PERSONAL_VAULT,
                HASH_A,
                OpaquePolicyAttestation.create(
                    ATTESTATION_B, "7", Portability.PERSONAL_VAULT
                ),
            ),
        )

    with pytest.raises(InvalidPolicy):
        evaluate_portability(Portability.PERSONAL_VAULT, conflicting_rules, HASH_D)


def test_duplicate_identical_rules_are_deduplicated_without_changing_receipt():
    from mneme.core.policy import PolicyRule, Portability, evaluate_portability

    rule = PolicyRule("source", "7", Portability.PERSONAL_VAULT, HASH_A)

    result = evaluate_portability(Portability.PERSONAL_VAULT, (rule, rule), HASH_D)

    assert result.refs == (rule.ref,)


def test_approved_sanitized_derivative_rule_is_retained_immutably_in_receipt():
    from mneme.core.policy import PolicyRule, Portability, evaluate_portability

    receipt = evaluate_portability(
        Portability.PERSONAL_VAULT,
        (
            PolicyRule(
                "source",
                "7",
                Portability.PERSONAL_VAULT,
                HASH_A,
                approved_rule_id=DERIVATIVE_RULE,
            ),
        ),
        HASH_D,
    )

    assert receipt.approved_rule_ids == (DERIVATIVE_RULE,)
    with pytest.raises(FrozenInstanceError):
        receipt.approved_rules = ()


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
            HASH_D,
        )

    receipt = evaluate_portability(
        Portability.LOCAL_ONLY,
        (PolicyRule("source", "7", Portability.LOCAL_ONLY, HASH_A),),
        HASH_D,
    )
    assert receipt.evaluated_at.tzinfo is timezone.utc
    assert receipt.evaluator_version == "madi.policy.v1"
    with pytest.raises(FrozenInstanceError):
        receipt.allowed = False


def test_policy_inputs_and_frozen_values_cannot_mutate_an_existing_receipt():
    from mneme.core.policy import PolicyRule, Portability, evaluate_portability

    rule = PolicyRule("source", "7", Portability.PERSONAL_VAULT, HASH_A)
    caller_rules = [rule]
    receipt = evaluate_portability(Portability.PERSONAL_VAULT, tuple(caller_rules), HASH_D)
    caller_rules.clear()

    assert receipt.refs == (rule.ref,)
    with pytest.raises(FrozenInstanceError):
        rule.ceiling = Portability.LOCAL_ONLY
    with pytest.raises(FrozenInstanceError):
        rule.ref.policy_id = "other"
