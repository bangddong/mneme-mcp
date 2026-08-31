"""Pure privacy-policy evaluation and portable-safe admission receipts.

This module deliberately has no storage dependencies.  Policy revisions and active
pointers are registry concerns; this boundary evaluates already-loaded immutable
rules and returns a new immutable receipt for every decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import math
import re
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, TypeAlias

from mneme.core.artifacts import (
    ArtifactDocument,
    ArtifactFamily,
    ArtifactReference,
    ReferenceKind,
    ReferenceManifest,
    StorageClass,
)
from mneme.core.errors import ArtifactExists, InvalidArtifact, MadiError

if TYPE_CHECKING:
    from mneme.core.vault import Vault


POLICY_EVALUATOR_VERSION: Final = "madi.policy.v1"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_ATTESTATION_ID = re.compile(
    r"^attest-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_APPROVED_RULE_ID = re.compile(
    r"^rule-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_POLICY_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_POLICY_REVISION_SCHEMA: Final = "madi.policy-revision.v1"
_POLICY_INDEX_SCHEMA: Final = "madi.policy-index.v1"
_DEFAULT_POLICY_ID: Final = "vault-default"
_DEFAULT_POLICY_REVISION: Final = "1"
_DEFAULT_POLICY_RULE: Final = {"ceiling": "personal-vault"}


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


def _require_policy_index_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidArtifact("policy index generation must be a non-negative integer")
    return value


def _require_storage_class(value: object) -> StorageClass:
    if not isinstance(value, StorageClass):
        raise InvalidArtifact("policy lifecycle requires an explicit StorageClass")
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
class PolicyIndex:
    """The CAS-protected active policy pointers for one storage class."""

    generation: int
    policies: Mapping[str, PolicyRef]
    storage_class: StorageClass = StorageClass.PORTABLE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise InvalidPolicy("policy index generation must be a non-negative integer")
        if not isinstance(self.policies, Mapping):
            raise InvalidPolicy("policy index policies must be a mapping")
        _require_storage_class(self.storage_class)
        copied: dict[str, PolicyRef] = {}
        for policy_id, ref in self.policies.items():
            _require_policy_token(policy_id, label="policy index id")
            if not isinstance(ref, PolicyRef) or ref.policy_id != policy_id:
                raise InvalidPolicy("policy index entries must match their PolicyRef ids")
            copied[policy_id] = PolicyRef(ref.policy_id, ref.revision, ref.digest)
        object.__setattr__(self, "policies", MappingProxyType(copied))


def parse_policy_index(
    metadata: object, storage_class: StorageClass
) -> PolicyIndex:
    """Parse the one authoritative policy-index schema for a routed root."""
    _require_storage_class(storage_class)
    if not isinstance(metadata, dict):
        raise InvalidArtifact("policy index must be a mapping")
    if set(metadata) == {"generation", "policies"}:
        if metadata != {"generation": 0, "policies": {}}:
            raise InvalidArtifact("policy index has an invalid bootstrap schema")
        return PolicyIndex(0, {}, storage_class)
    if set(metadata) != {"schema", "generation", "policies"}:
        raise InvalidArtifact("policy index has an invalid schema")
    if metadata.get("schema") != _POLICY_INDEX_SCHEMA:
        raise InvalidArtifact("policy index has an unsupported schema")
    generation = metadata.get("generation")
    _require_policy_index_generation(generation)
    policies = metadata.get("policies")
    if not isinstance(policies, dict):
        raise InvalidArtifact("policy index policies must be a mapping")
    parsed: dict[str, PolicyRef] = {}
    for policy_id, value in policies.items():
        _require_policy_token(policy_id, label="policy index id")
        if not isinstance(value, dict) or set(value) != {"revision", "digest"}:
            raise InvalidArtifact("policy index reference has an invalid schema")
        parsed[policy_id] = PolicyRef(policy_id, value["revision"], value["digest"])
    return PolicyIndex(generation, parsed, storage_class)


class PolicyStore:
    """Official immutable policy-revision and active-pointer lifecycle API."""

    def __init__(self, vault: Vault):
        self._vault = vault

    def create_revision(
        self,
        policy_id: str,
        revision: str,
        rule: Mapping[str, Any],
        storage_class: StorageClass,
    ) -> PolicyRef:
        """Create one immutable policy revision through the canonical store."""
        self._validate_storage_class(storage_class)
        _require_policy_token(policy_id, label="policy id")
        _require_policy_token(revision, label="policy revision")
        document = self._revision_document(policy_id, revision, rule)
        encoded = self._vault.router.codec(
            ArtifactFamily.REGISTRY, storage_class
        ).encode(document)
        self._vault.artifacts.write_new(
            ArtifactFamily.REGISTRY,
            storage_class=storage_class,
            relative_path=self._revision_path(policy_id, revision),
            document=document,
        )
        return PolicyRef(policy_id, revision, sha256(encoded.encode("utf-8")).hexdigest())

    def activate(self, policy_ref: PolicyRef, expected_generation: int) -> PolicyIndex:
        """CAS-advance the index that owns an immutable policy revision."""
        if not isinstance(policy_ref, PolicyRef):
            raise InvalidPolicy("policy activation requires a PolicyRef")
        self._validate_generation(expected_generation)
        storage_class = self._storage_class_for_ref(policy_ref)
        self._ensure_index(storage_class)
        current = self._read_index(storage_class)
        policies = dict(current.policies)
        policies[policy_ref.policy_id] = policy_ref
        document = self._index_document(expected_generation + 1, policies, storage_class)
        self._vault.artifacts.write_cas(
            ArtifactFamily.REGISTRY,
            storage_class=storage_class,
            relative_path=".madi/policy-index.yaml",
            document=document,
            expected_generation=expected_generation,
        )
        return PolicyIndex(expected_generation + 1, policies, storage_class)

    def load_active(
        self, policy_id: str, storage_class: StorageClass
    ) -> PolicyRef:
        """Load and verify the active immutable revision from one routed index."""
        _require_policy_token(policy_id, label="policy id")
        self._validate_storage_class(storage_class)
        index = self._read_index(storage_class)
        try:
            ref = index.policies[policy_id]
        except KeyError as exc:
            raise InvalidArtifact(f"policy has no active revision: {policy_id}") from exc
        actual = self._load_revision(ref.policy_id, ref.revision, storage_class)
        if actual != ref:
            raise InvalidArtifact("active policy index digest does not match its revision")
        return actual

    def load_rule(self, policy_ref: PolicyRef) -> PolicyRule:
        """Load one exact immutable rule without consulting an active pointer."""
        if not isinstance(policy_ref, PolicyRef):
            raise InvalidPolicy("policy rule load requires a PolicyRef")
        storage_class = self._storage_class_for_ref(policy_ref)
        actual = self._load_revision(
            policy_ref.policy_id, policy_ref.revision, storage_class
        )
        if actual != policy_ref:
            raise InvalidArtifact("policy revision digest does not match its reference")
        document = self._vault.reader.read(
            ArtifactFamily.REGISTRY,
            storage_class=storage_class,
            relative_path=self._revision_path(policy_ref.policy_id, policy_ref.revision),
        )
        rule = document.metadata.get("rule")
        if not isinstance(rule, Mapping):
            raise InvalidArtifact("policy revision rule must be a mapping")
        try:
            ceiling = Portability(rule.get("ceiling"))
        except (TypeError, ValueError) as exc:
            raise InvalidArtifact("policy revision must declare a valid ceiling") from exc
        approved_rule_id = rule.get("approved_rule_id")
        return PolicyRule(
            actual.policy_id,
            actual.revision,
            ceiling,
            actual.digest,
            approved_rule_id=approved_rule_id,
        )

    def bootstrap_default(self) -> PolicyRef:
        """Ensure the exact default policy exists and is active via public lifecycle APIs."""
        document = self._revision_document(
            _DEFAULT_POLICY_ID, _DEFAULT_POLICY_REVISION, _DEFAULT_POLICY_RULE
        )
        expected = PolicyRef(
            _DEFAULT_POLICY_ID,
            _DEFAULT_POLICY_REVISION,
            sha256(
                self._vault.router.codec(
                    ArtifactFamily.REGISTRY, StorageClass.PORTABLE
                )
                .encode(document)
                .encode("utf-8")
            ).hexdigest(),
        )
        try:
            created = self.create_revision(
                _DEFAULT_POLICY_ID,
                _DEFAULT_POLICY_REVISION,
                _DEFAULT_POLICY_RULE,
                StorageClass.PORTABLE,
            )
        except ArtifactExists:
            created = self._load_revision(
                _DEFAULT_POLICY_ID, _DEFAULT_POLICY_REVISION, StorageClass.PORTABLE
            )
            if created != expected:
                raise InvalidArtifact("default policy revision does not match bootstrap content")
        if created != expected:
            raise InvalidArtifact("default policy revision digest is not deterministic")

        index = self._read_index(StorageClass.PORTABLE)
        current = index.policies.get(_DEFAULT_POLICY_ID)
        if current == created:
            return created
        if current is not None:
            raise InvalidArtifact("default policy pointer does not match bootstrap revision")
        self.activate(created, index.generation)
        return created

    def _storage_class_for_ref(self, policy_ref: PolicyRef) -> StorageClass:
        matches: list[StorageClass] = []
        for storage_class in StorageClass:
            location = self._vault.router.location(
                ArtifactFamily.REGISTRY,
                storage_class,
                self._revision_path(policy_ref.policy_id, policy_ref.revision),
            )
            if not location.path.exists():
                continue
            actual = self._load_revision(
                policy_ref.policy_id, policy_ref.revision, storage_class
            )
            if actual != policy_ref:
                raise InvalidArtifact("policy revision digest does not match stored content")
            matches.append(storage_class)
        if not matches:
            raise InvalidArtifact("policy revision does not exist in a routed storage class")
        if len(matches) != 1:
            raise InvalidArtifact("policy revision is ambiguous across storage classes")
        return matches[0]

    def _load_revision(
        self, policy_id: str, revision: str, storage_class: StorageClass
    ) -> PolicyRef:
        location = self._vault.router.location(
            ArtifactFamily.REGISTRY,
            storage_class,
            self._revision_path(policy_id, revision),
        )
        document = self._vault.reader.read(ArtifactFamily.REGISTRY, location=location)
        metadata = document.metadata
        expected_keys = {"schema", "policy_id", "revision", "rule"}
        if set(metadata) != expected_keys:
            raise InvalidArtifact("policy revision has an invalid schema")
        if (
            metadata.get("schema") != _POLICY_REVISION_SCHEMA
            or metadata.get("policy_id") != policy_id
            or metadata.get("revision") != revision
        ):
            raise InvalidArtifact("policy revision identity does not match its path")
        canonical = self._revision_document(policy_id, revision, metadata.get("rule"))
        encoded = self._vault.router.codec(
            ArtifactFamily.REGISTRY, storage_class
        ).encode(canonical)
        try:
            raw = location.path.read_bytes()
        except OSError as exc:
            raise InvalidArtifact(f"cannot read policy revision: {location.path}") from exc
        if raw != encoded.encode("utf-8"):
            raise InvalidArtifact("policy revision is not canonically encoded")
        return PolicyRef(policy_id, revision, sha256(raw).hexdigest())

    def _read_index(self, storage_class: StorageClass) -> PolicyIndex:
        location = self._vault.router.location(
            ArtifactFamily.REGISTRY, storage_class, ".madi/policy-index.yaml"
        )
        document = self._vault.reader.read(ArtifactFamily.REGISTRY, location=location)
        return parse_policy_index(document.metadata, storage_class)

    def _ensure_index(self, storage_class: StorageClass) -> None:
        location = self._vault.router.location(
            ArtifactFamily.REGISTRY, storage_class, ".madi/policy-index.yaml"
        )
        if location.path.exists():
            return
        self._vault.artifacts.write_new(
            ArtifactFamily.REGISTRY,
            storage_class=storage_class,
            relative_path=".madi/policy-index.yaml",
            document=self._index_document(0, {}, storage_class),
        )

    @staticmethod
    def _revision_path(policy_id: str, revision: str) -> str:
        _require_policy_token(policy_id, label="policy id")
        _require_policy_token(revision, label="policy revision")
        return f".madi/policies/{policy_id}/{revision}.yaml"

    @staticmethod
    def _validate_generation(generation: object) -> None:
        _require_policy_index_generation(generation)

    @staticmethod
    def _validate_storage_class(storage_class: object) -> None:
        _require_storage_class(storage_class)

    @staticmethod
    def _canonical_rule(rule: object) -> dict[str, Any]:
        if not isinstance(rule, Mapping):
            raise InvalidArtifact("policy rule must be a mapping")
        return {
            key: PolicyStore._canonical_rule_value(value)
            for key, value in rule.items()
            if PolicyStore._validate_rule_key(key)
        }

    @staticmethod
    def _validate_rule_key(key: object) -> bool:
        if not isinstance(key, str) or not key:
            raise InvalidArtifact("policy rule mapping keys must be non-empty text")
        return True

    @staticmethod
    def _canonical_rule_value(value: object) -> Any:
        if value is None or isinstance(value, (str, bool)):
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise InvalidArtifact("policy rule numbers must be finite")
            return value
        if isinstance(value, list):
            return [PolicyStore._canonical_rule_value(item) for item in value]
        if isinstance(value, Mapping):
            return {
                key: PolicyStore._canonical_rule_value(item)
                for key, item in value.items()
                if PolicyStore._validate_rule_key(key)
            }
        raise InvalidArtifact("policy rule must contain canonical YAML data")

    def _revision_document(
        self, policy_id: str, revision: str, rule: object
    ) -> ArtifactDocument:
        return ArtifactDocument(
            metadata={
                "schema": _POLICY_REVISION_SCHEMA,
                "policy_id": policy_id,
                "revision": revision,
                "rule": self._canonical_rule(rule),
            },
            references=ReferenceManifest.complete(),
        )

    @staticmethod
    def _index_document(
        generation: int,
        policies: Mapping[str, PolicyRef],
        storage_class: StorageClass,
    ) -> ArtifactDocument:
        index = PolicyIndex(generation, policies, storage_class)
        return ArtifactDocument(
            metadata={
                "schema": _POLICY_INDEX_SCHEMA,
                "generation": index.generation,
                "policies": {
                    policy_id: {"revision": ref.revision, "digest": ref.digest}
                    for policy_id, ref in sorted(index.policies.items())
                },
            },
            references=ReferenceManifest.complete(
                metadata=tuple(
                    ArtifactReference(ReferenceKind.ID, storage_class, policy_id)
                    for policy_id in sorted(index.policies)
                )
            ),
        )


@dataclass(frozen=True, slots=True, init=False)
class OpaquePolicyAttestation:
    """Portable-safe proof for a policy whose source identity is confidential.

    The attestation deliberately contains no source, project, or policy identifier.
    Its stable opaque identifier and verification digest retain auditability without
    putting the confidential mapping in the portable receipt.
    """

    attestation_id: str
    revision: str
    ceiling: Portability
    digest: str

    @classmethod
    def create(
        cls,
        attestation_id: str,
        revision: str,
        ceiling: Portability,
    ) -> OpaquePolicyAttestation:
        """Create a portable attestation from safe public fields only."""
        cls._validate_public_fields(attestation_id, revision, ceiling)
        instance = object.__new__(cls)
        object.__setattr__(instance, "attestation_id", attestation_id)
        object.__setattr__(instance, "revision", revision)
        object.__setattr__(instance, "ceiling", ceiling)
        object.__setattr__(
            instance,
            "digest",
            _portable_attestation_digest(attestation_id, revision, ceiling),
        )
        return instance

    @classmethod
    def from_receipt(
        cls,
        attestation_id: str,
        revision: str,
        ceiling: Portability,
        digest: str,
    ) -> OpaquePolicyAttestation:
        """Validate a serialized receipt attestation without trusting its digest."""
        instance = cls.create(attestation_id, revision, ceiling)
        if digest != instance.digest:
            raise InvalidPolicy("opaque attestation digest does not match safe fields")
        return instance

    @staticmethod
    def _validate_public_fields(
        attestation_id: object, revision: object, ceiling: object
    ) -> None:
        if not isinstance(attestation_id, str) or not _OPAQUE_ATTESTATION_ID.fullmatch(
            attestation_id
        ):
            raise InvalidPolicy(
                "opaque attestation id must be a generated attest-UUID identifier"
            )
        _require_policy_token(revision, label="opaque attestation revision")
        if not isinstance(ceiling, Portability):
            raise InvalidPolicy("opaque attestation ceiling must be a Portability")


def _portable_attestation_digest(
    attestation_id: str, revision: str, ceiling: Portability
) -> str:
    payload = "\0".join(
        ("madi.policy.opaque-attestation.v1", attestation_id, revision, ceiling.value)
    )
    return sha256(payload.encode("utf-8")).hexdigest()


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
        if self.opaque_attestation is not None:
            attestation = OpaquePolicyAttestation.from_receipt(
                self.opaque_attestation.attestation_id,
                self.opaque_attestation.revision,
                self.opaque_attestation.ceiling,
                self.opaque_attestation.digest,
            )
            if (
                attestation.revision != self.revision
                or attestation.ceiling is not self.ceiling
            ):
                raise InvalidPolicy(
                    "opaque attestation must bind the containing policy revision and ceiling"
                )
            object.__setattr__(self, "opaque_attestation", attestation)
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
class ApprovedRuleProvenance:
    """An approved derivative/declassification rule bound to its authority."""

    rule_id: str
    authorizer: PolicyReceiptRef

    def __post_init__(self) -> None:
        _require_approved_rule_id(self.rule_id)
        object.__setattr__(self, "authorizer", _copy_receipt_ref(self.authorizer))


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
    approved_rules: tuple[ApprovedRuleProvenance, ...] = ()

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
        if not isinstance(self.refs, tuple):
            raise InvalidPolicy("receipt refs must be a tuple of policy receipt refs")
        copied_refs = tuple(_copy_receipt_ref(ref) for ref in self.refs)
        if copied_refs != _canonical_refs(copied_refs):
            raise InvalidPolicy("receipt refs must be deterministically sorted and deduplicated")
        object.__setattr__(self, "refs", copied_refs)
        if not isinstance(self.violations, tuple) or not all(
            isinstance(violation, PolicyViolation) for violation in self.violations
        ):
            raise InvalidPolicy("receipt violations must be a tuple of PolicyViolation")
        if not isinstance(self.approved_rules, tuple) or not all(
            isinstance(provenance, ApprovedRuleProvenance)
            for provenance in self.approved_rules
        ):
            raise InvalidPolicy("receipt approved rules must be bound ApprovedRuleProvenance values")
        copied_approved_rules = tuple(
            ApprovedRuleProvenance(provenance.rule_id, provenance.authorizer)
            for provenance in self.approved_rules
        )
        if copied_approved_rules != _canonical_approved_rules(copied_approved_rules):
            raise InvalidPolicy(
                "receipt approved rules must be deterministically sorted and deduplicated"
            )
        if any(
            provenance.authorizer not in copied_refs
            for provenance in copied_approved_rules
        ):
            raise InvalidPolicy(
                "every approved rule authorizer must match a canonical receipt refs entry"
            )
        object.__setattr__(self, "approved_rules", copied_approved_rules)
        expected_allowed = self.requested <= self.effective_ceiling
        if self.allowed is not expected_allowed:
            raise InvalidPolicy("receipt allowed must match the requested portability ceiling")
        if self.allowed and self.violations:
            raise InvalidPolicy("an allowed receipt cannot contain policy violations")
        if not self.allowed and not self.violations:
            raise InvalidPolicy("a denied receipt requires a structured policy violation")

    @property
    def approved_rule_ids(self) -> tuple[str, ...]:
        """Compatibility projection of receipt-bound approved-rule provenance."""
        return tuple(provenance.rule_id for provenance in self.approved_rules)


def _receipt_ref_key(ref: PolicyReceiptRef) -> tuple[str, str, str, str]:
    if isinstance(ref, PolicyRef):
        return ("policy", ref.policy_id, ref.revision, ref.digest)
    return ("opaque", ref.attestation_id, "", ref.digest)


def _copy_receipt_ref(ref: object) -> PolicyReceiptRef:
    if isinstance(ref, PolicyRef):
        return PolicyRef(ref.policy_id, ref.revision, ref.digest)
    if isinstance(ref, OpaquePolicyAttestation):
        return OpaquePolicyAttestation.from_receipt(
            ref.attestation_id, ref.revision, ref.ceiling, ref.digest
        )
    raise InvalidPolicy("receipt refs must contain only policy refs or opaque attestations")


def _canonical_refs(refs: tuple[PolicyReceiptRef, ...]) -> tuple[PolicyReceiptRef, ...]:
    """Sort receipt-safe provenance and remove exact duplicate inputs."""
    return tuple(dict.fromkeys(sorted(refs, key=_receipt_ref_key)))


def _approved_rule_key(
    provenance: ApprovedRuleProvenance,
) -> tuple[str, tuple[str, str, str, str]]:
    return (provenance.rule_id, _receipt_ref_key(provenance.authorizer))


def _canonical_approved_rules(
    rules: tuple[ApprovedRuleProvenance, ...],
) -> tuple[ApprovedRuleProvenance, ...]:
    by_rule_id: dict[str, ApprovedRuleProvenance] = {}
    for provenance in rules:
        previous = by_rule_id.setdefault(provenance.rule_id, provenance)
        if previous != provenance:
            raise InvalidPolicy("the same approved rule has conflicting authorizers")
    return tuple(dict.fromkeys(sorted(rules, key=_approved_rule_key)))


def _validate_inputs(inputs: object) -> tuple[PolicyRule, ...]:
    if not isinstance(inputs, tuple):
        raise InvalidPolicy("policy inputs must be a tuple of PolicyRule values")
    if not all(isinstance(rule, PolicyRule) for rule in inputs):
        raise InvalidPolicy("policy inputs must contain only PolicyRule values")
    rules = tuple(
        PolicyRule(
            rule.policy_id,
            rule.revision,
            rule.ceiling,
            rule.digest,
            rule.opaque_attestation,
            rule.approved_rule_id,
        )
        for rule in inputs
    )
    rule_by_identity: dict[tuple[str, str], PolicyRule] = {}
    opaque_provenance: dict[OpaquePolicyAttestation, tuple[str, str, str, Portability, str | None]] = {}
    for rule in rules:
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
    return rules


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
    refs = _canonical_refs(tuple(_copy_receipt_ref(rule.receipt_ref) for rule in rules))
    approved_rules = _canonical_approved_rules(
        tuple(
            ApprovedRuleProvenance(rule.approved_rule_id, rule.receipt_ref)
            for rule in rules
            if rule.approved_rule_id is not None
        )
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
        approved_rules=approved_rules,
    )


def reevaluate_portability(
    receipt: PolicyEvaluation,
    inputs: tuple[PolicyRule, ...],
) -> PolicyEvaluation:
    """Evaluate current policy without changing the historical admission receipt."""
    if not isinstance(receipt, PolicyEvaluation):
        raise InvalidPolicy("reevaluation requires a PolicyEvaluation receipt")
    return evaluate_portability(receipt.requested, inputs, receipt.semantic_hash)
