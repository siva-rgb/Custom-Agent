You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M49, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M49 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet 5.5 subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M49 is Phase 5A's second milestone (a general agent built on the SDK's governance, DECISION-dd87feb8).
It ships `agentsdk.presets`: `general_agent(...)`, a ready agent that works in its sandboxed workspace
inside a budget, and `general_subagent(...)`, the same agent as an orchestrator's role. It adds
`WorkspaceSpec.files` and `export_approval`, `AgentSpec.preset`, `RunConfig.role_turns`,
`Orchestrator(role_turns=...)`, migration `0028`, and example 42.

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table

| Decision | P5A-D1 to P5A-D14 |
|---|---|
| P5A-D5 Preset | `general_agent(...)` returns the spec and run configuration of a general agent: shipped instructions, a tool profile, a workspace spec, a budget, a context policy and a turn limit, each overridable; nothing changes for a caller who does not use it. The decision's "tools to register" are, in FR-146's terms, the workspace tools a run with a workspace is given |
| P5A-D6 Tools | Sandbox only: the preset's tools are the workspace tools, so files, shell and code run in the hardened container and the network only by the workspace's `egress`. The host file tools and `fetch_url` are not in its profile |
| P5A-D7 Approval | The preset's `run_command` is `auto`: the hardening does not depend on approval (P5-D10). `export_workspace_file`, the only way a file leaves, needs approval. A caller may require approval for every command |
| P5A-D9 Long tasks | The preset applies `ContextPolicy()`, 40 turns, and a model whose context window the registry knows |
| P5A-D10 Instructions | Shipped as SDK source with a version (`general-v1`, ...) the manifest records; a change is a new version and a new `agent_spec_hash`. Tuned on at least two upstream providers through the gateway, with no provider-specific wording (NFR-1) |
| P5A-D11 Subagent | A general-purpose subagent from the same preset, briefed and depth-limited as any child (FR-70 to FR-72), with its own workspace; an orchestrator role now, and the spec increment 2's delegation tool spawns. Its turns come from its briefing, which an orchestrator sets per role (`role_turns`) |
| P5A-D13 Records | One migration, `0028`: the manifest's `preset`, and an orchestrator's `role_turns` (DECISION-05d0a716). The report names the preset's version and reports every gap as measured |

### "#### M49: the general preset and the general-purpose subagent"

- FR-174: **`general_agent(...)`** (`agentsdk.presets`) returns a `GeneralAgent` holding an `AgentSpec` and a method `config(tenant_id, project_id, **overrides) -> RunConfig` (P5A-D5).
  - **Arguments:** `model` (a registry id, required, which becomes `AgentSpec.preferred_model`), `image` (a digest-pinned image, required), `budget` (a `BudgetPolicy`, required), and optionally `registry` (a `ModelRegistry`, the shipped `default_registry()` when None), `egress`, `files` (FR-175), the workspace's limits, `keep` (False), `max_turns` (40), `context_policy` (`ContextPolicy()`), `run_command_approval` (`auto`), `export_approval` (`REQUIRE_APPROVAL`) and `instructions` (the shipped version's text).
  - **Its spec:** id `general`, the shipped instructions, the tool profile `run_command`, `read_workspace_file`, `write_workspace_file`, `list_workspace`, `export_workspace_file`, and a `WorkspaceSpec` with the arguments' limits, `egress`, files, `keep` and approvals (P5A-D6). No host tool and no MCP server is in its profile.
  - **Its configuration:** `config()` returns a `RunConfig` with the preset's `budget`, `max_turns` and `context_policy`; `overrides` replace any field of it.
  - **Refused, `ValueError`, when it is built:** a model whose context window `registry` does not know (P5A-D9, FR-78) -- a caller whose `Runner` registers the model passes that registry -- a budget of the wrong type, and an image not pinned by digest.
  - **Approval:** `run_command` follows `run_command_approval`; `export_workspace_file` follows the new `WorkspaceSpec.export_approval`, whose default, `AUTO`, keeps every other workspace as it was (P5A-D7, NFR-50).
