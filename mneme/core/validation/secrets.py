"""Deterministic credential guards shared by portable artifacts and Git audits."""

from collections.abc import Mapping
import re

from mneme.core.errors import InvalidArtifact


_LABEL = r"(?:api[\s_-]?key|access[\s_-]?(?:token|key)|auth[\s_-]?token|client[\s_-]?secret|private[\s_-]?key|secret|password|passwd|token)"
_ASSIGNMENT = re.compile(r"\b" + _LABEL + r"\b\s*(?:=|:)\s*", re.I)
_KEY = re.compile(_LABEL, re.I)
_MATERIAL = re.compile(
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|-----BEGIN(?:\s+[A-Z0-9]+)?\s+PRIVATE\s+KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}|\bAKIA[A-Z0-9]{16}\b", re.I)
_MAX_ASSIGNMENT = 256 * 1024


def contains_detectable_secret(value: str) -> bool:
    for match in _ASSIGNMENT.finditer(value):
        if "\r" in match[0] or "\n" in match[0]:
            return True
        if assignment_contains_secret(value[match.end():]):
            return True
    return _MATERIAL.search(value) is not None


def assignment_contains_secret(remainder: str) -> bool:
    if len(remainder) > _MAX_ASSIGNMENT:
        return True
    if "\r" in remainder or "\n" in remainder:
        return bool(remainder.strip())
    assigned = remainder.strip()
    if not assigned:
        return False
    if assigned[0] in {'"', "'"}:
        quote = assigned[0]
        other = "'" if quote == '"' else '"'
        if len(assigned) < 2 or assigned[-1] != quote or assigned.count(quote) != 2 or other in assigned[1:-1]:
            return True
        assigned = assigned[1:-1].strip()
    elif '"' in assigned or "'" in assigned:
        return True
    return len(assigned) >= 8 and assigned.casefold() not in {"redacted", "placeholder"}


def _credential_value_contains_secret(value: object) -> bool:
    if isinstance(value, (str, int, float)):
        return assignment_contains_secret(str(value))
    if isinstance(value, Mapping):
        return any(_credential_value_contains_secret(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_credential_value_contains_secret(item) for item in value)
    return False


def reject_detectable_secrets(value: object) -> None:
    """Inspect semantic scalars and mapping keys without echoing sensitive values."""
    if isinstance(value, str):
        if contains_detectable_secret(value):
            raise InvalidArtifact("portable artifact contains a detectable credential")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            reject_detectable_secrets(key)
            if (isinstance(key, str) and _KEY.fullmatch(key)
                    and _credential_value_contains_secret(item)):
                raise InvalidArtifact("portable artifact contains a detectable credential")
            reject_detectable_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_detectable_secrets(item)


def validate_portable_content(storage_class, document, relative_path) -> None:
    from mneme.core.artifacts import StorageClass

    if storage_class is not StorageClass.PORTABLE:
        return
    reject_detectable_secrets(str(relative_path))
    reject_detectable_secrets(document.metadata)
    if document.body:
        # Token material can span lines even when assignments are checked one
        # line at a time to keep placeholder handling precise.
        if _MATERIAL.search(document.body):
            raise InvalidArtifact("portable artifact contains a detectable credential")
        lines = document.body.splitlines()
        for index, line in enumerate(lines):
            reject_detectable_secrets(line)
            for match in _ASSIGNMENT.finditer(line):
                if line[match.end():].strip():
                    continue
                following = next(
                    (candidate for candidate in lines[index + 1:] if candidate.strip()),
                    "",
                )
                if following and assignment_contains_secret(following):
                    raise InvalidArtifact(
                        "portable artifact contains a detectable credential"
                    )
