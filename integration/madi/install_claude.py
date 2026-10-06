#!/usr/bin/env python3
"""Install the optional Madi stdio integration into one explicit Claude project."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, Sequence


_KIT = Path(__file__).resolve().parent
_BEGIN = "<!-- MADI-CLAUDE-ADAPTER: BEGIN -->"
_END = "<!-- MADI-CLAUDE-ADAPTER: END -->"


class InstallError(Exception):
    """A project-local installation preflight failed without modifying the project."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    changed: bool


def install(project: Path | str) -> InstallResult:
    """Add the Madi stdio server and rules once, preserving existing project data."""
    root = _project_root(project)
    mcp_path = _project_file(root, ".mcp.json")
    rules_path = _project_file(root, "CLAUDE.md")
    desired_server = _desired_server()

    mcp_data = _read_mcp(mcp_path)
    if "mcpServers" not in mcp_data:
        servers = {}
        mcp_data["mcpServers"] = servers
    else:
        servers = mcp_data["mcpServers"]
    if not isinstance(servers, dict):
        raise InstallError("project MCP server configuration is not an object")
    has_existing_server = "madi" in servers
    existing_server = servers.get("madi")
    if has_existing_server and existing_server != desired_server:
        raise InstallError("project already has a different Madi server configuration")
    mcp_changed = not has_existing_server
    if mcp_changed:
        servers["madi"] = desired_server
        next_mcp = json.dumps(mcp_data, ensure_ascii=False, indent=2) + "\n"
    else:
        next_mcp = None

    existing_rules = _read_text(rules_path) if rules_path.exists() else ""
    rules_changed, next_rules = _merged_rules(existing_rules)

    # Validate every target before writing either one, so a malformed project
    # cannot receive a half-installed integration.
    _project_file(root, ".mcp.json")
    _project_file(root, "CLAUDE.md")
    if mcp_changed:
        _atomic_write(mcp_path, next_mcp)
    if rules_changed:
        _atomic_write(rules_path, next_rules)
    return InstallResult(changed=mcp_changed or rules_changed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly add the optional Madi stdio integration to one project."
    )
    parser.add_argument("--project", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = install(arguments.project)
    except InstallError as exc:
        parser.error(str(exc))
    print(json.dumps({"changed": result.changed}))
    return 0


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
    if not resolved.is_relative_to(root) or _is_link_or_reparse(target):
        raise InstallError("project configuration path is unsafe")
    return target


def _read_mcp(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(
            _read_text(path), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise InstallError("project MCP configuration is not strict JSON") from exc
    if not isinstance(value, dict):
        raise InstallError("project MCP configuration is not an object")
    return value


def _desired_server() -> dict[str, Any]:
    template = _KIT / "claude" / "mcp-stdio.json"
    try:
        parsed = json.loads(
            _read_text(template), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
        value = parsed["mcpServers"]["madi"]
    except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallError("bundled Madi stdio template is invalid") from exc
    expected = {
        "type": "stdio",
        "command": "python",
        "args": ["-m", "mneme.transports.mcp_stdio"],
    }
    if value != expected:
        raise InstallError("bundled Madi stdio template is invalid")
    return expected


def _merged_rules(existing: str) -> tuple[bool, str]:
    block = _read_text(_KIT / "claude" / "CLAUDE.md.template")
    if block.count(_BEGIN) != 1 or block.count(_END) != 1:
        raise InstallError("bundled Madi rules template is invalid")
    begins = existing.count(_BEGIN)
    ends = existing.count(_END)
    if begins == ends == 1:
        if existing.find(_BEGIN) > existing.find(_END):
            raise InstallError("existing Madi rules have incomplete markers")
        start = existing.find(_BEGIN)
        finish = existing.find(_END) + len(_END)
        if existing[start:finish] != block.rstrip("\r\n"):
            raise InstallError("existing Madi rules do not match this integration")
        return False, existing
    if begins or ends:
        raise InstallError("existing Madi rules have incomplete markers")
    if not existing:
        return True, block
    separator = "\n\n" if not existing.endswith("\n") else "\n"
    return True, existing + separator + block


def _read_text(path: Path) -> str:
    try:
        # The rules merger appends to user-authored Markdown.  Disable universal
        # newline translation so the original byte sequence is preserved.
        with path.open("r", encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError) as exc:
        raise InstallError("project configuration cannot be read as UTF-8") from exc


def _atomic_write(path: Path, value: str | None) -> None:
    if not isinstance(value, str):
        raise InstallError("installer write content is invalid")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_chain(path, allow_missing=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            _validate_chain(temporary, allow_missing=False)
            _validate_chain(path, allow_missing=True)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    except (OSError, ValueError) as exc:
        raise InstallError("project configuration cannot be written safely") from exc


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
    raise ValueError(f"invalid JSON constant: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
