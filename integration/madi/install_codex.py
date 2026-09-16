#!/usr/bin/env python3
"""Install the optional Madi Codex hook into one explicit project."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any


_KIT = Path(__file__).resolve().parent
_BEGIN = "<!-- MADI-CODEX-ADAPTER: BEGIN -->"
_END = "<!-- MADI-CODEX-ADAPTER: END -->"
_HOOK_COMMAND = "python -m integration.madi.codex.pre_compact_hook"


class InstallError(Exception):
    """A project-local installation preflight failed without modifying the project."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    changed: bool


def install(project: Path | str) -> InstallResult:
    """Add the Madi hook and instructions once while preserving project data."""
    root = _project_root(project)
    hooks_path = _project_file(root, ".codex/hooks.json")
    agents_path = _project_file(root, "AGENTS.md")
    desired = _desired_registration()

    hook_data = _read_hooks(hooks_path)
    hooks_changed, next_hooks = _merged_hooks(hook_data, desired)
    existing_agents = _read_text(agents_path) if agents_path.exists() else ""
    agents_changed, next_agents = _merged_agents(existing_agents)

    # Revalidate both targets before the first write so malformed projects do
    # not receive a partial integration during ordinary preflight failures.
    _project_file(root, ".codex/hooks.json")
    _project_file(root, "AGENTS.md")
    if hooks_changed:
        _atomic_write(hooks_path, next_hooks)
    if agents_changed:
        _atomic_write(agents_path, next_agents)
    return InstallResult(changed=hooks_changed or agents_changed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly add the optional Madi PreCompact hook to one project."
    )
    parser.add_argument("--project", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = install(arguments.project)
    except InstallError as exc:
        parser.error(str(exc))
    print(json.dumps({"changed": result.changed}))
    return 0


def _desired_registration() -> dict[str, Any]:
    expected = {
        "hooks": {
            "PreCompact": [
                {
                    "matcher": "manual|auto",
                    "hooks": [{"type": "command", "command": _HOOK_COMMAND}],
                }
            ]
        }
    }
    try:
        bundled = json.loads(
            _read_text(_KIT / "codex" / "hooks.json"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise InstallError("bundled Codex hook configuration is invalid") from exc
    if bundled != expected:
        raise InstallError("bundled Codex hook configuration is invalid")
    return bundled["hooks"]["PreCompact"][0]


def _read_hooks(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(
            _read_text(path),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise InstallError("project Codex hooks are not strict JSON") from exc
    if not isinstance(value, dict):
        raise InstallError("project Codex hook configuration is not an object")
    return value


def _merged_hooks(
    value: dict[str, Any], desired: dict[str, Any]
) -> tuple[bool, str | None]:
    if "hooks" not in value:
        hooks: dict[str, Any] = {}
        value["hooks"] = hooks
    else:
        hooks = value["hooks"]
    if not isinstance(hooks, dict):
        raise InstallError("project Codex hooks field is not an object")
    if "PreCompact" not in hooks:
        registrations: list[Any] = []
        hooks["PreCompact"] = registrations
    else:
        registrations = hooks["PreCompact"]
    if not isinstance(registrations, list):
        raise InstallError("project PreCompact hooks are not a list")
    if not all(isinstance(item, Mapping) for item in registrations):
        raise InstallError("project PreCompact hook entry is invalid")
    matches = sum(item == desired for item in registrations)
    if matches > 1:
        raise InstallError("project contains duplicate Madi PreCompact hooks")
    if matches == 1:
        return False, None
    if any(_uses_madi_command(item) for item in registrations):
        raise InstallError("project already has a conflicting Madi PreCompact hook")
    registrations.append(desired)
    return True, json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _uses_madi_command(value: Mapping[str, Any]) -> bool:
    hooks = value.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(item, Mapping) and item.get("command") == _HOOK_COMMAND
        for item in hooks
    )


def _merged_agents(existing: str) -> tuple[bool, str]:
    block = _read_text(_KIT / "codex" / "AGENTS.md.template")
    if block.count(_BEGIN) != 1 or block.count(_END) != 1:
        raise InstallError("bundled Madi Codex instructions are invalid")
    begins = existing.count(_BEGIN)
    ends = existing.count(_END)
    if begins == ends == 1:
        if existing.find(_BEGIN) > existing.find(_END):
            raise InstallError("existing Madi Codex instructions have invalid markers")
        start = existing.find(_BEGIN)
        finish = existing.find(_END) + len(_END)
        if existing[start:finish] != block.rstrip("\r\n"):
            raise InstallError("existing Madi Codex instructions do not match")
        return False, existing
    if begins or ends:
        raise InstallError("existing Madi Codex instructions have invalid markers")
    if not existing:
        return True, block
    separator = "\n\n" if not existing.endswith("\n") else "\n"
    return True, existing + separator + block


def _project_root(value: Path | str) -> Path:
    try:
        root = _validate_chain(Path(value), allow_missing=False)
        if not root.is_dir() or _is_link_or_reparse(root):
            raise InstallError("project root is unsafe")
        return root.resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise InstallError("project root is unavailable") from exc


def _project_file(root: Path, name: str) -> Path:
    try:
        target = _validate_chain(root / name, allow_missing=True)
        resolved = target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InstallError("project configuration path is unsafe") from exc
    if not resolved.is_relative_to(root):
        raise InstallError("project configuration path is unsafe")
    if target.exists() and _is_link_or_reparse(target):
        raise InstallError("project configuration path is unsafe")
    return target


def _read_text(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError) as exc:
        raise InstallError("project configuration cannot be read as UTF-8") from exc


def _atomic_write(path: Path, value: str | None) -> None:
    if not isinstance(value, str):
        raise InstallError("installer write content is invalid")
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_chain(path, allow_missing=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_chain(temporary, allow_missing=False)
        _validate_chain(path, allow_missing=True)
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError) as exc:
        raise InstallError("project configuration cannot be written safely") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _validate_chain(path: Path, *, allow_missing: bool) -> Path:
    candidate = Path(path)
    if candidate.drive and not candidate.root:
        raise InstallError("project configuration path is unsafe")
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise InstallError("project configuration path is unsafe")
    current = Path(candidate.anchor)
    missing = False
    for part in candidate.parts[1:]:
        current /= part
        if missing:
            continue
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if not allow_missing:
                raise InstallError("project configuration path is unavailable") from None
            missing = True
            continue
        except (OSError, ValueError) as exc:
            raise InstallError("project configuration path is unsafe") from exc
        if _metadata_is_link_or_reparse(metadata):
            raise InstallError("project configuration path is unsafe")
    return candidate


def _is_link_or_reparse(path: Path) -> bool:
    try:
        return _metadata_is_link_or_reparse(os.lstat(path))
    except (OSError, ValueError):
        return True


def _metadata_is_link_or_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object field")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


if __name__ == "__main__":
    raise SystemExit(main())