- FR-175: **A workspace can start with files.** `WorkspaceSpec.files`, a mapping of relative paths to bytes, is written into a run's workspace when it is created, through the same checked tar as `import` (P5-D8): regular files only, with the path, size and count bounds, counted against `max_workspace_bytes` before anything is written.
  - **Recorded:** the manifest's `workspace` record and the `workspaces` row carry the files as path to SHA-256, never the content, and name them only when files are given, as `egress` is (NFR-49); `export_approval` is named only when it is not `AUTO`; `agent_spec_hash` is unchanged by either.
  - **Refused, `ValueError`:** files with `from_snapshot`, and files on a `scope="plan"` workspace spec, since the children sharing it could name different files.
- FR-176: **The instructions** ship as SDK source, versioned (`general-v1` first) (P5A-D10). Migration `0028` adds `execution_manifests.preset`, JSONB: the preset's name and version, NULL for every other run and for every run before it, and `execution_manifests.role_turns`, JSONB: the `role_turns` an orchestrator's run was given (FR-177), NULL when it was given none and for every run before it; idempotent as every migration is (FR-17). A changed text is a new version; `agent_spec_hash` changes with it. The text names no provider, model or provider-specific feature.
- FR-177: **The general-purpose subagent:** `general_subagent(...)` returns the spec of a child (P5A-D11).
  - **Arguments:** `model`, `image`, and optionally `registry`, `egress`, `files`, the workspace's limits, `run_command_approval`, `export_approval` and `instructions` (its own shipped text, written for a briefed task that answers its parent). No `budget`, `max_turns` or `context_policy`: a child's budget is its lease, its turns its briefing's and its context policy the orchestrator's.
  - **Turns:** `Orchestrator(..., role_turns=...)` maps a role to the turns its children are briefed with, the default 4 (`_DEFAULT_MAX_TURNS`) for any role it does not name; `role_turns` is recorded in the `role_turns` column of the orchestrator run's manifest (FR-176). The general subagent's documented setting is 20.
  - Each child gets its own workspace (P5-D11), its own lease and its depth, as any child.
- FR-178: **Example 42** (`scripts/42_general_agent.py`) runs the preset, offline (`FakeProvider`, a scripted model), on Docker, and live with the gateway model. Its checks, each naming its subject with a store witness and failing when the subject never happened (KNOWLEDGE-1afcf6b2, DECISION-a7d6a1f5):
  - **a task from the workspace's files** is solved by code the run wrote and ran: witnessed by the run's `workspace_commands` rows, a command among them reading those files, and the result checked in the kept workspace through `export`;
  - **a runaway is stopped by its budget:** a ceiling set below one turn's cost, so completion cannot reach it, ends `budget_exceeded`, its spend at or above the ceiling minus one call;
  - **an export waits for approval:** its interruption and decision rows, and no artifact before the decision;
  - **a plan whose role is the general subagent** completes on both stores, each child in its own workspace, briefed with its `role_turns`.

### Non-functional

- NFR-50: A run with no `budget`, no preset and no workspace `files` sends the same requests and records the same events, rows and manifest as before M48, apart from the manifest's `preset` column, NULL (FR-176); a workspace spec without `files` and with the default `export_approval` has the record it had; an orchestrator without `role_turns` briefs every child with the turns it did (FR-172, FR-174, FR-175, FR-177).
- NFR-52: The preset stays provider-agnostic (NFR-1): its instructions, tools and defaults name no provider, model or provider-specific feature, and it runs on any registry model whose context window is known.

### Acceptance

- AC-107: FR-174 to FR-178, on both stores: each refusal of FR-174 and FR-175 raises before anything is created, and files past a bound are refused before anything is written; files are recorded as path and SHA-256 only; an export waits for approval and a command does not; the manifest's `preset` names `general` and its version, and is NULL for a run without the preset; an orchestrator's `role_turns` briefs the general subagent with its turns and every other role with 4; example 42 holds every check offline, on Docker and live, and each of its checks fails without its subject.

