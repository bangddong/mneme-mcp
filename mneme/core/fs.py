"""Explicit UTF-8 codecs and atomic filesystem primitives."""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

import yaml

from mneme.core.errors import ArtifactExists, ConcurrentWrite, InvalidArtifact


_cas_locks_guard = threading.Lock()
_cas_locks: dict[Path, threading.Lock] = {}


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
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InvalidArtifact(f"cannot read YAML artifact: {path}") from exc
    if not isinstance(value, dict):
        raise InvalidArtifact(f"YAML artifact is not a mapping: {path}")
    return value


def dump_frontmatter(metadata: Mapping[str, Any], body: str) -> str:
    """Encode deterministic YAML frontmatter and a normalized Markdown body."""
    frontmatter = dump_yaml(metadata)
    return f"---\n{frontmatter}---\n{normalize_text(body)}"


def read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Read strict YAML frontmatter and Markdown body from a UTF-8 file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InvalidArtifact(f"cannot read Markdown artifact: {path}") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise InvalidArtifact(f"Markdown artifact has no frontmatter: {path}")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise InvalidArtifact(f"Markdown artifact has unclosed frontmatter: {path}")
    try:
        metadata = yaml.safe_load(normalized[4:end])
    except yaml.YAMLError as exc:
        raise InvalidArtifact(f"invalid YAML frontmatter: {path}") from exc
    if not isinstance(metadata, dict):
        raise InvalidArtifact(f"frontmatter is not a mapping: {path}")
    body = normalize_text(normalized[end + len("\n---\n") :])
    return metadata, body


def write_new(path: Path, text: str) -> None:
    """Create a new UTF-8 artifact exclusively; never overwrite an existing path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(normalize_text(text))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ArtifactExists(f"artifact already exists: {target}") from exc


def replace_text(path: Path, text: str) -> None:
    """Atomically replace a text file using a same-directory temporary."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
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
    target = Path(path).resolve(strict=False)
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
    target = Path(path).resolve(strict=False)
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
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
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
