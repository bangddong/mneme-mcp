# Madi D3: Portable Vault Information Architecture

**Status:** Approved — authoritative D3 specification

**Date:** 2026-08-26

**Decision scope:** Durable information architecture for a personal Madi Vault

**Implementation status:** Not implemented

## 1. Decision Summary

Madi is a person-owned continuity layer shared by that person's agents. It is not
the authoritative store for a project's facts, an agent transcript archive, or a
second semantic brain.

This design makes three families of portable artifacts authoritative:

1. **Registry artifacts** declare identities, relationships, policies, and the
   active heads that a resolver is allowed to use.
2. **Session revisions** are immutable, bounded snapshots of working continuity.
   Creating a revision is the durable checkpoint operation.
3. **Memory records** hold candidates and accepted personal memories. Candidate
   content is mutable; accepted semantics are immutable and superseded by a new
   record.

`CURRENT`, `PROFILE`, search indexes, SQLite databases, vector data, and runtime
logs are generated local state. Git history adds transport and audit history, but
no artifact depends on Git history for its semantic existence.

The portable Vault and machine-local state may describe the same logical
workstream. The relationship is strictly one-way: local artifacts may refer to
portable artifacts, while portable artifacts must not reveal a local-only
artifact, confidential source, or local overlay's existence.

## 2. Product Boundaries

### 2.1 Ownership

The ownership unit is the person. One person owns one private portable Madi Vault.
Projects, agents, models, and machines are sources or consumers connected to it.

### 2.2 Sources of truth

| Store | Authoritative for | Not authoritative for |
|---|---|---|
| Project repository | Source code, ADRs, runbooks, official decisions, team conventions, legitimate project status | A person's cross-project learning or private work continuity |
| Portable Madi Vault | Sanitized personal continuity, personal preferences, reusable lessons, personal decisions, workstream checkpoints, references to project truth | Project facts merely copied from the project repository, raw transcripts, raw evidence |
| Machine-local Madi state | Confidential/local continuity, raw evidence, source path bindings, indexes, caches, generated views | Portable continuity on another machine |

An official project decision remains in the project repository. Madi may contain
a reference to it, a personal consequence of it, or a cross-project lesson derived
under the applicable source policy. A personal workstream state is not an official
project status even when it is project-scoped.

### 2.3 Core boundary

Madi Core must work without a generation LLM and without an always-on daemon.
Core provides deterministic schema validation, storage, source registration,
indexing, retrieval, context assembly, checkpoint mechanics, repair, and sync
primitives. Host agents perform semantic selection, summarization, and conflict
interpretation. Optional embeddings improve retrieval but do not become a durable
source of truth.

Claude, Codex, and future native payloads terminate at their adapters. Core accepts
only the agent-neutral lifecycle and command contracts in this specification.
Growth Lab is optional and its failure cannot break Core.

## 3. Terminology

| Term | Meaning |
|---|---|
| Vault | The private Git repository containing portable Markdown and registry metadata |
| Registry | Portable metadata that explicitly declares identities, policies, sources, workstreams, and active heads |
| Workstream | A person-owned thread of work; it can reference a project but is not project truth |
| Session | One agent execution lineage within a workstream |
| Session revision | An immutable, self-contained working snapshot; also the checkpoint artifact |
| Head | A registry reference to a specific session revision eligible for current-context resolution |
| Memory | A candidate or accepted personal record with independent kind, status, scope, authority, and portability axes |
| Portable | Allowed to enter the private Madi Git Vault under an evaluated policy ceiling |
| Local-only | Stored outside the Vault and excluded from Git, portable views, and portable references |
| Overlay | Local-only state that extends a portable workstream without being visible from portable artifacts |

## 4. Repository and Local-State Layout

### 4.1 Portable private Vault

```text
my-madi/
├── README.md
├── .gitignore
├── .madi/
│   ├── schema-version
│   ├── vault.yaml
│   ├── policy-index.yaml
│   └── policies/
│       └── <policy-id>/
│           └── <revision>.yaml
├── projects/
│   └── <project-id>.yaml
├── sources/
│   └── <source-id>.yaml
├── workstreams/
│   └── <workstream-id>/
│       ├── workstream.yaml
│       └── sessions/
│           └── <session-id>/
│               ├── 000001.md
│               ├── 000002.md
│               └── ...
└── memory/
    └── <memory-id>.md
```

The current tree, not prior commits, contains every session revision and every
accepted memory semantic version needed to reconstruct portable continuity.

