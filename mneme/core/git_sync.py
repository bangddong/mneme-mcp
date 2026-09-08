"""Explicit, non-merging Git transport for validated portable Vault artifacts.

Checkpoint persistence never enters this module.  Git is an optional transport
layer over the current portable tree, and every mutating operation either uses
an explicit canonical path list or a verified fast-forward relationship.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from mneme.core.artifacts import ArtifactFamily
from mneme.core.doctor import Doctor, DoctorReport
from mneme.core.errors import InvalidArtifact, MadiError
from mneme.core.fs import is_symlink_or_reparse, read_yaml, validate_path_chain
from mneme.core.validation.vault import (
    validate_contained_path,
    validate_identifier,
    validate_relative_path,
)
from mneme.core.vault import Vault


_SAFE_REF_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SAFE_ISSUE_CODE = re.compile(r"^[a-z][a-z0-9-]*$")
_GIT_OID = re.compile(r"^[0-9a-fA-F]{40,64}$")
_CANONICAL_TREE_ROOTS = frozenset({".madi", "projects", "sources", "workstreams", "memory"})
_PORTABLE_DIRECTORIES = (
    ".madi/policies",
    "projects",
    "sources",
    "workstreams",
    "memory",
)
_LOCAL_DIRECTORIES = (
    "bindings",
    "overlays",
    "evidence",
    "pending",
    "views",
    "index",
    "cache",
    "locks",
    "logs",
)
_GIT_ENVIRONMENT_OVERRIDES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NO_REPLACE_OBJECTS",
    }
)


class GitSyncError(MadiError):
    """A Git transport operation failed without exposing subprocess details."""


@dataclass(frozen=True, slots=True)
class _ConfiguredUpstream:
    branch_ref: str
    remote: str
    branch: str
    reference: str


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
    """Commit an exact validated temporary-index tree, never the caller index."""
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
    try:
        branch_ref, _branch = _current_branch(root)
    except GitSyncError:
        return GitSyncResult("detached-head", False)
    parent = _commit(root, "HEAD")
    pathspecs = tuple(path.as_posix() for path in canonical_paths)
    primary_entries = _index_entries(root, pathspecs)
    with _isolated_index(root, parent) as index_environment:
        _run_git(root, ("add", "--", *pathspecs), environment=index_environment)
        staged_entries = _index_entries(
            root, pathspecs, environment=index_environment
        )
        if not _selected_entries_are_regular(canonical_paths, staged_entries):
            return GitSyncResult("staged-content-invalid", False)
        staged = _run_git(
            root,
            ("diff", "--cached", "--quiet", "--", *pathspecs),
            accepted_returncodes=(0, 1),
            environment=index_environment,
        )
        if staged.returncode == 0:
            return GitSyncResult("no-changes", False)
        tree = _tree_oid(root, environment=index_environment)
        if not _validate_commit_tree(root, tree):
            return GitSyncResult("staged-content-invalid", False)
    if not _same_branch_head(root, branch_ref, parent):
        return GitSyncResult("concurrent-ref-changed", False)
    candidate = _commit_tree(root, tree, parent, message)
    if not _update_ref_cas(root, branch_ref, candidate, parent):
        return GitSyncResult("concurrent-ref-changed", False)
    _synchronize_primary_index(
        root,
        pathspecs,
        primary_entries,
        staged_entries,
    )
    return GitSyncResult("committed", True)


def fast_forward(
    vault: Vault,
    remote: str = "origin",
    branch: str | None = None,
) -> GitSyncResult:
    """Fetch and validate a target; safe application remains an explicit action."""
    safe_remote = _safe_remote(remote)
    root = _repository_root(vault)
    upstream_status = _configured_upstream_request(root, safe_remote, branch)
    if upstream_status != "ready":
        return GitSyncResult(upstream_status, False)
    fetch(vault, safe_remote)
    upstream, upstream_status = _configured_upstream(root, safe_remote, branch)
    if upstream is None:
        return GitSyncResult(upstream_status, False)
    current = _commit(root, "HEAD")
    preflight = sync_preflight(vault)
    if not preflight.allowed:
        return GitSyncResult("blocked", False)
    if not _is_clean(root):
        return GitSyncResult("dirty", False)
    if not _same_branch_head(root, upstream.branch_ref, current):
        return GitSyncResult("concurrent-ref-changed", False)
    target = _try_commit(root, upstream.reference)
    if target is None:
        return GitSyncResult("no-upstream", False)
    if not _validate_commit_tree(root, target):
        return GitSyncResult("target-invalid", False)
    current_upstream, _current_upstream_status = _configured_upstream(
        root, safe_remote, branch
    )
    if current_upstream != upstream or (
        current_upstream is not None
        and _try_commit(root, current_upstream.reference) != target
    ):
        return GitSyncResult("concurrent-upstream-changed", False)
    if not _is_clean(root):
        return GitSyncResult("dirty", False)
    if not _same_branch_head(root, upstream.branch_ref, current):
        return GitSyncResult("concurrent-ref-changed", False)
    if current == target:
        return GitSyncResult("up-to-date", False)
    if not _is_ancestor(root, current, target):
        status = "ahead" if _is_ancestor(root, target, current) else "divergent"
        return GitSyncResult(status, False)
    return GitSyncResult("manual-fast-forward-required", False)


def push(
    vault: Vault,
    remote: str = "origin",
    branch: str | None = None,
) -> GitSyncResult:
    """Push one captured validated commit to its configured upstream only."""
    safe_remote = _safe_remote(remote)
    root = _repository_root(vault)
    upstream, upstream_status = _configured_upstream(root, safe_remote, branch)
    if upstream is None:
        return GitSyncResult(upstream_status, False)
    current = _commit(root, "HEAD")
    if not _validate_commit_tree(root, current):
        return GitSyncResult("head-invalid", False)
    preflight = sync_preflight(vault)
    if not preflight.allowed:
        return GitSyncResult("blocked", False)
    if not _is_clean(root):
        return GitSyncResult("dirty", False)
    if not _same_branch_head(root, upstream.branch_ref, current):
        return GitSyncResult("concurrent-ref-changed", False)
    current_upstream, upstream_status = _configured_upstream(root, safe_remote, branch)
    if current_upstream != upstream:
        return GitSyncResult(
            "concurrent-ref-changed"
            if current_upstream is not None
            else upstream_status,
            False,
        )
    result = _run_git(
        root,
        (
            "push",
            "--porcelain",
            "--",
            safe_remote,
            f"{current}:refs/heads/{upstream.branch}",
        ),
        accepted_returncodes=(0, 1),
    )
    if result.returncode != 0:
        return GitSyncResult(_push_refusal_status(result.stdout), False)
    return GitSyncResult("pushed", True)


def _authorize_current_policy(
    vault: Vault, _paths: tuple[PurePosixPath, ...]
) -> LivePolicyDecision:
    """Fail closed over every current-tree artifact before Git mutation.

    The preflight path list remains exact for staging, while policy evaluation
    intentionally covers the whole current portable tree: committing one policy
    assignment must not distribute another now-noncompliant artifact.
    """
    try:
        from mneme.core.security import PolicyAuthorizer

        decision = PolicyAuthorizer(vault).authorize_current_tree()
        return LivePolicyDecision(decision.allowed, decision.issue_codes)
    except Exception:
        return LivePolicyDecision(False, ("policy-current-unavailable",))


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
    if not _GIT_OID.fullmatch(commit):
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
    return commit if _GIT_OID.fullmatch(commit) else None


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    result = _run_git(
        root,
        ("merge-base", "--is-ancestor", older, newer),
        accepted_returncodes=(0, 1),
    )
    return result.returncode == 0


def _configured_upstream(
    root: Path, expected_remote: str, requested_branch: str | None
) -> tuple[_ConfiguredUpstream | None, str]:
    """Return only the tracking destination configured for the current branch."""
    requested = None if requested_branch is None else _safe_branch(requested_branch)
    try:
        branch_ref, _branch = _current_branch(root)
        result = _run_git(
            root,
            ("rev-parse", "--symbolic-full-name", "@{upstream}"),
            accepted_returncodes=(0, 1, 128),
        )
    except GitSyncError:
        return None, "no-upstream"
    if result.returncode != 0:
        return None, "no-upstream"
    reference = result.stdout.strip()
    prefix = "refs/remotes/"
    if not reference.startswith(prefix):
        return None, "no-upstream"
    remote, separator, branch = reference[len(prefix) :].partition("/")
    if not separator:
        return None, "no-upstream"
    try:
        remote = _safe_remote(remote)
        branch = _safe_branch(branch)
    except GitSyncError:
        return None, "no-upstream"
    if remote != expected_remote or (requested is not None and requested != branch):
        return None, "upstream-mismatch"
    return _ConfiguredUpstream(branch_ref, remote, branch, reference), "ready"


def _configured_upstream_request(
    root: Path, expected_remote: str, requested_branch: str | None
) -> str:
    """Check branch tracking config before fetching an arbitrary caller remote."""
    requested = None if requested_branch is None else _safe_branch(requested_branch)
    try:
        _branch_ref, local_branch = _current_branch(root)
        remote_result = _run_git(
            root,
            ("config", "--get", f"branch.{local_branch}.remote"),
            accepted_returncodes=(0, 1),
        )
        merge_result = _run_git(
            root,
            ("config", "--get", f"branch.{local_branch}.merge"),
            accepted_returncodes=(0, 1),
        )
    except GitSyncError:
        return "no-upstream"
    if remote_result.returncode != 0 or merge_result.returncode != 0:
        return "no-upstream"
    remote = remote_result.stdout.strip()
    merge = merge_result.stdout.strip()
    prefix = "refs/heads/"
    if not merge.startswith(prefix):
        return "no-upstream"
    try:
        remote = _safe_remote(remote)
        branch = _safe_branch(merge[len(prefix) :])
    except GitSyncError:
        return "no-upstream"
    if remote != expected_remote or (requested is not None and requested != branch):
        return "upstream-mismatch"
    return "ready"


def _same_branch_head(root: Path, branch_ref: str, commit: str) -> bool:
    try:
        current_ref, _branch = _current_branch(root)
        return current_ref == branch_ref and _commit(root, "HEAD") == commit
    except GitSyncError:
        return False


def _validate_commit_tree(root: Path, commit: str) -> bool:
    """Validate a commit from blobs in an isolated Vault, never the checkout."""
    if not _GIT_OID.fullmatch(commit):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="mneme-git-tree-") as temporary:
            temporary_root = Path(temporary)
            vault_root = temporary_root / "vault"
            vault_root.mkdir()
            _materialize_canonical_tree(root, commit, vault_root)
            for relative in _PORTABLE_DIRECTORIES:
                (vault_root / relative).mkdir(parents=True, exist_ok=True)
            metadata = read_yaml(vault_root / ".madi" / "vault.yaml")
            vault_id = metadata.get("id")
            validate_identifier(vault_id, label="Vault id")
            state_home = temporary_root / "state"
            local_root = state_home / "vaults" / vault_id
            for relative in _LOCAL_DIRECTORIES:
                (local_root / relative).mkdir(parents=True, exist_ok=True)
            target_vault = Vault.open(vault_root, state_home)
            report = Doctor(target_vault).run()
            return not any(
                issue.severity == "invalid"
                and not issue.code.startswith("generated-index-")
                for issue in report.issues
            )
    except Exception:
        return False


def _materialize_canonical_tree(root: Path, commit: str, destination: Path) -> None:
    canonical_entries: list[tuple[str, PurePosixPath]] = []
    seen: set[PurePosixPath] = set()
    for mode, kind, object_id, relative in _tree_entries(root, commit):
        if not relative.parts or relative.parts[0] not in _CANONICAL_TREE_ROOTS:
            continue
        if mode != "100644" or kind != "blob" or not _GIT_OID.fullmatch(object_id):
            raise InvalidArtifact("Git target has an unsafe canonical tree entry")
        if relative in seen:
            raise InvalidArtifact("Git target contains duplicate canonical entries")
        seen.add(relative)
        canonical_entries.append((object_id, relative))
    for object_id, relative in canonical_entries:
        target = validate_contained_path(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        validate_path_chain(target, allow_missing=True)
        payload = _run_git_bytes(root, ("cat-file", "blob", object_id)).stdout
        with target.open("xb") as stream:
            stream.write(payload)


def _tree_entries(
    root: Path, commit: str
) -> tuple[tuple[str, str, str, PurePosixPath], ...]:
    result = _run_git_bytes(root, ("ls-tree", "-r", "-z", commit))
    entries: list[tuple[str, str, str, PurePosixPath]] = []
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            descriptor, raw_path = record.split(b"\t", 1)
            raw_mode, raw_kind, raw_object_id = descriptor.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            kind = raw_kind.decode("ascii")
            object_id = raw_object_id.decode("ascii")
        except (UnicodeDecodeError, ValueError):
            raise InvalidArtifact("Git target tree entry is malformed") from None
        entries.append((mode, kind, object_id, _safe_tree_relative(raw_path)))
    return tuple(entries)


def _safe_tree_relative(raw_path: bytes) -> PurePosixPath:
    try:
        value = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise InvalidArtifact("Git target path is not UTF-8") from None
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise InvalidArtifact("Git target path is unsafe")
    return path


@contextmanager
def _isolated_index(root: Path, parent: str) -> Iterator[dict[str, str]]:
    """Create a HEAD-derived index that cannot mutate the caller's index."""
    with tempfile.TemporaryDirectory(prefix="mneme-git-index-") as temporary:
        environment = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
        _run_git(root, ("read-tree", parent), environment=environment)
        yield environment


