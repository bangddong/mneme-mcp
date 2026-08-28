"""Pure privacy-policy evaluation and portable-safe admission receipts.

This module deliberately has no storage dependencies.  Policy revisions and active
pointers are registry concerns; this boundary evaluates already-loaded immutable
rules and returns a new immutable receipt for every decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Final, TypeAlias

from mneme.core.errors import InvalidArtifact, MadiError


POLICY_EVALUATOR_VERSION: Final = "madi.policy.v1"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ATTESTATION_ID = re.compile(
    r"^attest-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_APPROVED_RULE_ID = re.compile(
    r"^rule-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_POLICY_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PolicyError(MadiError):
    """Base class for deterministic policy evaluation failures."""


class InvalidPolicy(PolicyError, InvalidArtifact):
    """A policy value or evaluator input has an invalid runtime shape."""


class UnknownPolicy(InvalidPolicy):
    """A portable request cannot be evaluated from available policy inputs."""

    def __init__(self, message: str, *, violations: tuple[PolicyViolation, ...]):
        super().__init__(message)
        self.violations = violations


class Portability(str, Enum):
    """D3 storage ceilings, ordered from most to least restrictive."""

    LOCAL_ONLY = "local-only"
    PERSONAL_VAULT = "personal-vault"
    SHAREABLE = "shareable"

    def _rank(self) -> int:
        return _PORTABILITY_RANK[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Portability):
            return NotImplemented
        return self._rank() < other._rank()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Portability):
            return NotImplemented
        return self._rank() <= other._rank()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Portability):
            return NotImplemented
        return self._rank() > other._rank()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Portability):
            return NotImplemented
        return self._rank() >= other._rank()


_PORTABILITY_RANK: Final = {
    Portability.LOCAL_ONLY: 0,
    Portability.PERSONAL_VAULT: 1,
    Portability.SHAREABLE: 2,
}


def _require_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() and character not in {" ", "-", "_"} for character in value)
    ):
        raise InvalidPolicy(f"{label} must be non-empty text without surrounding whitespace")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
        raise InvalidPolicy(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _require_approved_rule_id(value: object) -> str:
    if not isinstance(value, str) or not _APPROVED_RULE_ID.fullmatch(value):
        raise InvalidPolicy(
            "approved rule id must be a generated rule-UUID identifier"
        )
    return value


def _require_policy_token(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _POLICY_TOKEN.fullmatch(value):
        raise InvalidPolicy(f"{label} must be a safe policy token")
    return value


@dataclass(frozen=True, slots=True)
class PolicyRef:
    """An exact immutable policy-revision identity safe to include in a receipt."""

    policy_id: str
    revision: str
    digest: str

    def __post_init__(self) -> None:
        _require_policy_token(self.policy_id, label="policy id")
        _require_policy_token(self.revision, label="policy revision")
        _require_sha256(self.digest, label="policy digest")


@dataclass(frozen=True, slots=True)
class OpaquePolicyAttestation:
    """Portable-safe proof for a policy whose source identity is confidential.

    The attestation deliberately contains no source, project, or policy identifier.
    Its stable opaque identifier and verification digest retain auditability without
    putting the confidential mapping in the portable receipt.
    """

    attestation_id: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.attestation_id, str) or not _OPAQUE_ATTESTATION_ID.fullmatch(
            self.attestation_id
        ):
            raise InvalidPolicy(
                "opaque attestation id must be a generated attest-UUID identifier"
            )
        _require_sha256(self.digest, label="opaque attestation digest")


# Short alias for callers that do not need the policy-specific spelling.
OpaqueAttestation = OpaquePolicyAttestation
PolicyReceiptRef: TypeAlias = PolicyRef | OpaquePolicyAttestation


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One already-loaded immutable policy rule applicable to a proposed write."""

    policy_id: str
    revision: str
    ceiling: Portability
    digest: str
    opaque_attestation: OpaquePolicyAttestation | None = None
    approved_rule_id: str | None = None

    def __post_init__(self) -> None:
        _require_policy_token(self.policy_id, label="policy id")
        _require_policy_token(self.revision, label="policy revision")
        if not isinstance(self.ceiling, Portability):
            raise InvalidPolicy("policy ceiling must be a Portability")
        _require_sha256(self.digest, label="policy digest")
        if self.opaque_attestation is not None and not isinstance(
            self.opaque_attestation, OpaquePolicyAttestation
        ):
            raise InvalidPolicy(
                "policy opaque_attestation must be an OpaquePolicyAttestation or None"
            )
        if self.approved_rule_id is not None:
            _require_approved_rule_id(self.approved_rule_id)

    @property
    def ref(self) -> PolicyRef:
        return PolicyRef(self.policy_id, self.revision, self.digest)

    @property
    def receipt_ref(self) -> PolicyReceiptRef:
        """Return the disclosure-safe policy provenance for this rule."""
        return self.opaque_attestation if self.opaque_attestation is not None else self.ref