### 4.2 Machine-local state

Machine-local state lives outside the Vault checkout under the platform's Madi
state directory. It must not rely on an ignored directory inside the Vault as its
only safety boundary.

```text
<MADI_STATE_HOME>/vaults/<vault-id>/
├── config.toml
├── bindings/
│   └── sources.yaml
├── overlays/
│   ├── .madi/
│   │   ├── policy-index.yaml
│   │   └── policies/<policy-id>/<revision>.yaml
│   ├── projects/<project-id>.yaml
│   ├── sources/<source-id>.yaml
│   ├── workstreams/
│   │   └── <local-workstream-id>/
│   │       ├── workstream.yaml
│   │       └── sessions/<session-id>/<revision>.md
│   └── memory/<memory-id>.md
├── evidence/
├── pending/
├── views/
│   ├── CURRENT.md
│   └── PROFILE.md
├── index/
│   ├── state.db
│   └── vectors/
├── cache/
├── locks/
└── logs/
```

`MADI_STATE_HOME` is machine configuration, not a path recorded in the Vault.
Credentials and provider selection also belong only in machine configuration or
the operating system's secret store.

## 5. Artifact Ownership and Mutation Rules

| Artifact | Authoritative ownership | Storage class | Write actor | Conflict strategy |
|---|---|---|---|---|
| `README.md` | Human-facing Vault explanation only | Mutable, non-canonical documentation | Human/init or migration command | Normal reviewed text merge |
| `.gitignore` | Defense-in-depth exclusions, not the primary privacy boundary | Mutable repository configuration | Init/admin command | Review conflicting rule changes |
| `.madi/schema-version` | Vault format version | Small bounded mutable registry | Migration command | Exact version check; no semantic merge |
| `.madi/vault.yaml` | Vault identity and portable defaults | Small bounded mutable registry | Explicit admin/config command | Generation/CAS; conflicting identity or policy changes require review |
| `.madi/policy-index.yaml` | Active policy revision references | Small bounded mutable registry | Authorized policy command | Generation/CAS; never last-write-wins |
| `.madi/policies/<id>/<revision>.yaml` | Policy semantics at a named revision | Immutable registry revision | Authorized policy command | New revision only; filename/content collision is an error |
| `projects/<id>.yaml` | Personal registration of a project, not project truth | Small bounded mutable registry | Project registration command | Generation/CAS; identity conflict requires review |
| `sources/<id>.yaml` | Portable logical source identity, type, policy reference, and safe locator | Small bounded mutable registry | Source registration command | Generation/CAS; unsafe locator rejected |
| `workstreams/<id>/workstream.yaml` | Workstream lifecycle, active heads, resolution mode, preferred head | Small bounded mutable registry | Workstream/head commands | Generation/CAS; disjoint head additions may structurally merge, preferred-head conflicts do not |
| `sessions/<session>/<revision>.md` | Working continuity at that checkpoint | Immutable after creation | Host agent through checkpoint command | Writer-owned lineage; add/add or expected-parent conflict fails explicitly |
| `memory/<id>.md`, candidate | Sanitized semantic candidate | Mutable with revision field | Host agent or human through remember/edit commands | Generation/CAS; body conflicts require explicit choice |
| `memory/<id>.md`, accepted | Accepted semantic version and its original policy evaluation | Semantic body immutable; bounded lifecycle envelope mutable | Promote, retire, or supersede commands | Semantic change creates a new ID; lifecycle change uses CAS |
| Local overlay artifacts | Confidential or machine-specific continuity | Same rules as corresponding portable artifact, but local-only | Local Core instance/host agent | Local CAS and locks; never resolved by portable Git merge |
| Local `config.toml` and source bindings | Machine/provider selection, credentials references, and absolute mount paths | Mutable local configuration | Human/local setup command | Machine-local; never Git merged |
| `CURRENT.md`, `PROFILE.md` | Nothing; projections only | Generated local state | Deterministic resolver | Regenerate; never merge or commit |
| SQLite/FTS/vector indexes | Nothing; acceleration only | Generated local state | Indexer | Delete and rebuild |
| Evidence, cache, locks, logs | Runtime support only | Local/transient | Core runtime | Rotate/delete locally; never sync as memory |

All portable writers validate schema, privacy policy, expected registry generation,
and artifact path before an atomic write. Direct file edits remain possible because
the Vault is human-owned, but `madi doctor` reports violated invariants rather than
silently repairing semantic conflicts.