def _index_entries(
    root: Path,
    pathspecs: tuple[str, ...],
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[PurePosixPath, tuple[str, str, str]]:
    result = _run_git_bytes(
        root,
        ("ls-files", "--stage", "-z", "--", *pathspecs),
        environment=environment,
    )
    entries: dict[PurePosixPath, tuple[str, str, str]] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            descriptor, raw_path = record.split(b"\t", 1)
            raw_mode, raw_object_id, raw_stage = descriptor.split(b" ", 2)
            mode = raw_mode.decode("ascii")
            object_id = raw_object_id.decode("ascii")
            stage = raw_stage.decode("ascii")
        except (UnicodeDecodeError, ValueError):
            raise GitSyncError("Git index validation failed safely") from None
        path = _safe_tree_relative(raw_path)
        if path in entries:
            raise GitSyncError("Git index validation failed safely")
        entries[path] = (mode, object_id, stage)
    return entries


def _selected_entries_are_regular(
    canonical_paths: tuple[PurePosixPath, ...],
    entries: Mapping[PurePosixPath, tuple[str, str, str]],
) -> bool:
    return (
        set(entries) == set(canonical_paths)
        and all(
            mode == "100644" and stage == "0" and _GIT_OID.fullmatch(object_id)
            for mode, object_id, stage in entries.values()
        )
    )


def _tree_oid(
    root: Path, *, environment: Mapping[str, str]
) -> str:
    tree = _run_git(root, ("write-tree",), environment=environment).stdout.strip()
    if not _GIT_OID.fullmatch(tree):
        raise GitSyncError("Git tree validation failed safely")
    return tree


def _commit_tree(root: Path, tree: str, parent: str, message: str) -> str:
    commit = _run_git(
        root,
        ("commit-tree", tree, "-p", parent, "-m", message),
    ).stdout.strip()
    if not _GIT_OID.fullmatch(commit):
        raise GitSyncError("Git commit validation failed safely")
    return commit


def _update_ref_cas(root: Path, reference: str, candidate: str, parent: str) -> bool:
    result = _run_git(
        root,
        ("update-ref", reference, candidate, parent),
        accepted_returncodes=(0, 1, 128),
    )
    return result.returncode == 0


def _push_refusal_status(porcelain: str) -> str:
    """Classify only the stable porcelain rejection marker, never Git diagnostics."""
    return (
        "remote-conflict"
        if any(line.startswith("!") for line in porcelain.splitlines())
        else "push-rejected"
    )


def _synchronize_primary_index(
    root: Path,
    pathspecs: tuple[str, ...],
    before: Mapping[PurePosixPath, tuple[str, str, str]],
    staged: Mapping[PurePosixPath, tuple[str, str, str]],
) -> None:
    """Reflect a successful explicit commit without rereading its worktree paths."""
    try:
        if _index_entries(root, pathspecs) != before:
            return
        arguments = ["update-index", "--add"]
        for path, (mode, object_id, stage) in sorted(staged.items()):
            if mode != "100644" or stage != "0" or not _GIT_OID.fullmatch(object_id):
                return
            arguments.extend(("--cacheinfo", f"{mode},{object_id},{path.as_posix()}"))
        _run_git(root, tuple(arguments))
    except GitSyncError:
        return


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
    environment: Mapping[str, str] | None = None,
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
            env=_git_environment(environment),
        )
    except (OSError, subprocess.SubprocessError):
        raise GitSyncError("Git operation failed safely") from None
    if result.returncode not in accepted_returncodes:
        raise GitSyncError("Git operation failed safely")
    return result


def _run_git_bytes(
    root: Path,
    arguments: tuple[str, ...],
    *,
    accepted_returncodes: tuple[int, ...] = (0,),
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run Git for binary object/index records without decoding unsafe output."""
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=False,
            shell=False,
            env=_git_environment(environment),
        )
    except (OSError, subprocess.SubprocessError):
        raise GitSyncError("Git operation failed safely") from None
    if result.returncode not in accepted_returncodes:
        raise GitSyncError("Git operation failed safely")
    return result


def _git_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    result = os.environ.copy()
    for name in _GIT_ENVIRONMENT_OVERRIDES:
        result.pop(name, None)
    if environment is not None:
        result.update(environment)
    result["GIT_NO_REPLACE_OBJECTS"] = "1"
    return result