@dataclass(frozen=True, slots=True)
class PolicyViolation:
    """Machine-readable reason an evaluated portability request is not allowed."""

    code: str
    message: str

    def __post_init__(self) -> None:
        _require_text(self.code, label="policy violation code")
        _require_text(self.message, label="policy violation message")


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    """An immutable policy-evaluation receipt for an attempted artifact write."""

    requested: Portability
    effective_ceiling: Portability
    allowed: bool
    evaluated_at: datetime
    evaluator_version: str
    refs: tuple[PolicyReceiptRef, ...]
    semantic_hash: str
    violations: tuple[PolicyViolation, ...] = ()
    approved_rule_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.requested, Portability):
            raise InvalidPolicy("receipt requested portability must be a Portability")
        if not isinstance(self.effective_ceiling, Portability):
            raise InvalidPolicy("receipt effective ceiling must be a Portability")
        if not isinstance(self.allowed, bool):
            raise InvalidPolicy("receipt allowed must be a boolean")
        if (
            not isinstance(self.evaluated_at, datetime)
            or self.evaluated_at.tzinfo is None
            or self.evaluated_at.utcoffset() != timezone.utc.utcoffset(None)
        ):
            raise InvalidPolicy("receipt evaluated_at must be an aware UTC datetime")
        _require_text(self.evaluator_version, label="receipt evaluator version")
        _require_sha256(self.semantic_hash, label="receipt semantic hash")
        if not isinstance(self.refs, tuple) or not all(
            isinstance(ref, (PolicyRef, OpaquePolicyAttestation)) for ref in self.refs
        ):
            raise InvalidPolicy("receipt refs must be a tuple of policy receipt refs")
        if self.refs != _canonical_refs(self.refs):
            raise InvalidPolicy("receipt refs must be deterministically sorted and deduplicated")
        if not isinstance(self.violations, tuple) or not all(
            isinstance(violation, PolicyViolation) for violation in self.violations
        ):
            raise InvalidPolicy("receipt violations must be a tuple of PolicyViolation")
        if not isinstance(self.approved_rule_ids, tuple):
            raise InvalidPolicy("receipt approved rule ids must be a tuple")
        if self.approved_rule_ids != tuple(sorted(set(self.approved_rule_ids))):
            raise InvalidPolicy(
                "receipt approved rule ids must be deterministically sorted and deduplicated"
            )
        for rule_id in self.approved_rule_ids:
            _require_approved_rule_id(rule_id)
        expected_allowed = self.requested <= self.effective_ceiling
        if self.allowed is not expected_allowed:
            raise InvalidPolicy("receipt allowed must match the requested portability ceiling")
        if self.allowed and self.violations:
            raise InvalidPolicy("an allowed receipt cannot contain policy violations")
        if not self.allowed and not self.violations:
            raise InvalidPolicy("a denied receipt requires a structured policy violation")


def _receipt_ref_key(ref: PolicyReceiptRef) -> tuple[str, str, str, str]:
    if isinstance(ref, PolicyRef):
        return ("policy", ref.policy_id, ref.revision, ref.digest)
    return ("opaque", ref.attestation_id, "", ref.digest)


