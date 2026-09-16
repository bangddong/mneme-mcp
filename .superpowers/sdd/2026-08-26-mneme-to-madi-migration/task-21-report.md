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

## Round 2: complete serialized-reference and label secret guard

### Findings and root cause

Review against clean `134bedf` found that the deterministic guard only inspected
free-text Session fields. `ArtifactReference.value` is also serialized into a
Session's front matter, Markdown body, and reference manifest, but a
credential-bearing `label` value was omitted from the pre-admission scan. The
same omission applied to an `ArtifactReference` carried by relation provenance.
Thus direct portable/local Core, Claude adapter, and actual stdio checkpoints
could persist `API_KEY=SENSITIVE_SENTINEL` in `source_refs` and advance the
corresponding workstream head.

The bounded label pattern accepted underscore/hyphen spelling only. It did not
recognize common `API KEY`, `API Key`, `access key`, or `private_key` labels when
followed by an assigned secret value.

### Red-green evidence

| Command/result | Outcome |
|---|---|
| New direct Core serialized-reference and spaced-label regressions against `134bedf` | Expected RED: 12 failures. Source and relation-provenance reference values wrote portable and local-only revisions; four common spaced/private-key labels wrote in both storage classes. |
| Direct Core source-reference case with `-x -vv` | Expected RED: `CommandResult.ok` was `True` and revision `000001` was created instead of the requested closed rejection. |
| New adapter serialized-reference regressions | Expected RED: 2 failures (portable and local-only): adapter returned success and wrote a revision. |
| New actual stdio serialized-reference regressions | Expected RED: 2 failures (portable and local-only): `madi_checkpoint` returned success and wrote a revision. |
| Core serialized references, four label variants in both storage classes, and non-secret reminder | 13 passed in 12.86s. |
| Adapter secret/reference/confinement subset | 4 passed in 3.75s. |
| Stdio direct secret/reference subset | 3 passed in 5.97s. |
| Adapter, installer, Task 20 contracts, and stdio | 84 passed in 80.16s. |
| Task 19 policy-staleness, Session, and storage privacy boundaries | 94 passed in 235.39s. |
| Fresh retained logged full suite: `python -m pytest -q --basetemp .pytest-task21-r2-full` | 571 passed in 1310.81s (21:50). |

### Implementation and self-review

The central `SessionStore.create_revision` guard now enumerates every
host-controlled canonical string that reaches durable Session content: the
adapter id and semantic text, `source_refs` string values, every relation text
field, relation `ArtifactReference` provenance values, and typed provenance
kind/id values. It still runs before policy admission, artifact creation,
registry generation change, or head advancement, and therefore protects direct
Core, adapter, and stdio routes for both portable and local-only Sessions.

Its deterministic expression now recognizes space, underscore, hyphen, and
case variants for API/access/auth/client/private key labels, while retaining the
assignment-and-bounded-value requirement. PEM private-key headers remain
detectable. A non-secret sentence that reminds a host to configure a credential
continues to be accepted, so this is neither an LLM check nor a broad prose or
transcript scan. Error envelopes remain closed and do not echo the matched value.

| Requirement | Evidence |
|---|---|
| Reference-value secrets cannot persist | Source and relation provenance regressions require no artifact, generation, or head change in portable and local-only Core. Adapter and actual stdio assert the same path remains closed. |
| Common label spelling cannot evade detection | `API KEY=`, `API Key:`, `access key=`, and `private_key:` forms all reject before a write in both storage classes. |
| Non-secret reminder remains valid | The established detector-description checkpoint is retained in the 13-test green run. |
| Round-1 confinement remains intact | The adapter subset includes the existing hard-link rejection alongside new guard coverage. |

Round-2 files changed:

- `mneme/core/sessions.py`
- `tests/core/test_contracts.py`
- `tests/adapters/test_claude.py`
- `tests/transports/test_mcp_stdio.py`
- `task-21-report.md`

Round-2 commit message: `fix: scan serialized session references for secrets`.
No unresolved round-2 concern remains. Captured test logs and the exact fresh
base-temp directory are removed before the final clean-worktree audit.

## Round 3: quote-aware assigned-value guard

### Finding and correction

Review of `83b4cf7` found that the deterministic assignment pattern admitted a
quoted right-hand-side value.  For example,
`API_KEY="SENSITIVE_SENTINEL"` did not satisfy the old immediate bare-token
pattern, so it could reach the serialized Session content.  The issue applied
at the shared Core admission point and therefore affected direct Core,
Claude-adapter, and stdio checkpoint routes.

The revised pattern parses a bounded assigned value as double-quoted,
single-quoted, or bare text for the existing API/access/private-key label
variants.  It allows only an exact, whole assigned value of `REDACTED` or
`placeholder` (case-insensitive, with optional matching quotes); a placeholder
prefix followed by additional material is still rejected.  The shared
pre-admission scan continues to enumerate every host-controlled serialized
Session string: body text, reference values, relation text, and relation
provenance.  It does not inspect transcripts or infer semantics.  Its generic
error remains closed, so neither Core nor either transport echoes the matched
value, and rejection occurs before artifact creation or registry mutation.