## 6. Registry Model

### 6.1 Project and source registries

A project registry gives a stable personal identifier to a project and relates it
to workstreams and sources. It may store a portable-safe repository identity, such
as an explicitly approved remote URL or opaque fingerprint. It must not store an
absolute machine path, credentials, or project documentation copied as Madi truth.

A source registry describes a logical, read-only source such as project Markdown.
Machine-local `bindings/sources.yaml` maps its ID to a checkout path. A missing
binding makes the source unavailable; it does not mutate the portable source
registry.

Source registration is not ingestion. Retrieval may index mounted project files,
but a result retains source provenance and project authority. The generated index
can always be rebuilt from the Vault and currently available mounts.

### 6.2 Workstream registry

Each `workstream.yaml` declares, rather than infers, its usable heads:

```yaml
id: ws-example
generation: 12
project: project-example       # optional
status: active                 # active | paused | closed
resolution:
  mode: preferred              # single | preferred | parallel
  preferred_head:
    session: ses-codex-01
    revision: 000004
active_heads:
  - session: ses-claude-01
    revision: 000007
  - session: ses-codex-01
    revision: 000004
```

The registry does not summarize session bodies. A checkpoint writer first creates
an immutable revision and then updates this registry using an expected generation.
An interruption between the two writes can leave an orphan revision, which
`doctor` reports but never attaches automatically.

### 6.3 Resolution states

Resolution state is computed from an explicit registry; it is not itself canonical.

| State | Meaning | Resolver behavior |
|---|---|---|
| `resolved` | The registry is valid and selects one head, or selects a preferred head while retaining valid alternatives | Render the selected head and list alternatives without merging them |
| `divergent` | `mode: parallel` intentionally declares multiple valid heads and no preferred head | Render a deterministic multi-head view; make no canonical-head claim |
| `degraded` | Canonical state remains safe and usable, but optional/local inputs such as a mount, index, overlay, or provenance predecessor are unavailable | Render the valid subset with explicit diagnostics |
| `invalid` | Schema, policy, lineage, or required-reference invariants fail; for example, an active head is missing or a portable artifact points local | Refuse a normal CURRENT for that workstream and require repair or explicit head selection |

Absence of `preferred_head` is valid for `single` with exactly one active head,
`parallel` with one or more heads, and paused/closed workstreams with no active
head. It is invalid only when the declared resolution mode requires a preferred
head or the registry is otherwise contradictory.

Multiple heads are therefore not corruption by themselves. Undeclared ambiguity,
an invalid head, or two writers mutating the same session lineage is corruption.

## 7. Portable and Local-Only Overlays

The same logical workstream may have portable state and a local-only overlay.
A local overlay declares `overlay_of: <portable-workstream-id>` and may name a
portable base head. It can add confidential sessions, memories, or a local
preferred head. The local record is the only place where this relationship is
stored.

The following rules are mandatory:

1. A local artifact may reference a portable artifact.
2. A portable artifact may not reference a local ID, path, count, hash, label, or
   the fact that an overlay or confidential scope exists.
3. A confidential-only workstream has only a local ID; Madi creates no portable
   placeholder for it.
4. A local overlay may reduce effective portability but cannot relax the portable
   base or source policy.
5. Promotion from a local artifact creates a newly evaluated portable artifact.
   It includes only portable-safe provenance. The full local derivation link stays
   local.
6. A project's or source's identity is itself local-only when policy forbids
   disclosing its existence. In that case its registry and policy revisions live
   under the local overlay tree; `projects/` and `sources/` contain no stub.

### 7.1 Exact CURRENT composition

`CURRENT` is always generated under `<MADI_STATE_HOME>` and has two explicit modes.

**Portable projection:**

1. Load and validate portable vault, policy, project, source, and workstream
   registries.
2. Resolve only the active heads named by the portable workstream registry.
3. Load only portable session revisions and accepted portable memories allowed by
   the requested scope.
4. Render `resolved`, `divergent`, `degraded`, or `invalid` status. Never consult
   local overlay metadata, even to report that it exists.

**Effective-local projection:**

1. Build the portable projection unchanged.
2. Load only locally authorized overlays for the selected local invocation.
3. Validate one-way references and apply the overlay's own registry resolution.
4. Present the portable projection as a labelled base and the local projection as
   a labelled overlay. Do not synthesize their prose into a new canonical fact.
5. If an overlay selects a local preferred head, use it as the foreground for this
   local view only. Preserve the portable selected or parallel heads as its base.
