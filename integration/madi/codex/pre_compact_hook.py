#!/usr/bin/env python3
"""Fail-open stdin runner for the pinned Codex ``PreCompact`` payload."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import sys
from typing import Any, TextIO

from mneme.adapters.codex import CodexLifecycleEvent, translate_pre_compact


_MAX_STDIN_CHARS = 256 * 1024
_WARNING = (
    "Madi could not process this Codex PreCompact hook; continue ordinary Codex work."
)


def run_hook(
    stdin: TextIO,
    stdout: TextIO,
    *,
    local_binding: object,
    translator: Callable[[object, object], object] = translate_pre_compact,
) -> int:
    """Translate one stdin document and always return a non-blocking exit status."""
    try:
        raw = stdin.read(_MAX_STDIN_CHARS + 1)
        if len(raw) > _MAX_STDIN_CHARS:
            raise ValueError("hook input exceeds the bounded size")
        payload = json.loads(
            raw,
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
        translated = translator(payload, local_binding)
        if not isinstance(translated, CodexLifecycleEvent):
            raise TypeError("translator returned an invalid lifecycle event")
        diagnostic: dict[str, Any] = {
            "block_host": False,
            "checkpoint_required": True,
            "context_required": False,
            "event": translated.name,
            "lifecycle": translated.as_core_event().as_dict(),
            "native_metadata": translated.native_metadata,
            "ok": True,
            "version": translated.version,
            "warning": None,
        }
    except Exception:
        diagnostic = {
            "block_host": False,
            "checkpoint_required": False,
            "context_required": False,
            "event": "unknown",
            "lifecycle": None,
            "native_metadata": None,
            "ok": False,
            "version": 1,
            "warning": _WARNING,
        }
    stdout.write(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


def main() -> int:
    binding = {"workstream_id": os.environ.get("MADI_CODEX_WORKSTREAM_ID")}
    return run_hook(sys.stdin, sys.stdout, local_binding=binding)


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