### Red-green and verification evidence

| Command/result | Outcome |
|---|---|
| Quote-wrapped credential and placeholder regressions before the correction | Expected RED: quoted assignment values were not recognized by the previous bare-token matcher. |
| Selected Core quote/placeholder/security cases | 21 passed. |
| Selected Claude adapter quote/reference cases | 5 passed. |
| Selected stdio quote/reference cases | 4 passed. |
| `python -m pytest tests/adapters/test_claude.py tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -q` | 108 passed in 123.33s. |
| `python -m pytest tests/core/test_sessions.py tests/core/test_contracts.py tests/core/test_policy_staleness.py tests/core/test_storage.py -q` | 94 passed in 220.62s. |
| First full suite: `python -m pytest -q --basetemp .pytest-task21-r3-full` | 594 passed, then one setup-only `WinError 5` while `Vault.initialize` renamed its temporary Doctor fixture directory before `test_doctor_never_repairs_an_injected_external_index_path` executed (1197.37s). No matcher test failed. |
| Retried full suite in the same foreground process: `python -m pytest -q --basetemp .pytest-task21-r3-full-retry` | 595 passed in 1116.61s (18:36), exit 0. |

### Round-3 files and concern disposition

- `mneme/core/sessions.py`
- `tests/core/test_contracts.py`
- `tests/adapters/test_claude.py`
- `tests/transports/test_mcp_stdio.py`
- `task-21-report.md`

The first full-run error was investigated as a Windows filesystem setup flake:
the error arose in `Vault.initialize` before the Doctor test body and the exact
test passed as part of the subsequent clean full retry.  No production retry or
Doctor behavior was changed.  The retry is the definitive full-suite evidence.
Exact Task 21 round-3 pytest base-temp directories and captured logs are removed
after this report update; no Python test process remains.

## Round 4: fail closed on malformed quoted assignments

### Finding and root cause

Review of clean `6e58e44` reproduced a fourth deterministic-guard bypass.  The
assignment expression recognized a quoted value only when its matching closing
quote occurred on the same line.  Its bare-value alternative could not consume
an opening quote.  Consequently double- or single-quoted assignments that were
unterminated, ended with the other quote type, or crossed CR/LF were not rejected
at central admission.  Direct Core, Claude-adapter, and stdio calls could create
a revision; the CR cases could write the artifact and only then surface a later
registry conflict.

The correction factors the existing bounded credential-label and assignment
delimiter into one shared expression fragment.  A second bounded matcher now
recognizes an opening single or double quote that reaches CR, LF, or end of
string without the corresponding closing quote and fails closed before policy
admission or storage.  The existing matched-quote and bare-value parser remains
responsible for assigned values, including its case-insensitive allowance for
only exact whole values of `REDACTED` and `placeholder`.  No semantic inference,
transcript scanning, or error echo was added.

### Red-green and verification evidence

| Command/result | Outcome |
|---|---|
| New malformed-quote regressions against `6e58e44`: `python -m pytest tests/core/test_contracts.py::test_checkpoint_rejects_malformed_quoted_credential_assignments_before_writing tests/adapters/test_claude.py::test_adapter_rejects_malformed_quoted_credential_without_writing tests/transports/test_mcp_stdio.py::test_madi_checkpoint_rejects_malformed_quoted_credential_without_writing -q` | Expected RED: 18 failed in 33.97s. Both storage classes and all six Core forms bypassed; representative Claude and stdio forms also bypassed. |
| Same command after the central matcher correction | 18 passed in 17.54s. |
| Core/adaptor/stdio secret, reference, quote, placeholder, and prose slices | 68 passed (52 Core, 8 Claude, 8 stdio). |
| `python -m pytest tests/adapters/test_claude.py tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -q` | 120 passed in 111.17s. |
| `python -m pytest tests/core/test_sessions.py tests/core/test_policy_staleness.py tests/core/test_storage.py -q` | 94 passed in 197.61s. |
| `python -m pytest -q --basetemp .pytest-task21-r4-full` | 618 passed in 1334.04s (22:14), exit 0. |
| `git diff --check` | Clean; no whitespace errors. |

### Self-review and requirement matrix

| Requirement | Evidence |
|---|---|
| Unterminated and mismatched single/double quotes fail closed | Direct Core covers both quote types and both mismatch directions in portable and local-only storage; Claude and stdio cover representative forms. |
| CR/LF-split quoted values fail closed | Direct Core covers isolated CR and LF in both storage classes; Claude covers LF and stdio covers CR. |
| Matching quotes and placeholders retain their bounded behavior | Existing matching-quote cases remain green. Exact quoted and unquoted placeholders remain accepted; new cases reject extra secret material before or after a placeholder token. |
| Normal prose is not semantically scanned | Two descriptive credential-detector statements without an assignment remain accepted. |
| Every serialized Session string stays guarded centrally | `_reject_detectable_secrets` retains the complete body, source-reference, relation, and relation-provenance traversal and invokes the corrected matcher for each string. |
| Rejection is atomic and closed | Tests assert `invalid-artifact`, no secret/path echo, no revision artifact, registry generation zero, and no active head through Core and representative transports. |