6. Propagate the stricter resolution/security result and show local diagnostics
   only in the effective-local view.

The effective-local view reports `portable_status`, `overlay_status`, and an
`effective_status` rather than hiding one layer's condition. The effective state is
computed in this order:

1. An invalid portable base makes the effective view `invalid`; a local overlay
   cannot repair portable canonical state.
2. An absent or invalid optional overlay leaves the valid portable base usable but
   makes the effective view `degraded`; invalid local content is omitted.
3. A valid foreground layer with intentional parallel heads makes the effective
   view `divergent`. An overlay may explicitly anchor itself to one portable head,
   but other portable heads remain visible as alternatives.
4. Any unavailable optional source or index makes the effective view `degraded`
   unless a prior rule already made it `invalid`.
5. Otherwise the effective view is `resolved`. A closed workstream with no active
   heads is a valid resolved empty state.

If no workstream is named, the resolver uses an unambiguous machine-local project
binding or a sole active workstream. Otherwise it renders a deterministic overview
and requests an explicit selection; it does not guess a global canonical head.

No generated CURRENT is committed, regardless of projection mode.

## 8. Session Revisions, Checkpoints, and Handoffs

### 8.1 Session revision

A session is a writer-owned linear lineage within a workstream. Each revision is
an immutable, bounded Markdown snapshot containing enough semantic state to resume
without replaying prior chat or Git history. It includes at least:

- session, workstream, agent adapter, and revision identity;
- timestamp and optional predecessor/continuation references;
- objective and current state;
- verified decisions or facts needed for continuation;
- completed work and verification evidence at a portable semantic level;
- blockers, risks, and concrete next actions;
- source references without raw confidential content;
- handoff relations emitted at this boundary;
- policy evaluation metadata.

A predecessor reference improves provenance but is not required to reconstruct the
revision body. Shallow clones, squashed history, repository exports, and Git
history rewrites therefore retain checkpoint semantics as long as the current
files are present.

### 8.2 Checkpoint operation

`checkpoint` means:

1. The host agent selects and sanitizes meaningful working state.
2. Core evaluates policy and validates the revision.
3. Core atomically writes the next immutable session revision.
4. Core updates the workstream's active head using registry CAS.
5. Core returns a domain event or an explicit failure.

Git commit, push, pull, and automatic sync are separate policies. A successful
checkpoint guarantees a valid artifact in the local Vault working tree; another
machine receives it only after the chosen Git commit and sync workflow completes.
There is no D3 default such as `commit_on_checkpoint=true`.

### 8.3 Handoff relation

Handoff is an append-oriented relation inside the revision that records source
session/revision, intended recipient or capability, purpose, required context, and
open next action. It is not a separate artifact and does not change the source
session to a terminal `handed-off` status.

The original agent may resume with another revision. One revision may name several
recipients, and a recipient starts or continues its own session with a
`continues_from` relation. Completion or cancellation is expressed in a later
revision, preserving the original handoff event.

## 9. Memory Model

### 9.1 Independent axes

Each memory record has independent fields:

| Axis | Initial values | Purpose |
|---|---|---|
| `kind` | `decision`, `lesson`, `preference`, `knowledge` | What semantic role the memory plays |
| `status` | `candidate`, `accepted`, `retired` | Governance lifecycle |
| `scope` | personal-global or project/workstream-bound references | Where it is relevant |
| `authority` | `personal`, `external-reference` | Whether it is personal knowledge or a reference to another source of truth |
| `portability` | `local-only`, `personal-vault`, future explicitly approved scopes | Where policy permits storage |
| `provenance` | source references and policy evaluation | Why it exists and under which ceiling it was admitted |

These axes must not imply one another. For example, an accepted decision can
remain local-only; a portable candidate is not accepted; a project-scoped lesson
is still personally authoritative unless promoted to the project repository.

`knowledge` is retained for a verified durable fact, technique, or explanation
that is neither a decision, a generalized lesson, nor a preference. It is not a
license to duplicate project documentation: if the source is readily retrievable
project truth, Madi stores a reference or personal consequence instead.

### 9.2 Candidate and acceptance lifecycle

```text
raw evidence (local only)
    -> sanitized semantic candidate
       -> local-only candidate, or
       -> policy-approved portable candidate
          -> accepted memory, or
          -> retired candidate
```