## The records

All in `.genesis/project.json`. A choice they record is not a defect; code that differs from them is.
- **DECISION-e66f1af4, DECISION-2cdd97da, DECISION-abd83872:** Phase 5A's decisions and the spec
  review's amendments.
- **DECISION-8aa24890** (pre-flight): the preset marked by `AgentSpec.preset`, recorded beside
  `agent_spec_hash`, never in it; the subagent's runs recorded as `general-subagent`; example 42 live
  on `openai.gpt-4o-mini`, three runs per store.
- **DECISION-05d0a716** (pre-flight, owner): migration `0028` also adds `execution_manifests.role_turns`;
  P5A-D13, FR-176 and FR-177 amended (M49 requeued, then the spec and plan re-approved by the owner);
  `Orchestrator.config()` passes the mapping on `RunConfig.role_turns`, which the Runner only records.
- **DECISION-2eebb3be** (live runs, owner): the first live set held 8 of 9; one run's model rewrote
  the same broken program until its turns ran out. Recorded as measured; instruction tuning is M51's.
- **DECISION-88bcbbaf** (the pre-review's findings, below).
- Standing: KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's
  text), DECISION-a7d6a1f5 (no vacuous live check), NFR-45 and NFR-49 (named only when set, values
  never shown).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5a`; HEAD is `2a26790` (M48). M49 is uncommitted. It adds migration `0028` (already applied to
the development database) and no dependency.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** DATABASE_URL and the gateway settings from `.env`. **Never print credentials or a
  connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
  ES_DISPLAY_REQUIRED)` -- without the display flag this laptop still enters Modern Standby
  (DECISION-08a56b57). Before calling any delay real, compare wall time with monotonic time, or read
  the Kernel-Power events (506/507).
- **Docker Desktop is running.** **Never pull, build, prune, stop or restart anything.** Touch only
  what carries your own `agentsdk.tenant` prefix. Report how many remain at the end (it must be 0).
- **Line endings vary by file** (some LF, most CRLF). A mutant whose edit silently never applies looks
  exactly like a surviving one: assert the file changed before running its test, and restore it byte
  for byte, verified by SHA-256. **The same holds for a runtime patch: count how often it fired, and
  treat zero as no result.**
