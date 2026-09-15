# Task 21 report: opt-in non-blocking Claude adapter

## Scope and inherited audit

This task started at `c81cab974a485ade4dd5a900a81eff72c4233a61` in the
`feat/madi-core-migration` linked worktree.  The inherited uncommitted Task 21
implementation was limited to the eight files listed below; there was no Task 21
commit or report.  `integration/install.py` was inspected and remains unchanged.

The audit confirmed that the adapter terminates Claude-specific input at
`mneme.adapters`: it accepts the closed v1 envelope, converts it to the Task 20
`LifecycleEvent`, and can issue only `create_session_revision` after reading an
explicit host-composed checkpoint.  A bare lifecycle event is observation-only.
All adapter exceptions become closed, non-blocking warnings.  The adapter neither
imports a Claude SDK nor accepts transcript/tool-output fields, and its result
never contains the checkpoint path.  Checkpoint reads are bounded, require a
regular JSON file below the project or configured temporary root, reject links and
unexpected fields, and require matching session/workstream/adapter identities.

The installer is explicitly opt-in and uses the stdio command `python -m
mneme.transports.mcp_stdio`; it has no Vault path, credential, or HTTP endpoint.
It preserves existing JSON server values, validates both project targets before
writing, and rejects conflicting `madi` configuration.  During this audit two
installer gaps were found and corrected through red-green tests:

1. Text-mode reads translated existing `CLAUDE.md` CRLF bytes before appending the
   Madi block. `_read_text` now disables universal-newline translation.
2. A pair of Madi marker comments alone was treated as a prior install. The
   installer now validates that the bounded marker block exactly matches its
   bundled template, otherwise failing before either project file changes.

## Inherited evidence (reported by the prior worker; not treated as fresh proof)

| Reported command/result | Status in this audit |
|---|---|
| Initial Task 21 suite: 9 RED because new adapter/installer modules were absent | Historical RED evidence only |
| Adversarial cases: 4 RED | Historical RED evidence only |
| Original focused suite: 14 GREEN | Historical evidence only |
| Original adversarial subset: 9 GREEN | Historical evidence only |
| Task 20 boundary collection: 52, final summary lost | Replaced by a fresh 43-test Task 20 run below |
| Full collection: 545, background runner lost | Replaced by a fresh foreground 547-test run below |

## Fresh verification

| Command | Outcome |
|---|---|
| `python -m pytest tests/integration/test_install_claude.py::test_install_claude_preserves_existing_rule_bytes_when_appending_madi_block -v` before the newline fix | Expected RED: original CRLF byte prefix was normalized to LF |
| Same command after the newline fix | 1 passed |
| `python -m pytest tests/integration/test_install_claude.py::test_install_claude_refuses_unrecognized_complete_madi_marker_block -v` before the marker validation fix | Expected RED: installer did not raise `InstallError` |
| `python -m pytest tests/integration/test_install_claude.py -v` | 11 passed |
| `python -m pytest tests/adapters/test_claude.py tests/integration/test_install_claude.py -v` | 17 passed |
| `python -m pytest tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -q` | 43 passed in 35.10s |
| `python -m pytest -q --basetemp 'D:\\Temp\\mneme-task21-full-20260915-001'` | Excluded evidence: invalid base parent caused 395 pytest setup errors before test bodies; 152 tests happened to pass |
| `python -m pytest -q --basetemp 'C:\\Temp\\Temp\\mneme-task21-full-20260915-002'` | 547 passed in 1156.95s (19:16), exit 0 |
| `git diff --check` | Clean; no whitespace errors after removing two inherited trailing blank lines |

The final full run remained in a retained foreground terminal until its exit code
was collected. No Python process or Task 21 test log remains in the worktree.

## Requirement matrix

| Requirement | Evidence |
|---|---|
| Closed, non-blocking adapter failures | `AdapterResult` forces `block_host=False`; adapter tests cover unavailable service and closed warnings without raw error disclosure. |
| Bare lifecycle does not checkpoint | `test_bare_precompact_observes_lifecycle_and_requires_checkpoint_without_writing`; Task 20 `CoreService.observe` boundary tests. |
| Host owns semantic checkpoint content | Adapter admits only a host-composed, schema-checked checkpoint and calls `create_session_revision`; native transcript fields are rejected. |
| Exact v1 envelope and Core boundary | `AdapterEnvelope` accepts only `{version,event,adapter,session_id,workstream_id,checkpoint_file?}` and validates through Task 20 contracts. |
| No path/secret leakage or path shortcut | Closed warnings/results omit checkpoint paths and service errors; checkpoint paths are constrained to safe project/temp roots; installer templates contain no Vault path, credential, or HTTP endpoint. |
| Idempotent opt-in installation | Installer tests preserve existing MCP server values and Claude rules, add the Madi server/rules once, reject malformed/conflicting config, preserve CRLF rule bytes, and fail closed on a spoofed marker block. |
| Agent-neutral Core and Task 20 compatibility | Task 20 contract/stdio suite: 43 passed; adapter depends only on Core contracts/service surface. |
| Legacy integration preserved | No change to `integration/install.py`, legacy server, database, Wiki, Growth, package, or repository name. |