Candidate content is editable with CAS. Before a candidate enters Git, Core must
validate its content, metadata disclosure, source policy inheritance, and effective
portability ceiling. Being a candidate neither requires nor prohibits portability.

Promotion freezes the accepted record's semantic fields and semantic hash.
Subsequent correction, reinterpretation, scope/authority change, or material body
change creates a new Memory ID with `supersedes: <old-id>`. The old accepted body
remains in the current tree. A reverse `superseded_by` link is derived from the new
record and is not required to mutate the old record.

Only the bounded lifecycle envelope, such as `status: retired`, `retired_at`, and
retirement reason, may change on an accepted record. It uses CAS and cannot alter
the semantic hash. Thus prior accepted semantics survive shallow clones and
history rewrites without relying on Git history.

Authorized privacy or legal remediation is the sole exception to retaining an
accepted file in the current tree. It may physically remove the record and, only
when safe, leave a non-sensitive tombstone. This is deletion, not semantic
overwrite, and it still cannot guarantee erasure from prior Git propagation.

### 9.3 Generated PROFILE

`PROFILE.md` is not canonical. It is deterministically generated from applicable
accepted, non-retired `preference` memories, with source IDs and policy filters.
Conflicting preferences are displayed as conflicts or resolved by explicit
supersession; the generator does not choose semantically.

## 10. Privacy, Portability, and Policy Time

### 10.1 Portability ceiling

Every potential portable write computes an effective ceiling from all applicable
inputs:

```text
effective ceiling = most restrictive of
    vault policy,
    project policy,
    source policy,
    input/evidence classification,
    explicit user restriction
```

The agent may reduce portability but may not increase it beyond the inherited
ceiling. Sanitization alone does not declassify a source. A policy may explicitly
permit sanitized derivatives; otherwise an authorized human or source-policy
authority must change or grant the applicable classification before portability
can rise.

Multiple sources inherit the most restrictive ceiling. Unknown or unavailable
source policy fails closed for portable writes. Policy evaluation applies both to
the semantic body and to metadata: project names, source IDs, URLs, hashes, and the
existence of a confidential workstream may themselves be non-portable.

### 10.2 Policy evaluation provenance

Each portable session revision and memory record stores a portable-safe evaluation
receipt containing:

- evaluation time and evaluator/schema version;
- requested portability and effective ceiling;
- allow/deny result for the written artifact;
- exact immutable vault/project/source policy revision references or portable-safe
  opaque attestations;
- content semantic hash;
- any approved declassification or sanitized-derivative rule identifier.

Full source/evidence mappings remain local when their disclosure is not portable.
A portable receipt must not leak their IDs, count, hashes, or existence. Policy
revision files are immutable, and current policy pointers are registry state, so a
decision can be audited from the current tree without Git history.

A policy revision referenced by any current-tree artifact cannot be garbage
collected merely because it is no longer the active revision. It remains in the
current tree until every dependent artifact is removed through an authorized
retention/remediation decision.

### 10.3 Later policy tightening

Madi cannot guarantee retroactive deletion of information already copied into Git
commits, reflogs, clones, forks, mirrors, backups, caches, or third-party storage.
Person ownership does not create technical authority over every propagated copy.
This is a product invariant, not merely an operational caveat.

When a current source or project policy becomes stricter, `doctor`, `context`,
`recall`, and sync preflight re-evaluate affected current-tree artifacts against
the new policy pointer. Non-compliant or stale artifacts are:

1. excluded from generated CURRENT, PROFILE, recall, and export by default;
2. blocked from further automated commit/push/sync;
3. reported in a local-only remediation report without exposing confidential
   details in the Vault;
4. remediated, under human authority, by retirement, a sanitized successor,
   credential rotation/revocation, and—where required—coordinated Git history
   rewrite and clone/backup handling.

History rewrite may reduce future exposure but is never described as guaranteed
erasure. The artifact's original admission receipt remains evidence of the policy
revision used at write time unless an authorized remediation deliberately removes
it from the current tree.

### 10.4 Data classes

| Input encountered by an agent | Default location | Portable form, if policy permits |
|---|---|---|
| Credentials, tokens, secret values | Never memory; secret store or discard | None; only a non-secret reminder to configure a credential |
| Raw customer/production data | Local transient evidence, minimised and expired | Only authorized, non-identifying generalized lesson under source policy |
| Raw operational logs | Local transient evidence | Verified root cause or reusable remedy without sensitive payloads |
| Internal URL or resource name | Local-only unless its existence is approved | Approved opaque source reference or no reference |
| Source snippet | Project repository/source mount or local evidence | Personal lesson/reference; snippet only with explicit compatible license and policy |
| Raw transcript/tool output | Local transient evidence or not stored | Sanitized semantic checkpoint/candidate only |