def _canonical_refs(refs: tuple[PolicyReceiptRef, ...]) -> tuple[PolicyReceiptRef, ...]:
    """Sort receipt-safe provenance and remove exact duplicate inputs."""
    return tuple(dict.fromkeys(sorted(refs, key=_receipt_ref_key)))


def _validate_inputs(inputs: object) -> tuple[PolicyRule, ...]:
    if not isinstance(inputs, tuple):
        raise InvalidPolicy("policy inputs must be a tuple of PolicyRule values")
    if not all(isinstance(rule, PolicyRule) for rule in inputs):
        raise InvalidPolicy("policy inputs must contain only PolicyRule values")
    rule_by_identity: dict[tuple[str, str], PolicyRule] = {}
    opaque_provenance: dict[OpaquePolicyAttestation, tuple[str, str, str, Portability, str | None]] = {}
    for rule in inputs:
        key = (rule.policy_id, rule.revision)
        previous = rule_by_identity.setdefault(key, rule)
        if previous != rule:
            if (previous.opaque_attestation is None) != (
                rule.opaque_attestation is None
            ):
                raise InvalidPolicy(
                    "the same policy identity cannot be supplied as opaque and raw provenance"
                )
            raise InvalidPolicy("the same policy identity has conflicting provenance")
        if rule.opaque_attestation is not None:
            provenance = (
                rule.policy_id,
                rule.revision,
                rule.digest,
                rule.ceiling,
                rule.approved_rule_id,
            )
            previous_provenance = opaque_provenance.setdefault(
                rule.opaque_attestation, provenance
            )
            if previous_provenance != provenance:
                raise InvalidPolicy(
                    "one opaque attestation cannot represent conflicting policy provenance"
                )
    return inputs


def evaluate_portability(
    requested: Portability,
    inputs: tuple[PolicyRule, ...],
    semantic_hash: str,
) -> PolicyEvaluation:
    """Evaluate one requested storage class against every inherited policy rule.

    An empty input set is unknown policy for portable writes and therefore raises a
    structured ``UnknownPolicy`` failure.  Local-only requests remain permitted:
    they create no portable artifact and cannot raise a policy ceiling.
    """
    if not isinstance(requested, Portability):
        raise InvalidPolicy("requested portability must be a Portability")
    rules = _validate_inputs(inputs)
    _require_sha256(semantic_hash, label="semantic hash")
    if not rules and requested is not Portability.LOCAL_ONLY:
        violation = PolicyViolation(
            "unknown-policy",
            "portable writes require at least one available applicable policy rule",
        )
        raise UnknownPolicy("portable write policy is unknown", violations=(violation,))

    effective_ceiling = (
        min((rule.ceiling for rule in rules), default=Portability.LOCAL_ONLY)
    )
    refs = _canonical_refs(tuple(rule.receipt_ref for rule in rules))
    approved_rule_ids = tuple(
        sorted({rule.approved_rule_id for rule in rules if rule.approved_rule_id})
    )
    allowed = requested <= effective_ceiling
    violations: tuple[PolicyViolation, ...] = ()
    if not allowed:
        violations = (
            PolicyViolation(
                "requested-exceeds-effective-ceiling",
                "requested portability exceeds the inherited effective ceiling",
            ),
        )
    return PolicyEvaluation(
        requested=requested,
        effective_ceiling=effective_ceiling,
        allowed=allowed,
        evaluated_at=datetime.now(timezone.utc),
        evaluator_version=POLICY_EVALUATOR_VERSION,
        refs=refs,
        semantic_hash=semantic_hash,
        violations=violations,
        approved_rule_ids=approved_rule_ids,
    )


def reevaluate_portability(
    receipt: PolicyEvaluation,
    inputs: tuple[PolicyRule, ...],
) -> PolicyEvaluation:
    """Evaluate current policy without changing the historical admission receipt."""
    if not isinstance(receipt, PolicyEvaluation):
        raise InvalidPolicy("reevaluation requires a PolicyEvaluation receipt")
    return evaluate_portability(receipt.requested, inputs, receipt.semantic_hash)
