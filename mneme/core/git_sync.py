"""Explicit, non-merging Git transport for validated portable Vault artifacts.

Checkpoint persistence never enters this module.  Git is an optional transport
layer over the current portable tree, and every mutating operation either uses
an explicit canonical path list or a verified fast-forward relationship.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from mneme.core.artifacts import ArtifactFamily
from mneme.core.doctor import Doctor, DoctorReport
from mneme.core.errors import InvalidArtifact, MadiError
from mneme.core.fs import is_symlink_or_reparse, validate_path_chain
from mneme.core.validation.vault import (
    validate_contained_path,
    validate_relative_path,
)
from mneme.core.vault import Vault


_SAFE_REF_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_ISSUE_CODE = re.compile(r"^[a-z][a-z0-9-]*$")


class GitSyncError(MadiError):
    """A Git transport operation failed without exposing subprocess details."""


@dataclass(frozen=True, slots=True)
class SyncInspection:
    """Portable-safe relationship between local HEAD and its configured upstream."""

    status: str
    repository: bool
    clean: bool
    has_upstream: bool
    ahead: int = 0
    behind: int = 0
    changed: bool = False


@dataclass(frozen=True, slots=True)
class LivePolicyDecision:
    """Task 19 extension seam for live, current-pointer authorization."""

    allowed: bool
    issue_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool) or not isinstance(self.issue_codes, tuple):
            raise TypeError("live policy decision has an invalid shape")
        if not all(
            isinstance(code, str) and _SAFE_ISSUE_CODE.fullmatch(code)
            for code in self.issue_codes
        ):
            raise ValueError("live policy decision contains an unsafe issue code")


LiveSyncAuthorizer = Callable[
    [Vault, tuple[PurePosixPath, ...]], LivePolicyDecision
]


@dataclass(frozen=True, slots=True)
class SyncPreflight:
    """Doctor and current-policy verdict required before outbound Git mutation."""

    status: str
    allowed: bool
    doctor_status: str
    blocker_codes: tuple[str, ...]
    path_count: int


@dataclass(frozen=True, slots=True)
class GitSyncResult:
    """A mutation result that carries no paths, remotes, URLs, or Git output."""

    status: str
    changed: bool


def inspect_sync(vault: Vault) -> SyncInspection:
    """Inspect local/upstream Git state without mutating the repository."""
    root = _validated_root(vault)
    if not _is_repository(root):
        return SyncInspection("unavailable", False, True, False)
    clean = _is_clean(root)
    head = _try_commit(root, "HEAD")
    if head is None:
        return SyncInspection("unborn", True, clean, False)
    upstream = _try_commit(root, "@{upstream}")
    if upstream is None:
        return SyncInspection("untracked", True, clean, False)
    counts = _run_git(
        root,
        ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"),
    ).stdout.split()
    if len(counts) != 2 or not all(item.isdigit() for item in counts):
        raise GitSyncError("Git sync inspection failed safely")
    ahead, behind = (int(item) for item in counts)
    if ahead and behind:
        status = "divergent"
    elif ahead:
        status = "ahead"
    elif behind:
        status = "behind"
    elif clean:
        status = "up-to-date"
    else:
        status = "dirty"
    return SyncInspection(status, True, clean, True, ahead, behind)


def sync_preflight(
    vault: Vault,
    paths: Iterable[str | Path] = (),
    *,
    live_authorizer: LiveSyncAuthorizer | None = None,
) -> SyncPreflight:
    """Compose Doctor with a fail-closed live-policy authorization seam.

    Generated index findings are deliberately non-authoritative here.  Doctor's
    canonical/current-policy findings remain authoritative; Task 19 can extend
    the final live decision without trusting cached policy fields.
    """
    _validated_root(vault)
    canonical_paths = _canonical_paths(vault, paths)
    report = Doctor(vault).run()
    blockers = {
        issue.code
        for issue in report.issues
        if issue.severity == "invalid"
        and not issue.code.startswith("generated-index-")
    }
    authorize = live_authorizer or _authorize_current_policy
    try:
        decision = authorize(vault, canonical_paths)
    except Exception:
        decision = LivePolicyDecision(False, ("live-policy-authorization-failed",))
    if not isinstance(decision, LivePolicyDecision):
        decision = LivePolicyDecision(False, ("live-policy-authorization-failed",))
    if not decision.allowed:
        blockers.update(decision.issue_codes or ("live-policy-denied",))
    blocker_codes = tuple(sorted(blockers))
    allowed = not blocker_codes
    return SyncPreflight(
        "ready" if allowed else "blocked",
        allowed,
        _doctor_status_without_generated_authority(report),
        blocker_codes,
        len(canonical_paths),
    )


def fetch(vault: Vault, remote: str = "origin") -> GitSyncResult:
    """Fetch one named configured remote without applying it to the Vault tree."""
    root = _repository_root(vault)
    safe_remote = _safe_remote(remote)
    _run_git(root, ("fetch", "--", safe_remote))
    return GitSyncResult("fetched", True)


def commit_paths(
    vault: Vault,
    paths: Iterable[str | Path],
    message: str,
) -> GitSyncResult:
    """Commit only explicitly selected, validated portable canonical paths."""
    canonical_paths = _canonical_paths(vault, paths)
    if not canonical_paths:
        raise InvalidArtifact("commit requires at least one canonical artifact path")
    if (
        not isinstance(message, str)
        or not message.strip()
        or "\x00" in message
    ):
        raise InvalidArtifact("commit message must be non-empty text")
    preflight = sync_preflight(vault, canonical_paths)
    if not preflight.allowed:
        return GitSyncResult("blocked", False)
    root = _repository_root(vault)
    pathspecs = tuple(path.as_posix() for path in canonical_paths)
    _run_git(root, ("add", "--", *pathspecs))
    staged = _run_git(
        root,
        ("diff", "--cached", "--quiet", "--", *pathspecs),
        accepted_returncodes=(0, 1),
    )
    if staged.returncode == 0:
        return GitSyncResult("no-changes", False)
    _run_git(
        root,
        ("commit", "--only", "-m", message, "--", *pathspecs),
    )
    return GitSyncResult("committed", True)


def fast_forward(
    vault: Vault,
    remote: str = "origin",
    branch: str | None = None,
) -> GitSyncResult:
    """Fetch, validate, and apply only a proven clean-tree fast-forward."""
    safe_remote = _safe_remote(remote)
    fetch(vault, safe_remote)
    root = _repository_root(vault)
    preflight = sync_preflight(vault)
    if not preflight.allowed:
        return GitSyncResult("blocked", False)
    if not _is_clean(root):
        return GitSyncResult("dirty", False)
    branch_ref, branch_name = _current_branch(root)
    if branch is not None:
        branch_name = _safe_branch(branch)
    target_ref = f"refs/remotes/{safe_remote}/{branch_name}"
    current = _commit(root, "HEAD")
    target = _try_commit(root, target_ref)
    if target is None:
        return GitSyncResult("no-upstream", False)
    if current == target:
        return GitSyncResult("up-to-date", False)
    if not _is_ancestor(root, current, target):
        status = "ahead" if _is_ancestor(root, target, current) else "divergent"
        return GitSyncResult(status, False)

    # The CAS update and restore are plumbing equivalents of an ff-only move.
    # No semantic merge, checkout, reset, rebase, or forced update is available.
    _run_git(root, ("update-ref", branch_ref, target, current))
    _run_git(
        root,
        ("restore", "--source=HEAD", "--staged", "--worktree", "--", "."),
    )
    return GitSyncResult("fast-forwarded", True)


def push(
    vault: Vault,
    remote: str = "origin",
    branch: str | None = None,
) -> GitSyncResult:
    """Push HEAD to one explicit branch; no force mode exists."""
    safe_remote = _safe_remote(remote)
    preflight = sync_preflight(vault)
    if not preflight.allowed:
        return GitSyncResult("blocked", False)
    root = _repository_root(vault)
    if not _is_clean(root):
        return GitSyncResult("dirty", False)
    _branch_ref, current_branch = _current_branch(root)
    target_branch = current_branch if branch is None else _safe_branch(branch)
    _run_git(
        root,
        (
            "push",
            "--porcelain",
            "--",
            safe_remote,
            f"HEAD:refs/heads/{target_branch}",
        ),
    )
    return GitSyncResult("pushed", True)


def _authorize_current_policy(
    _vault: Vault, _paths: tuple[PurePosixPath, ...]
) -> LivePolicyDecision:
    """Task 18 seam: Doctor already performs available current-pointer checks."""
    return LivePolicyDecision(True)


def _doctor_status_without_generated_authority(report: DoctorReport) -> str:
    severities = {
        issue.severity
        for issue in report.issues
        if not issue.code.startswith("generated-index-")
    }
    if "invalid" in severities:
        return "invalid"
    if "degraded" in severities:
        return "degraded"
    return "resolved"


def _validated_root(vault: Vault) -> Path:
    if not isinstance(vault, Vault):
        raise TypeError("Git sync requires a Vault")
    try:
        root = validate_path_chain(vault.root, allow_missing=False).resolve(strict=True)
        if root != vault.root.resolve(strict=True) or not root.is_dir():
            raise InvalidArtifact("Vault root is invalid")
    except (InvalidArtifact, OSError, RuntimeError, ValueError) as exc:
        raise GitSyncError("Git sync Vault validation failed safely") from None
    return root


def _repository_root(vault: Vault) -> Path:
    root = _validated_root(vault)
    if not _is_repository(root):
        raise GitSyncError("Git repository is unavailable")
    return root


def _is_repository(root: Path) -> bool:
    try:
        inside = _run_git(
            root,
            ("rev-parse", "--is-inside-work-tree"),
            accepted_returncodes=(0, 128),
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return False
        top = _run_git(root, ("rev-parse", "--show-toplevel")).stdout.strip()
        return Path(top).resolve(strict=True) == root
    except (GitSyncError, OSError, RuntimeError, ValueError):
        return False


def _is_clean(root: Path) -> bool:
    result = _run_git(
        root,
        ("status", "--porcelain=v1", "--untracked-files=normal"),
    )
    return result.stdout == ""


def _current_branch(root: Path) -> tuple[str, str]:
    result = _run_git(
        root,
        ("symbolic-ref", "-q", "HEAD"),
        accepted_returncodes=(0, 1),
    )
    reference = result.stdout.strip()
    prefix = "refs/heads/"
    if result.returncode != 0 or not reference.startswith(prefix):
        raise GitSyncError("Git HEAD is detached")
    branch = _safe_branch(reference[len(prefix) :])
    return reference, branch


def _commit(root: Path, reference: str) -> str:
    result = _run_git(
        root,
        ("rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"),
    )
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise GitSyncError("Git revision validation failed safely")
    return commit


def _try_commit(root: Path, reference: str) -> str | None:
    result = _run_git(
        root,
        ("rev-parse", "--verify", "--end-of-options", f"{reference}^{{commit}}"),
        accepted_returncodes=(0, 1, 128),
    )
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) else None


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    result = _run_git(
        root,
        ("merge-base", "--is-ancestor", older, newer),
        accepted_returncodes=(0, 1),
    )
    return result.returncode == 0


def _canonical_paths(
    vault: Vault, paths: Iterable[str | Path]
) -> tuple[PurePosixPath, ...]:
    if isinstance(paths, (str, bytes, Path)):
        raise InvalidArtifact("commit paths must be an explicit path collection")
    try:
        values = tuple(paths)
    except TypeError:
        raise InvalidArtifact("commit paths must be an explicit path collection") from None
    canonical = tuple(_canonical_path(vault, value) for value in values)
    if len(set(canonical)) != len(canonical):
        raise InvalidArtifact("commit paths must not contain duplicates")
    return canonical


def _canonical_path(vault: Vault, value: str | Path) -> PurePosixPath:
    root = _validated_root(vault)
    try:
        raw = Path(value)
        if raw.is_absolute():
            resolved = validate_path_chain(raw, allow_missing=True).resolve(strict=False)
            relative = resolved.relative_to(root).as_posix()
        else:
            relative = str(value)
        candidate_path = PurePosixPath(relative)
        family = _artifact_family(candidate_path)
        if candidate_path == PurePosixPath(".madi/schema-version"):
            canonical = candidate_path
        else:
            canonical = validate_relative_path(family, relative)
        candidate = validate_contained_path(root, canonical)
        validate_path_chain(candidate, allow_missing=True)
        if is_symlink_or_reparse(candidate) or not candidate.is_file():
            raise InvalidArtifact("canonical artifact path is unsafe")
        return canonical
    except (InvalidArtifact, OSError, RuntimeError, TypeError, ValueError):
        raise InvalidArtifact(
            "commit path is not a validated portable canonical artifact"
        ) from None


def _artifact_family(path: PurePosixPath) -> ArtifactFamily:
    parts = path.parts
    if path == PurePosixPath(".madi/schema-version"):
        return ArtifactFamily.REGISTRY
    if len(parts) == 2 and parts[0] == "memory":
        return ArtifactFamily.MEMORY
    if len(parts) == 5 and parts[0] == "workstreams" and parts[2] == "sessions":
        return ArtifactFamily.SESSION
    return ArtifactFamily.REGISTRY


def _safe_remote(value: str) -> str:
    if not _safe_ref_name(value) or "/" in value:
        raise GitSyncError("Git remote name is invalid")
    return value


def _safe_branch(value: str) -> str:
    if not _safe_ref_name(value):
        raise GitSyncError("Git branch name is invalid")
    return value


def _safe_ref_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SAFE_REF_COMPONENT.fullmatch(value) is not None
        and not value.startswith("-")
        and not value.endswith((".", "/"))
        and ".." not in value
        and "//" not in value
        and ".lock" not in value.split("/")
    )


def _run_git(
    root: Path,
    arguments: tuple[str, ...],
    *,
    accepted_returncodes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    """Run Git at the validated Vault root and discard unsafe diagnostics."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise GitSyncError("Git operation failed safely") from None
    if result.returncode not in accepted_returncodes:
        raise GitSyncError("Git operation failed safely")
    return result