Secrets are rejected by deterministic guards where detectable. Passing a guard is
not proof of safety; the host agent and user remain responsible for semantic
selection, while Core enforces the declared ceilings and known patterns.

## 11. Concurrency and Git Conflict Policy

Different agents normally write different session directories. Session IDs must
be globally collision-resistant and include no confidential names. A session has
one logical writer at a time. Local locks protect a process; expected predecessor
and registry generation protect cross-process and cross-machine updates.

If two writers produce the same next revision of one session, Madi does not choose
last-write-wins. The second write fails or Git reports add/add. Recovery creates a
new session lineage or an explicit continuation revision after human/host-agent
selection.

Workstream registries are the main mutable conflict surface:

- additions of distinct valid session heads may be structurally merged when the
  common registry generation and unchanged fields are provable;
- removal of a head, lifecycle changes, policy changes, and competing
  `preferred_head` updates require explicit resolution;
- semantic bodies are never auto-merged;
- orphan revisions are reported, not auto-promoted to heads;
- accepted memory changes use new IDs and supersession rather than overwrite.

Git sync first fetches and validates. It never resolves semantic conflicts with
blind merge or force push. Checkpoint persistence, commit batching, push cadence,
pull/rebase/merge preference, and offline behavior are a separate sync-policy
decision and machine configuration concern.

## 12. Agent-Neutral Interaction Contract

Four vocabularies have distinct responsibilities.

| Vocabulary | Meaning | Examples | May mutate canonical state? |
|---|---|---|---|
| Lifecycle event | Adapter observation about a host-agent boundary | `session_started`, `session_resumed`, `pre_compact`, `milestone_reached`, `session_ended`, `agent_switched` | No |
| Core command | Explicit requested action with validated payload and expected versions | `get_context`, `open_session`, `create_session_revision`, `submit_memory`, `promote_memory`, `retire_memory`, `set_active_heads`, `set_preferred_head`, `register_source` | Yes, after validation |
| Domain event | Fact returned after a successful domain state transition | `session_revision_created`, `memory_accepted`, `handoff_recorded`, `preferred_head_changed` | It reports a mutation; it does not request one |
| Operational event | Runtime/infrastructure observation | `index_rebuilt`, `source_unavailable`, `sync_failed`, `policy_reevaluation_failed`, `doctor_issue_found` | No domain mutation; local logs/metrics only by default |

Lifecycle events can prompt an adapter or host agent to compose a command, but Core
does not infer a semantic checkpoint merely because `pre_compact` arrived. Domain
events do not require a separate portable event-log architecture: the resulting
registry, session revision, or memory record is authoritative. Operational events
must not accidentally become portable personal knowledge.

A handoff is submitted only as part of `create_session_revision`; there is no
independently mutable handoff command or artifact. `handoff_recorded` is an
additional fact returned for each relation contained in the newly created
revision.

All commands return structured success/error envelopes. Adapter failure must warn
and leave the coding agent able to continue ordinary work. A later manual
checkpoint can recover continuity.

## 13. Long-Lived Session and Compact Lifecycle

1. An adapter observes agent startup and emits `session_started` or
   `session_resumed` as a lifecycle event.
2. It requests `get_context`. Core resolves the explicit workstream registry and
   generates a bounded portable or effective-local context.
3. The host agent works normally. Raw chat and tool output are not captured.
4. At a significant decision, milestone, explicit remember request, manual
   checkpoint, or impending compaction, the host agent selects and sanitizes
   semantic state.
5. For compaction, the adapter emits `pre_compact`; the host agent composes the
   checkpoint payload; the adapter issues `create_session_revision`.
6. Core writes the immutable revision, advances the active head by CAS, and
   returns `session_revision_created`. Failure is reported but does not block the
   host's ordinary operation.
7. Compaction proceeds. The continuing agent requests context if its compacted
   state is insufficient and resumes from the named head.
8. Repeated compactions add revisions to the same session; no SessionEnd is
   required.
9. On an agent switch, the source agent may record a handoff relation in its next
   revision. The receiving adapter starts a new session with `continues_from` and
   adds its head to the same workstream. It may become preferred only through an
   explicit head command.
