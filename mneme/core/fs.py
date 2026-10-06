"""Explicit UTF-8 codecs and atomic filesystem primitives."""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

import yaml

from mneme.core.errors import (
    ArtifactExists,
    ConcurrentWrite,
    InvalidArtifact,
    UnsafePath,
)


_cas_locks_guard = threading.Lock()
_cas_locks: dict[Path, threading.Lock] = {}


def validate_path_chain(path: Path, *, allow_missing: bool) -> Path:
    """Reject links and reparse points in every existing lexical component.

    Components are inspected in order with ``lstat`` from the filesystem anchor,
    so validation never reaches a child through a link that was already present.
    ``allow_missing`` permits the first absent component and its necessarily
    absent tail, which is required for safe initialization preflight.

    This is a best-effort path preflight, not a race-free open.  Another process
    can replace a checked component before the subsequent filesystem operation;
    eliminating that race requires platform-specific directory-handle traversal.
    Callers therefore repeat validation immediately before sensitive I/O.
    """
    if not isinstance(allow_missing, bool):
        raise TypeError("allow_missing must be a boolean")
    candidate = Path(path)
    if candidate.drive and not candidate.root:
        raise UnsafePath(f"path uses a drive-relative anchor: {candidate}")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if not candidate.is_absolute() or not candidate.anchor or ".." in candidate.parts:
        raise UnsafePath(f"path is not lexically anchored: {candidate}")

    current = Path(candidate.anchor)
    components = [current]
    for part in candidate.parts[1:]:
        current /= part
        components.append(current)
    missing = False
    for component in components:
        if missing:
            continue
        try:
            metadata = os.lstat(component)
        except FileNotFoundError as exc:
            if not allow_missing:
                raise UnsafePath(f"path component is missing: {component}") from exc
            missing = True
            continue
        except (OSError, ValueError) as exc:
            raise UnsafePath(f"cannot inspect path component: {component}") from exc
        if _metadata_is_link_or_reparse(metadata):
            raise UnsafePath(
                f"path component is a link or reparse point: {component}"
            )
    return candidate


def is_symlink_or_reparse(path: Path) -> bool:
    """Return whether *path* is a symlink or any Windows reparse point.

    ``Path.is_symlink`` does not identify Windows junctions.  ``lstat`` reads
    the directory entry itself, and the Windows file-attribute bit covers
    junctions and other reparse types without following their targets.
    """
    try:
        metadata = os.lstat(Path(path))
    except (OSError, ValueError):
        return False
    return _metadata_is_link_or_reparse(metadata)


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def normalize_text(text: str) -> str:
    """Return LF-only text with exactly one terminal newline."""
    if not isinstance(text, str):
        raise InvalidArtifact("artifact text must be a string")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip("\n") + "\n"


def dump_yaml(value: Mapping[str, Any]) -> str:
    """Serialize a YAML mapping with stable key order, UTF-8 text, and LF endings."""
    if not isinstance(value, Mapping):
        raise InvalidArtifact("YAML artifact must be a mapping")
    dumped = yaml.safe_dump(
        dict(value),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
        line_break="\n",
    )
    return normalize_text(dumped)


def read_yaml(path: Path) -> dict[str, Any]:
    """Read one UTF-8 YAML mapping."""
    target = validate_path_chain(path, allow_missing=True)
    try:
        value = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InvalidArtifact(f"cannot read YAML artifact: {target}") from exc
    if not isinstance(value, dict):
        raise InvalidArtifact(f"YAML artifact is not a mapping: {target}")
    return value


def dump_frontmatter(metadata: Mapping[str, Any], body: str) -> str:
    """Encode deterministic YAML frontmatter and a normalized Markdown body."""
    frontmatter = dump_yaml(metadata)
    return f"---\n{frontmatter}---\n{normalize_text(body)}"


def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Read strict YAML frontmatter and Markdown body from a UTF-8 file."""
    target = validate_path_chain(path, allow_missing=True)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InvalidArtifact(f"cannot read Markdown artifact: {target}") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise InvalidArtifact(f"Markdown artifact has no frontmatter: {target}")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise InvalidArtifact(f"Markdown artifact has unclosed frontmatter: {target}")
    try:
        metadata = yaml.safe_load(normalized[4:end])
    except yaml.YAMLError as exc:
        raise InvalidArtifact(f"invalid YAML frontmatter: {target}") from exc
    if not isinstance(metadata, dict):
        raise InvalidArtifact(f"frontmatter is not a mapping: {target}")
    body = normalize_text(normalized[end + len("\n---\n") :])
    return metadata, body


def write_new(path: Path, text: str) -> None:
    """Create a new UTF-8 artifact exclusively; never overwrite an existing path."""
    target = validate_path_chain(path, allow_missing=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    validate_path_chain(target, allow_missing=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(normalize_text(text))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ArtifactExists(f"artifact already exists: {target}") from exc


def replace_text(path: Path, text: str) -> None:
    """Atomically replace a text file using a same-directory temporary."""
    target = validate_path_chain(path, allow_missing=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    validate_path_chain(target, allow_missing=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(normalize_text(text))
            stream.flush()
            os.fsync(stream.fileno())
        validate_path_chain(temporary, allow_missing=False)
        validate_path_chain(target, allow_missing=True)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_yaml_cas(
    path: Path, value: dict[str, Any], expected_generation: int
) -> None:
    """Replace a YAML registry only when its generation matches exactly."""
    target = validate_path_chain(path, allow_missing=True).resolve(strict=False)
    write_text_cas(
        target,
        dump_yaml(value),
        expected_generation=expected_generation,
        next_generation=value.get("generation"),
        read_generation=lambda: read_yaml(target).get("generation"),
    )


def write_text_cas(
    path: Path,
    text: str,
    *,
    expected_generation: int,
    next_generation: object,
    read_generation: Callable[[], object],
) -> None:
    """Atomically replace text after a process-local generation CAS."""
    target = validate_path_chain(path, allow_missing=True).resolve(strict=False)
    lock = _lock_for(target)
    with lock:
        current_generation = read_generation()
        if current_generation != expected_generation:
            raise ConcurrentWrite(
                f"stale generation for {target}: expected {expected_generation}, "
                f"found {current_generation!r}"
            )
        if next_generation != expected_generation + 1:
            raise InvalidArtifact(
                "CAS replacement generation must be exactly expected_generation + 1"
            )
        replace_text(target, text)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize processes through a stable machine-local lock file."""
    lock_path = validate_path_chain(path, allow_missing=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    validate_path_chain(lock_path, allow_missing=True)
    with lock_path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        _lock_stream(stream)
        try:
            yield
        finally:
            _unlock_stream(stream)


def _lock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)


def _unlock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _lock_for(path: Path) -> threading.Lock:
    with _cas_locks_guard:
        return _cas_locks.setdefault(path, threading.Lock())
