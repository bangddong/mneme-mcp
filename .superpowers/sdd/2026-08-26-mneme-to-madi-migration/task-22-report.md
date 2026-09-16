# Task 22 report: pinned Codex PreCompact translation

## Status and scope

Implemented Task 22 from clean base `a5aa083` on the linked
`feat/madi-core-migration` worktree. The native Codex boundary is confined to
`mneme.adapters.codex`; Core receives only a plain `LifecycleEvent` and explicit
`CoreCommand`. No repository/package rename, legacy integration change, remote
operation, transcript read, user-home configuration write, or real-project
installer invocation was performed.

The Task 21 credential matcher was not changed. In particular, the parked
oversized-whitespace placeholder-cap ruling and the unrelated ledgered numeric
version issue remain outside Task 22.

## Pinned provenance evidence

`integration/madi/codex/README.md` records all binding source details:

| Item | Pinned value |
| --- | --- |
| Adapter schema ID | `openai-codex-hooks/pre-compact@2026-08-26+a26f1806` |
| Release behavior, checked 2026-08-26 | `https://learn.chatgpt.com/docs/hooks` |
| Generated schema commit | `a26f1806a4f4b8cfec2ea1be129963815a61e58c` |
| Generated schema URL | `https://github.com/openai/codex/blob/a26f1806a4f4b8cfec2ea1be129963815a61e58c/codex-rs/hooks/schema/generated/pre-compact.command.input.schema.json` |
| Release warning | Release documentation is behavioral authority; `main` schemas may contain unreleased fields. |

The native fixture pins the seven required fields (`session_id`, nullable
`transcript_path`, `cwd`, exact `hook_event_name`, `model`, `turn_id`, and the
`manual|auto` trigger enum) and includes optional `agent_id` / `agent_type`.
Tests prove removing the optional fields leaves translation unchanged and that
all private native values except the allowed adapter-local trigger/turn metadata
are absent from the normalized event and canonical artifacts.

## Strict TDD evidence

| Phase | Command | Result |
| --- | --- | --- |
| Existing boundary baseline | `python -m pytest tests/adapters/test_claude.py tests/integration/test_install_claude.py tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -q --basetemp .pytest-task22-baseline` | `166 passed in 150.59s` |
| Native fixture/translation RED | `python -m pytest tests/adapters/test_codex.py -v --tb=short --basetemp .pytest-task22-native-red` | Expected RED: `6 failed in 0.47s`; `mneme.adapters.codex` did not exist. |
| Native fixture/translation GREEN | Same test file with `.pytest-task22-native-green` | `6 passed in 0.16s` |
| Normalized adapter/Core RED | `python -m pytest tests/adapters/test_codex.py -v --tb=short --basetemp .pytest-task22-normalized-red` | Expected RED: `8 failed, 6 passed in 5.21s`; `CodexAdapter` did not exist. |
| Normalized adapter/Core GREEN plus Claude boundary | `python -m pytest tests/adapters/test_codex.py tests/adapters/test_claude.py -q --basetemp .pytest-task22-adapters-green` | `33 passed in 34.79s` |
| Runner/installer RED | `python -m pytest tests/integration/test_install_codex.py -v --tb=short --basetemp .pytest-task22-integration-red` | Expected RED: `15 failed in 0.89s`; runner/config/installer artifacts did not exist. |
| Runner/installer GREEN | Same test file with `.pytest-task22-integration-green` | `15 passed in 1.26s` |

The RED failures were caused by the requested behavior being absent, not by
fixture syntax or collection failures. Production code was added only after the
corresponding RED was observed.

## Final verification

| Check | Result |
| --- | --- |
| Task22 + Task21 + Task20 boundary command: `python -m pytest tests/adapters/test_codex.py tests/integration/test_install_codex.py tests/adapters/test_claude.py tests/integration/test_install_claude.py tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -v --basetemp .pytest-task22-focused` | `195 passed in 150.55s` |
| `python -m pytest --collect-only -q` | `682 tests collected in 0.96s` |
| Definitive full suite: `python -m pytest -q --cache-clear --basetemp .pytest-task22-full` | `682 passed in 1192.50s (0:19:52)`, exit 0 |
| Compiler check | `python -m py_compile` over both adapter modules, Codex translator/runner, and installer: exit 0 |
| Boundary search | No forbidden Claude/Codex/FastMCP/daemon imports in `mneme/core`; native field names occur only in the Codex adapter/integration documentation. |
| Whitespace | `git diff --check`: clean before staging; repeated on the staged tree before commit. |

All task-scoped pytest directories were resolved under the assigned worktree and
removed by exact path after verification. Tests wrote only to their disposable
project/base-temp roots.

## Requirement matrix

| Requirement | Evidence |
| --- | --- |
| Pinned official native contract | Closed required/optional field set, exact event and trigger validation, fixture pair, and README provenance above. |
| Agent-neutral Core boundary | `translate_pre_compact` returns a lifecycle subtype with adapter-local metadata; `CodexAdapter.handle_native` strips it to an exact plain `LifecycleEvent` before observation. |
| Native privacy | Translator never resolves or opens `transcript_path`; cwd/model/agent fields are discarded; native details are absent from Session/Registry content and all closed errors. |
| Machine-local workstream binding | Native payload has no workstream field; translation accepts only the closed local `{workstream_id}` binding. The runner sources its CLI binding from `MADI_CODEX_WORKSTREAM_ID`. |
| Nonblocking failure | Missing/wrong native input, unknown trigger, missing binding, translator failure, and unavailable Core all produce closed warnings with `block_host=false`; hook exit is zero. |
| Observation versus command | Bare PreCompact observes a plain lifecycle event, requests a checkpoint, and writes nothing. A host-composed checkpoint reuses the Claude adapter's confined reader and exact `create_session_revision` command path. |
| Agent switch | A Codex revision records `continues_from` the Claude revision in a distinct session; the parallel registry remains unpreferred until a separately tested `set_preferred_head` command. |
| Hook integration | `hooks.json` registers only `PreCompact` with matcher `manual|auto`; stdin runner calls the translator once and emits only normalized/adapter-local safe fields. |
| Installer preservation | Disposable-project tests retain existing JSON fields, other hooks, `.codex/config.toml` bytes, and `AGENTS.md` bytes; Madi is added once; malformed JSON, conflicts, marker spoofing, and linked targets fail before writes. |
| Procedural guidance | `AGENTS.md.template` marks Madi optional, prohibits transcript/tool/native-detail capture, requires an explicit sanitized checkpoint, and tells Codex to continue ordinary work on failure. It contains no credential or absolute Vault path. |

## Files

- `mneme/adapters/base.py`
- `mneme/adapters/claude.py`
- `mneme/adapters/codex.py`
- `integration/madi/codex/AGENTS.md.template`
- `integration/madi/codex/hooks.json`
- `integration/madi/codex/pre_compact_hook.py`
- `integration/madi/codex/README.md`
- `integration/madi/install_codex.py`
- `tests/fixtures/codex/pre_compact.native.json`
- `tests/fixtures/codex/pre_compact.normalized.json`
- `tests/adapters/test_codex.py`
- `tests/integration/test_install_codex.py`
- `.superpowers/sdd/2026-08-26-mneme-to-madi-migration/task-22-report.md`

## Commit and concerns

Implementation commit message: `feat: translate official Codex PreCompact hooks`.
The resulting SHA is supplied in the Task 22 handoff because a commit cannot
embed its own hash in its contents.

No unresolved implementation concern remains. Actual host attachment is
intentionally optional and fail-open per the brief; the installed runner requires
an explicit machine-local workstream binding and never invents one.