10. On another machine, Git first transports committed portable artifacts. Core
    rebuilds generated state, binds available sources locally, resolves registry
    heads, and starts another session. Local-only overlays from the first machine
    are intentionally absent.

Codex adapters should use an actual PreCompact surface when available. Claude and
other adapters use their supported lifecycle hook, wrapper threshold, milestone,
or explicit checkpoint. The normalized Core contract does not depend on the
native hook name.

### 13.1 Claude adapter

The Claude adapter translates only lifecycle surfaces actually offered by the
installed Claude environment. At startup/resume it requests bounded context. At a
supported pre-compact/stop boundary—or a manual or threshold fallback—it asks the
host Claude agent to compose the semantic revision and then issues Core commands.
`CLAUDE.md` integration is a bootloader/map to these operations, not durable memory
and not a Core schema. Hook or Core failure is surfaced as a warning and cannot
block ordinary Claude work.

### 13.2 Codex adapter

The Codex adapter translates Codex lifecycle payloads, including PreCompact where
available, to the same agent-neutral lifecycle events. The host Codex agent, not
Core, selects and sanitizes checkpoint semantics before
`create_session_revision`. `AGENTS.md` or a Madi skill explains when to request
context, remember, or checkpoint; it is procedural integration rather than Vault
truth. Switching from Claude creates or resumes a distinct Codex session linked by
`continues_from`; it does not mutate the Claude lineage.

## 14. Retrieval and Generated Context

Retrieval is progressive and inspectable:

1. Generate bounded CURRENT from explicit workstream heads.
2. Include active project references, blockers, next actions, and a small number
   of applicable accepted memories.
3. Use deterministic FTS/BM25 retrieval over portable artifacts and available
   mounted sources.
4. Return original chunks with artifact/source identity, authority, policy status,
   and provenance.
5. Use optional embeddings only as an additional retriever.

The resolver does not summarize all sessions to discover a canonical head, merge
parallel prose, or load the entire Vault at startup. A missing index triggers a
rebuild or direct bounded fallback, not memory loss.

## 15. Bootstrap and Failure Scenarios

### 15.1 Clean or shallow clone

```text
git clone [--depth N] <private-vault>
-> configure machine-local MADI_STATE_HOME and credentials/providers
-> madi doctor
-> bind available project sources
-> rebuild FTS/optional vector index
-> generate CURRENT/PROFILE from current-tree registries and artifacts
-> connect an adapter
```

Shallow history is sufficient because session revisions, accepted predecessors,
policy revisions used by current artifacts, and superseded memory bodies remain in
the current tree. A clone reconstructs the last synced portable state, not another
machine's unsynced checkpoint or local overlay.

### 15.2 Concurrent agents

Agents use distinct sessions and can safely append immutable revisions. Their
heads coexist. A declared parallel workstream is `divergent`, not corrupt. Choosing
a preferred head is an explicit, CAS-protected decision; Core never silently
promotes the most recent timestamp.

### 15.3 Missing source or generated database

A missing mount makes context `degraded` while preserving portable artifacts. A
deleted DB or vector cache is rebuilt. A malformed registry or missing active
revision is `invalid` and is not papered over by index contents.

### 15.4 Privacy tightening

Current-tree artifacts are re-evaluated and withheld from normal generated output
or further automated distribution when required. The admission receipt identifies
the historical policy basis. Madi coordinates remediation but does not promise to
erase already propagated Git objects or backups.

## 16. Future Team and Organization Compatibility

V1 creates only person-owned Vaults. `.madi/vault.yaml` nevertheless represents
the owner as a typed identity (`person`, with future `team` or `organization`)
rather than encoding `personal/` into every path. Scope, authority, status, kind,
and portability remain independent fields so a later resolver can layer separately
owned Vaults without changing the artifact path model.

Promotion across ownership boundaries is not an in-place move. A personal memory
may produce a proposal to a Team Vault or project repository; review creates a new
artifact under the destination owner's policy and records only portable-safe
provenance. The personal original remains personal. A Team/Org Vault cannot refer
to a person's local overlay, and a Personal Vault does not become authoritative
for accepted team or project truth merely by referencing it.

V1 does not define RBAC, a hosted hub, cross-vault automatic conflict resolution,
or organization retention enforcement. Those capabilities can be added around the
same registry/session/memory contracts without path migration.

## 17. Migration Constraints from Mneme

This D3 is a target architecture, not a claim that current Mneme already implements
it. Migration must remain incremental and keep the existing system runnable after
each phase.