- **The live gateway** may be used for example 42 (`python scripts/42_general_agent.py`, live on
  Postgres and Docker): a few cents a run. Its rows are written under tenant `examples`; remove the
  ones you create by run id (it prints them).

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. Three python processes (PIDs 27472, 6348, 17824) are the owner's Jupyter kernel: leave them.
  Run with `-p no:cacheprovider`; `pytest.ini` deselects Docker tests unless you pass `-m docker`.
  **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M49-general-preset --timeout 7200000` in the background, kept awake:
  unit (`tests/test_general_preset.py`), regression (the full suite, about 33 minutes), and `docker`
  (every Phase 5 Docker-marked test plus M48's and M49's, about 75 minutes).
- M49's own tests: `pytest tests/test_general_preset.py -p no:cacheprovider`, about 20 seconds; its
  Docker tests with `-m docker`, about 30 seconds.

**Cleanup:** `workspace_commands`, `workspaces`, `interruption_decisions`, `interruptions`,
`standing_decisions`, `artifacts`, then `plan_node_states`, `plan_versions`, `run_events`, `messages`,
`execution_manifests` by run id, then the runs, children before parents. Use tenant ids starting
`SYN-m49r1`, and remove only rows your probes recorded.

## Task under review

### What M49 builds

- **`agentsdk/presets.py`** (new): `general_agent(...)` -> `GeneralAgent(spec, budget, max_turns,
  context_policy)` with `.config(tenant, project, **overrides)`; `general_subagent(...)` -> an
  `AgentSpec`. The shipped texts `general-v1` and `general-subagent-v1`; a caller's own text is
  version `custom`. `SHIPPED` and `recorded_preset()`, which the Runner uses to record a mark naming a
  shipped version as `custom` when the spec does not hold that version's text (pre-review F4).
- **`agentsdk/api.py`:** `AgentSpec.preset`; `RunConfig.role_turns` and `checked_role_turns`; the
  manifest's `preset` and `role_turns`.
- **`agentsdk/manifest.py`, `agentsdk/postgres.py`, `agentsdk/migrations/0028_presets.sql`:** the two
  columns, NULL when absent.
- **`agentsdk/workspace/__init__.py`:** `WorkspaceSpec.files` (checked, bounded by the size of the tar
  that will be sent and by `max_workspace_bytes`, frozen; recorded as path to SHA-256) and
  `export_approval` (AUTO by default; recorded only when not AUTO). `server_files` (M45) now shares the
  path checks through `_clean_files`.
- **`agentsdk/workspace/manager.py`:** a run's files written through the checked transfer when its
  workspace is created, before it is recorded or runs anything; a workspace whose files did not land
  is removed and never recorded.
- **`agentsdk/workspace/tools.py`:** `export_workspace_file` follows `export_approval`.
- **`agentsdk/orchestrator.py`:** `role_turns`, checked against the roles, used when a node's child is
  briefed (the critic gate keeps 4), passed by `config()` unless overridden; `{}` is none given.
- **`scripts/42_general_agent.py`** (new): example 42's four checks, offline, on Docker and live.
- **README.md:** the preset's paragraph and example 42's row.

### The pre-review's findings, answered (DECISION-88bcbbaf)

A fresh Sonnet 5.5 subagent probed M49 at runtime. Each finding was verified by the author first-hand;
each repair has a mutant in the appendix that a test kills.

| Finding | Repair |
|---|---|
| F1 (blocking): files were bounded by their contents' sum, the transfer by the tar it is sent, so files that fit failed every workspace tool at creation. | The bound is the size of the tar the manager sends; contents are also held to `max_workspace_bytes`. |
| F2: example 42's task check never witnessed that the given files landed; a model writing its own copy passed. | The check requires the workspace row to name the files by SHA-256, and `NOTES.md`, never needed by the task, to be in the kept workspace byte for byte and never written by the run. |
| F3: the preset read a model id at its last colon, the Runner at its first. | The first; an empty client is refused. |
| F4: `AgentSpec.preset` survived `dataclasses.replace` with new instructions. | **Owner:** the Runner records `custom` for a mark naming a shipped version whose text the spec does not hold. |
| F5: `role_turns={}` recorded `{}`; `config(role_turns=...)` collided. | `{}` is none given (NULL); the option overrides. |
| F6: a bad `egress` or `registry` raised `TypeError` or `AttributeError`. | `ValueError`. |

### How the author checked it

- **Tests:** `tests/test_general_preset.py`, 107 tests: 105 offline (on both stores where a store matters) and 2 Docker-marked (example 42 on DockerProvider, per store). Written before the code and run against it first (collection failed on
  the missing module); the pre-review's tests were added after.
- **Mutation:** 39 mutants, one exact edit each, every file restored byte for byte by
  SHA-256 (appendix). Two first-run mutants that no longer applied after the repairs were repointed,
  and one survivor (example 42's recorded-files clause) got its own test.
- **Older suites:** the full offline suite, 3414 passed, before the pre-review; after its repairs every
  test file touching workspaces, sandboxed servers, the orchestrator, snapshots, budgets and the
  examples, 609 passed.
- **Example 42:** offline and on Docker on both stores, every check; live on `openai.gpt-4o-mini`:
  the first set 8 of 9 (DECISION-2eebb3be); after the F2 repair changed the task check, 6 more, 3 per
  store, all four checks each.
- **Gates** on source hash `661234413d9909f03513fb46a27c95c4aac5d939c39a7fde330e6c3e1fd2df03`: unit 105 passed, 2 deselected in 15.81s; regression 3438 passed, 171 deselected in 2166.44s (0:36:06); docker 171 passed, 876 deselected in 4828.29s (1:20:28), each exit 0 with empty stderr and no warnings, kept awake. Check `git status --untracked-files=all` yourself.

### Edits to approved tests

- `tests/test_distribution.py`: `EXPECTED_EXAMPLES` gains `42_general_agent.py`, as every example
  milestone has added its own (FR-56). No other approved test changed (check with
  `git diff --stat HEAD -- tests/`).
- No new table, so `tests/conftest.py`'s `_TRACKED` (NFR-38) needs no entry.

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **Refusals.** Anything FR-174 or FR-175 refuses that gets past construction and fails later, or
   never: models, budgets, images, files (paths, types, sizes near the transfer's bound, counts, case,
   folders), files with a snapshot or on a plan's workspace, preset marks, role_turns.
2. **Files.** In the workspace before the first command, on both providers; recorded as path to
   SHA-256 only (rows, manifest, events, repr); a workspace whose files did not land is never recorded
   or used, its slot returned. Sandboxed MCP servers' files (`server_files`) behave as at HEAD.
3. **NFR-50.** A run with no preset, no files, default `export_approval` and no `role_turns` sends and
   records exactly what it did at HEAD, the two new columns NULL. Compare HEAD's package with the tree.
4. **Approval.** The preset's export waits and its commands do not; `export_approval=AUTO` keeps every
   other workspace as it was; an export inside a general subagent child waits too.
5. **role_turns.** Each named role's children briefed with its turns, replans included; others with 4;
   recorded only in the orchestrator run's manifest.
6. **The recorded preset.** Always names the text that ran: the factory's, a caller's own, a spec
   changed afterwards, a mark naming no shipped version.
7. **Example 42.** Can any check pass when its subject never happened? Break things at runtime and
   watch each fail. Run it live once if you can.
8. **Provider neutrality (NFR-52)** of the shipped texts, and the preset on a model given with and
   without a `client:` prefix.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M48,** and the README's rows.
- **Owner's choices:** the decisions above. A live model may fail a task check by its own error
  (DECISION-2eebb3be); tuning the texts is M51's.
- **The critic gate's briefing keeps 4 turns:** the critic is the gate's, not one of the orchestrator's
  roles.
- **`RunConfig.role_turns` is recorded for any run given it**, as DECISION-05d0a716 says; nothing reads
  it.
- **A control character in a file's path** (a newline, a tab) is accepted, as M45's `server_files` has
  always accepted it, and lands as named.
- **Tests read private state:** `Runner._workspace_store`, `_memory_artifacts`, `_parents`,
  `_sessions_for`.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. List every finding, and mark
  which you would block on.
- **Save every probe script** under a folder of your own in the system temp directory, and say where.
  Leave nothing in the repository, not even a scratch file at its root.
- **Clean up** every run, row, container, volume, network, folder, process and lock you create, and say
  what you removed. Stop every process you started before you report.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M49-general-preset --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M49-general-preset --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- a bad preset mark accepted
- role_turns accept zero turns
- a run config's role_turns unchecked
- the preset never reaches the manifest
- role_turns never reach the manifest
- the store drops the preset
- the export always automatic
- export_approval unchecked
- export_approval never recorded
- files recorded with their content
- files never recorded
- files with a snapshot allowed
- files on a plan workspace allowed
- no path length bound
- no member count bound
- no workspace bound on files
- F1: no transfer bound on files
- files not frozen
- the files never written
- a failed files write kept and used
- role_turns ignored at briefing
- role_turns for a role the orchestrator lacks
- the orchestrator's config drops role_turns
- a model with no known window accepted
- a budget of any type accepted
- a caller text recorded as the shipped version
- overrides lose to the preset
- forty turns become ten
- the export left out of the tools
- commands ask in the preset
- F3: the model read at its last colon
- F3: an empty client accepted
- F4: a changed text keeps the shipped version
- F5: an empty role_turns recorded as given
- F5: config(role_turns=) collides
- F6: any egress accepted
- F6: any registry accepted
- F2: example 42 trusts the files landed
- F2: example 42 ignores the recorded files