## Files committed

- `mneme/adapters/__init__.py`
- `mneme/adapters/base.py`
- `mneme/adapters/claude.py`
- `integration/madi/claude/CLAUDE.md.template`
- `integration/madi/claude/mcp-stdio.json`
- `integration/madi/install_claude.py`
- `tests/adapters/test_claude.py`
- `tests/integration/test_install_claude.py`
- `task-21-report.md`

## Commit and concerns

Commit message: `feat: add opt-in non-blocking Claude adapter`.

No unresolved Task 21 concern remains. The invalid first full-suite invocation is
documented above and intentionally not used as test evidence; the replacement
foreground invocation is the definitive result.

## Round 1: checkpoint privacy and file-confinement hardening

### Findings and root cause

Two review findings were reproduced from the clean `a39780d` baseline. A
detectable `API_KEY=SENSITIVE_SENTINEL` in an otherwise valid checkpoint body
passed schema and policy validation, then persisted through the Claude adapter,
direct `CoreService.execute`, and `madi_checkpoint`. Session body validation had
no deterministic credential guard before `SessionStore.create_revision` wrote the
immutable artifact and advanced its head.

The adapter also treated the entire system temporary directory as its default
checkpoint trust root. Its path checks therefore accepted a sibling temporary
project. It read a validated path by name, which also permitted a hard-link alias
inside the project to outside content.

### Red-green evidence

| Command/result | Outcome |
|---|---|
| New adapter/Core/stdio/confinement regressions against `a39780d` | Expected RED: 6 failures. Secret checkpoint writes succeeded for adapter, portable Core, local-only Core, and stdio; sibling system-temp and hard-link aliases also succeeded. One stdio fixture setup error was corrected before its own RED assertion was re-run. |
| Corrected stdio secret regression alone | Expected RED: checkpoint result was successful. |
| Secret regressions after the `SessionStore` guard | 5 passed: adapter, portable and local-only Core, stdio, plus a non-secret mention of credential detection. |
| Confinement regressions after explicit-root/descriptor validation | 5 passed: project-root positive, configured temporary-root positive, configured-outside rejection, system-temp sibling rejection, and hard-link rejection. |
| Adapter/installer plus Task 20 contracts/stdio | 68 passed in 37.63s. |
| Task 19 policy-staleness, Session, and storage privacy boundaries | 94 passed in 141.24s. |
| Foreground full suite: `python -m pytest -q --basetemp '.pytest-task21-r1-full'` | 555 passed in 1097.92s (18:17), exit 0. |

### Implementation and review

`SessionStore.create_revision` now rejects bounded, explicit detectable
credential/token/Bearer/private-key patterns across the durable semantic Session
fields before policy admission, artifact creation, registry generation change, or
head update. It applies to both portable and local-only Session storage; it does
not use an LLM or scan a raw transcript, and a descriptive sentence about the
credential detector remains valid.

`ClaudeAdapter` now defaults to the explicitly supplied project root only. A
temporary checkpoint root must be explicitly configured. Its checkpoint reader
rejects links and multi-linked regular files, re-checks lexical path safety,
opens a descriptor with no-follow support where available, and compares regular
file identity, size, modification time, and link count before and after reading.
All adapter error results remain closed, non-blocking, and path/secret-free.

Round-1 files changed:

- `mneme/core/sessions.py`
- `mneme/adapters/claude.py`
- `tests/core/test_contracts.py`
- `tests/transports/test_mcp_stdio.py`
- `tests/adapters/test_claude.py`
- `task-21-report.md`

### Round-1 requirement matrix and handoff

| Requirement | Evidence |
|---|---|
| Detectable credentials never reach a Session revision | The central `SessionStore.create_revision` guard rejects them before admission or storage; adapter, direct portable/local-only Core, and stdio no-write regressions pass. |
| No broad transcript prohibition | A descriptive, non-secret reference to credential detection still creates a revision. |
| Checkpoint confinement | Project-root checkpoints remain valid; a configured temporary root is the sole optional extension. Sibling system-temp and configured-outside paths are rejected. |
| Link/race-resistant read | Symlink/reparse rejection, one-link requirement, checked descriptor read, and pre/post identity checks protect the checkpoint read; the hard-link regression passes where hard links are supported. |
| Closed failure surface | Adapter errors remain non-blocking and omit both the checkpoint path and detected credential. |

Round-1 commit message: `fix: protect checkpoint secrets and file confinement`.
No unresolved round-1 concern remains. The full-run temporary directory, including
test-created nested repositories, was removed with an exact-path `git clean -ffdx`;
no Python process or test log remains.
