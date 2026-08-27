# Task 3 Report: Read-Only Sources and Wiki Compatibility

## Status

DONE

## TDD evidence

- **Red:** `C:\Users\dhbang\AppData\Local\Programs\Python\Python313\python.exe -m pytest tests/core/sources/test_filesystem.py tests/core/validation/test_wiki_facade.py -v` failed as expected before implementation: `mneme.core.sources`, `mneme.legacy`, and `mneme.core.validation` did not exist (4 failed tests).
- **Green:** The same focused source/facade command passed after implementation (4 passed).
- **Focused compatibility:** `python -m pytest tests/core/sources/test_filesystem.py tests/core/validation/test_wiki_facade.py tests/characterization/test_legacy_contracts.py -v` passed (9 passed).
- **CLI:** `python -m mneme.lint` against the repository Wiki returned exit 0 and its normal empty-Wiki report.
- **HTTP entry point:** importing `mneme.server:main` succeeded.
- **Full:** `python -m pytest -q` passed (52 passed).

## Changes

- Added `FileSystemSource`, a read-only UTF-8 Markdown source with deterministic relative paths and fail-closed handling for absolute paths, traversal, alternate separators, and symlinks/resolved escapes.
- Moved mutable Wiki behavior to `mneme.legacy.wiki`; `mneme.wiki` is a module-object compatibility alias.
- Moved Wiki validation to `mneme.core.validation.wiki`; `mneme.lint` is a module-object compatibility alias and still delegates the module CLI to `main()`.
- Added behavior tests for source safety/determinism and facade identity/private-helper monkeypatching.

## Files

- `mneme/core/sources/__init__.py`
- `mneme/core/sources/filesystem.py`
- `mneme/core/validation/__init__.py`
- `mneme/core/validation/wiki.py`
- `mneme/legacy/__init__.py`
- `mneme/legacy/wiki.py`
- `mneme/wiki.py`
- `mneme/lint.py`
- `tests/core/sources/test_filesystem.py`
- `tests/core/validation/test_wiki_facade.py`

## Deviations

- Backslash-separated source input is rejected rather than accepted after separator normalization. This is the stricter interpretation of the task's explicit fail-closed requirement for alternate separators; returned/listed paths remain normalized as POSIX-relative strings.
