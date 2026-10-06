#!/usr/bin/env python3
"""Source-tree compatibility wrapper for the installed Codex hook runner."""

from mneme.adapters.codex_hook import main, run_hook


__all__ = ["main", "run_hook"]


if __name__ == "__main__":
    raise SystemExit(main())