- Existing external Wiki Markdown is first registered or mounted read-only. Import
  into Madi memory is explicit, policy-checked, and avoids duplicating project
  truth.
- Existing `state.db` is treated as mixed legacy state. Derived indexes are
  rebuildable; episodes, skills, self-model, or other potentially durable rows
  require an explicit export/classification path before deletion. Growth data may
  remain local or move to optional Growth Lab storage.
- Existing FTS, Korean normalization, deterministic lint/write guards, conflict
  protection, and useful MCP/CLI behavior are characterized before extraction.
- Existing server behavior is wrapped as one transport while direct library, CLI,
  and on-demand stdio paths are introduced. An always-on HTTP server is not a Core
  requirement.
- Existing LLM functions move behind optional provider or Growth Lab seams; Core
  recall and validation gain no generation-LLM dependency.
- Claude-specific integration becomes a thin adapter. Codex receives a peer
  adapter using the same lifecycle and command contracts.

No repository/package rename, broad storage rewrite, or deletion of legacy durable
data is part of D3 implementation until a separately reviewed migration plan and
characterization tests exist.

## 18. Testable Invariants

1. Deleting all generated indexes and views does not delete durable memory.
2. A clean or shallow clone reconstructs all synced portable session and accepted
   memory semantics from current-tree files without prior Git history.
3. No API credential or required absolute source path is stored in the Vault.
4. FTS recall and context assembly work without a generation LLM.
5. Core schemas contain no Claude- or Codex-native payload fields.
6. Project repositories remain authoritative for project truth; Madi source mounts
   are read-only and references retain external authority.
7. Raw transcript, tool output, logs, credentials, and raw customer data do not
   automatically enter portable Git.
8. Every successful checkpoint creates an immutable session revision before its
   registry head can advance.
9. Repeated compaction can resume from explicit heads without SessionEnd.
10. Madi failure does not prevent ordinary host-agent work.
11. Concurrent writers cannot silently overwrite a session, preferred head,
    accepted semantic body, or policy revision.
12. Every retrieved durable artifact exposes inspectable, policy-safe provenance
    and authority.
13. Local overlays can refer to portable state; no portable artifact can reveal
    their existence.
14. Accepted semantic changes create a new Memory ID and leave the old semantic
    body in the current tree.
15. An agent can lower but cannot raise inherited portability beyond source policy.
16. Later policy tightening blocks further automated use/distribution but does not
    claim retroactive deletion from Git history, clones, or backups.
17. Missing optional sources produce `degraded`; declared parallel heads produce
    `divergent`; structural or policy corruption produces `invalid`.
18. Growth Lab, embeddings, daemon, and provider failures do not break Core.

## 19. Consequences and Trade-offs

The design intentionally accepts more small files in exchange for independent
session writers, durable checkpoints outside Git history, and immutable accepted
semantics. Registry files become the bounded conflict hot spots, so their fields
are narrow, generation-checked, and never used as prose stores.

Explicit heads avoid heuristic CURRENT selection but require agents or humans to
maintain registry intent. `divergent` makes deliberate parallel work honest rather
than forcing a false winner. Generated CURRENT avoids another canonical mutable
summary but means bootstrap must run a resolver.

Strict policy inheritance can prevent a useful sanitized insight from entering the
personal Vault until the source policy expressly permits derivatives. That cost is
preferable to allowing an agent to declassify employer or customer information by
paraphrase. Policy receipts improve auditability but cannot undo prior Git
propagation.

The chosen design rejects a giant mutable `MEMORY.md`, Git-history-only
checkpoints, resolver-inferred canonical heads, mandatory transcript capture,
portable references to local overlays, and automatic semantic merge. It also keeps
Git commit/push cadence outside the information architecture.

## 20. Final D3 Verdict

Adopt the **Registry + immutable Session Revision + versioned Memory Record** model,
with generated CURRENT/PROFILE and an external machine-local overlay.

This is consistent with D0 because the person owns the portable continuity; with
D1 because deterministic Core mechanics remain agent- and LLM-neutral; and with
D2 because only host-selected, sanitized semantic state crosses checkpoint or
memory boundaries. Privacy inheritance limits what personal ownership may export
from employer, customer, or project sources. It does not conflict with ownership:
ownership of a Vault is not authority to declassify another source's information.

This document is the authoritative D3 specification. Production implementation
must follow a separately reviewed migration plan. Repository/package renaming
remains out of scope until a later explicit decision.