Mutation review confirmed that removing either malformed quote branch, either
line boundary, or the central call is caught by the new regressions.  The
existing tests catch changes to bounded labels, matching quotes, placeholder
whole-value handling, serialized-reference traversal, closed errors, and
pre-write ordering.

Round-4 files changed:

- `mneme/core/sessions.py`
- `tests/core/test_contracts.py`
- `tests/adapters/test_claude.py`
- `tests/transports/test_mcp_stdio.py`
- `task-21-report.md`

Round-4 commit message: `fix: fail closed on malformed quoted credentials`.
No unresolved round-4 concern remains.  The exact full-suite base-temp directory
is removed after verification; no test process or captured test log remains.

## Round 5: validate the complete credential assignment remainder

### Finding and root cause

Review of clean `7982107` found that the quote-aware expressions could accept a
valid first quoted fragment without validating the remainder of the assignment.
Triple quotes, an empty or short quoted fragment followed by secret material,
nested opposite quotes, adjacent or repeated quoted fragments, and an exact
placeholder followed by another token could therefore evade the deterministic
guard.  Because the shared guard is the admission boundary for direct Core,
Claude-adapter, and stdio requests, the bypass affected both portable and
local-only Session writes.

The correction retains the common bounded credential-label prefix, then parses
the complete trimmed right-hand-side remainder in one pass.  A quoted remainder
must contain exactly one matching outer pair, contain no opposite quote inside,
and consume the whole remainder.  An unquoted remainder containing either quote
fails closed.  CR/LF in the matched assignment prefix or non-empty remainder also
fails closed.  Only the exact whole values `REDACTED` and `placeholder`, quoted or
unquoted and case-insensitive, receive the existing reminder allowance; prefixes,
suffixes, extra quotes, and following tokens do not.  Assignment remainders over
256 KiB reject immediately, and all remaining operations are bounded linear
scans.  The independent Bearer/private-key matcher and the complete serialized
Session traversal are unchanged.

### TDD and verification evidence

The interrupted round-5 worker supplied the following retained TDD evidence,
which was audited but is not represented as a fresh rerun: the initial focused
regressions produced the expected 29/29 RED failures against `7982107`; the new
Core slice then passed 29 tests, and the expanded Core/Claude/stdio slice passed
35 after adding the CRLF-prefix case and representative transport coverage.

Fresh verification in the replacement session:

| Command/result | Outcome |
|---|---|
| `python -m pytest --collect-only -qq` | Exit 0; per-file collection counts total 653. |
| `python -m pytest tests/adapters/test_claude.py tests/core/test_contracts.py tests/transports/test_mcp_stdio.py -q --basetemp .pytest-task21-r5-focused` | 155 passed in 154.05s. |
| `python -m pytest tests/core/test_sessions.py tests/core/test_policy_staleness.py tests/core/test_storage.py -q --basetemp .pytest-task21-r5-privacy` | 94 passed in 216.83s. |
| Full split A: Core search/filesystem, contracts, current overlay, doctor, fs, and Git sync, using `.pytest-task21-r5-split-a` | 221 passed in 1094.37s. |
| Full split B: Core memory, policy, profile, recall, registry, reindex, resolver, Session, source, and Wiki-validation files, using `.pytest-task21-r5-split-b` | 208 passed in 399.08s. |
| Full split C: adapter, characterization, storage, Vault, installer, migration, provider, CLI/top-level, and transport files, using `.pytest-task21-r5-split-c` | 224 passed in 129.19s. |

The three explicit full-suite partitions are non-overlapping and sum to the fresh
collection exactly: `221 + 208 + 224 = 653`.  All three retained processes
returned exit 0, so the split is the definitive full-suite result rather than an
inference from the earlier interrupted run.

### Self-review and requirement matrix

| Requirement | Evidence |
|---|---|
| Triple, empty-prefix, nested, adjacent, repeated, and malformed quotes cannot hide later material | Direct Core covers both storage classes; Claude and stdio cover representative complete-remainder failures. |
| Placeholder allowance is exact and whole | Existing quoted/unquoted placeholder positives remain green; prefix, suffix, extra-quote, and token-continuation forms reject. |
| CR/LF continuations fail closed | Existing split-value cases remain green, and the credential-prefix CRLF-before-delimiter regression rejects in both storage classes. |
| Parsing is bounded and linear | The 256 KiB adversarial regression passes; the implementation caps before quote scans and uses no backtracking expression for the remainder. |
| Normal prose remains valid | Existing non-assignment descriptions of both the API-key detector and quoted parser still create revisions. |
| Rejection is central, closed, and atomic | The guard still runs before policy admission and artifact/head mutation, traverses body/reference/relation/provenance strings, and Core/adapter/stdio tests assert generic errors with no artifact or registry change. |

Round-5 production/test files changed:

- `mneme/core/sessions.py`
- `tests/core/test_contracts.py`
- `tests/adapters/test_claude.py`
- `tests/transports/test_mcp_stdio.py`

Round-5 commit message: `fix: validate complete credential assignment remainder`.
No unresolved round-5 concern remains.
