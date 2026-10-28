You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails to
start. M38 was implemented by Opus 5.5 and its pre-review run by an Opus 5.5 subagent; M37 was
approved by Sonnet 5 after rounds by Fable 5.1. Bring no memory of an earlier review into this
one: if your session holds any, say so and ask for a fresh one.

M38 is **Phase 5's first milestone**: the workspace contract, the fake provider and the workspace
tools. Re-derive the requirements from SPEC.md, section "### Phase 5, increment 1":
- the study and the decisions table P5-D1 to P5-D15, as amended at M38's pre-flight;
- "#### M38": FR-136 to FR-141, with FR-146 and FR-147, which the owner moved into M38;
- NFR-40 to NFR-45 (NFR-45 as amended at M38's pre-review), and AC-95 and AC-96.

Read the records:
- KNOWLEDGE-bdc63cbf: the Docker study the decisions rest on;
- DECISION-31b2c79d, DECISION-47158c90: Docker as the first provider, a fake one in the gates, and
  the fifteen decisions accepted;
- KNOWLEDGE-d9ea0d40, DECISION-a00773b0, DECISION-a7d6a1f5: M37's two carried caveats and the
  rules for every live check;
- DECISION-97fb116b: the owner's M38 pre-flight -- the tools and their approval moved into M38;
  example 31's lost-executions clause counted per tool live, timed-out calls included;
- DECISION-bb81ebbe: NFR-45 amended (an env value a model writes into arguments is recorded as
  written);
- KNOWLEDGE-6bbb571b: the pre-review's eleven findings, what became of each, and the first
  regression gate's two failures.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `6e59220` (M37). M38 is uncommitted. Migration `0019_workspaces.sql` is added.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop enters Modern Standby on battery when
  idle,** which suspends a gate mid-run; run long commands under a wrapper holding
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is installed and may be running; M38 does not use it.** Do not start, stop or
  prune it, and touch no container, image or volume: other projects' are there.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256. **Check that no repository file changed by content, never by
  mtime.**
- **Timings:** the full suite takes about 22 minutes; `tests/test_workspace.py` about one; one
  offline run of example 31 about 6 seconds; example 32 a second.

**Gates:** `genesis gate . M38-workspace-contract --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `workspace_commands`, then `workspaces`, then `interruption_decisions`,
`interruptions` and `standing_decisions`, then `artifacts` by `source_run`, `plan_node_states`,
`plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs, children first, by
run id. Use tenant ids starting `SYN-m38r1`, and remove only rows of runs your probes recorded.

## Task under review

**M38-workspace-contract**, round 1, risk **high**.

### What M38 builds

**1. `agentsdk.workspace` (FR-137 to FR-139).** `WorkspaceSpec`: an image pinned by digest, seven
limits with defaults and fixed maxima (none can be switched off), `env`, `scope`, `keep` and
`run_command_approval`. `CommandOutcome`, `WorkspaceHandle` and the `WorkspaceProvider` protocol.
`FakeProvider`: an in-memory tree of folders and regular files, commands as registered Python
callables, a kill that is total (each command writes through its own view of the files, cut off at
a kill), output kept to `max_output_bytes` and counted past it, and `import_` that accepts regular
files and folders only. `run` takes a `command_id` keyword the spec's signature did not name: `kill`
needs it to name the command.

**2. The manager and the tools (FR-138, FR-146, FR-147),** `agentsdk/workspace/manager.py` and
`tools.py`. `Runner(..., workspace_provider=None, max_concurrent_workspaces=4)`. A run whose agent
has a workspace adds to its tool view the workspace tools its profile names, bound to its own
`RunWorkspace`, which creates the workspace at their first need, records it, each command and its
removal, measures it after each command and write, and refuses every later one past
`max_workspace_bytes`. The workspace is removed before the run's terminal event on every path.
`run_command` is `DESTRUCTIVE` and asks for approval unless the spec says `AUTO`; the file tools
never ask. Paths are refused at step 2 by a schema pattern.

**3. The records (FR-140),** migration `0019`: `workspaces` (its removal its only update, made
once, held by a trigger) and `workspace_commands` (written once), each tenant-scoped; the manifest's
`workspace` column; six event types; `InMemoryWorkspaceStore` and `PostgresWorkspaceStore`.

**4. Example 31's carried caveats (FR-136).** The `barrier` and `wait` tools' timeouts outlast
`HOLD_BOUND` and `WAIT_BOUND`. The lost-executions clause runs live: per qualified tool, the
executions whose result reached no call must not outnumber that tool's calls that ended with the
executor's `ToolCancelled` or `ToolTimeout` error (DECISION-97fb116b).

**5. Example 32 (FR-141)** on `FakeProvider`, offline on both stores and live.

**Choices the author made that the SPEC leaves open** (judge each):
- `scope="plan"` and `keep=True` are accepted by the spec and refused at run start, as
  `WorkspaceUnavailable`, until M41; `max_concurrent_workspaces` is validated, not enforced (M41).
- Beside a provider, a registered tool named as any workspace tool, `export_workspace_file`
  included, refuses the Runner: it would run on the host.
- A non-zero exit is the command's result, not a tool failure.
- The executor now lets `WorkspaceRefused` through by name, as it does `ToolBlocked`.
- `run_command`'s executor deadline is `max_command_seconds + 30`, so a command ends by its own
  timeout and reports it.

### Every finding so far, answered

| Source | Finding | Answer |
|---|---|---|
| M37 approval, 1 | Example 31's lost-executions clause ran only strictly, so a displaced execution id passed NAMES live | Fixed, per tool (DECISION-97fb116b): `test_a_result_whose_id_was_displaced_fails_names_live_and_the_barrier_outlasts_the_hold`, `test_a_timed_out_call_does_not_cover_another_tools_lost_result`; both failed on M37's code |
| M37 approval, 2 | The barrier kept ToolSpec's 30 s default, half of HOLD_BOUND | Fixed; read off the tools the Runner was given, in the same test |
| Pre-review D1 | A newline hid a `..` from the path pattern, and `$` admitted a trailing one | Fixed: no control character, `\Z` (path test, five more cases) |
| Pre-review D2 | A cancel during removal abandoned the removal, its row and its event | Fixed: removal runs to its end (`test_a_cancel_during_the_removal_waits_for_it_and_its_record`, both stores) |
| Pre-review D3 | A command cut short by its run's cancellation left no row | Fixed: recorded `killed: cancelled` (`test_a_command_whose_run_is_cancelled_is_recorded_cancelled`, both stores) |
| Pre-review D4 | A workspace whose row failed still ran commands; a failed command row hid that it ran and skipped the bound | Fixed: unrecorded workspaces are removed; a failed command row still measures and tells the model the command ran (two tests) |
| Pre-review D5 | A server's success spelled `ToolTimeout: ...` counted as a timed-out call | Fixed: only executor error results count (`test_a_server_success_spelled_like_a_timeout_hides_no_lost_execution`) |
| Pre-review D6 | A command holding its `files` kept writing after its kill | Fixed: per-command view (`test_a_kill_is_total_however_the_command_holds_its_files`) |
| Pre-review D7 | An env value the model copies into argv reaches the command's event and row | Owner: NFR-45 amended, recorded as written (DECISION-bb81ebbe); declared |
| Pre-review D8 | The refusal advised removing files, which nothing can do | Fixed: it says every later command and write is refused |
| Pre-review D9 | `FakeFiles.untar` let a folder replace a file | Fixed, both ways (parametrised test) |
| Pre-review D10 | argv admitted a NUL | Refused earlier as unstorable; the schema now refuses it too (`test_run_commands_schema_refuses_a_nul_in_any_argument`) |
| Pre-review D11 | Example 32's live approval check matched by argv; its records check's subject was the rows under test | Fixed: by call id; subject is `WorkspaceCreated` |
| Pre-review, note | A per-run lock was held across `provider.create` | Removed: concurrent first needs share one creation task |
| Regression gate, 1 | `tests/test_adr12_comparison.py` pins the package list, which gains `agentsdk.workspace` | Approved-test edit, below, as at M33 |
| Regression gate, 2 | The README's not-built table lost the word sandbox (NFR-10's test) | README row reworded: "Sandboxed execution: ..." |

### How the author checked it

- **Tests first:** `tests/test_workspace.py` and `tests/workspace_contract.py` were written before
  the code. Against M37's example 31, the displaced-id and per-tool tests failed and the timeout
  test passed (it guards a false FAIL).
- **Mutation:** 29 of 29 killed by named tests, each file restored by SHA-256: twenty of the
  original design, and each pre-review repair reverted.
- **Gates,** on source hash `023def9c`:
  - `unit`: 176 passed in 57.25s;
  - `regression`: 2738 passed in 1427.87s (started 17:11:00 UTC on 2026-10-27).

  The first regression run failed two tests, both real: see the table's last two rows.
- **Live,** against the gateway model on PostgreSQL, before the last gate run (no source changed
  since):
  - examples 27 to 30: 3 of 3, 6 of 6, 6 of 6 and 5 of 5;
  - example 31: the seven safety checks PASS, NAMES now with its lost-executions clause live;
    plan completion SKIP (the plan ended failed: the model followed the injection);
  - example 32: its four live checks PASS -- every command after its call's approval and once,
    every result untrusted, the workspace removed before the run's last event, the rows written
    and the env value in none.
  - **Cleanup:** every run's rows removed by the printed run ids, descendants included, catalog
    and artifact rows by those runs' own tenancies, counted first. The store is at the baseline:
    10351 runs, 19 under example-tenant, 0 SYN, none running, 0 artifacts, empty catalog,
    interruption and workspace tables.

### Edits to approved tests

- **`tests/conftest.py`:** `_TRACKED` gains `workspaces` and `workspace_commands` (NFR-16, NFR-38's
  list).
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `32_workspace.py`.
- **`tests/test_adr12_comparison.py`:** `test_the_harness_is_not_packaged_in_the_wheel`'s package
  list gains `agentsdk.workspace`, with its comment, as M33 added `agentsdk.mcp`.
- No other approved test is edited.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m38-r1-probes` in your own scratchpad.

0. **The table above:** check every row is true, and both approved-test edits by content.
1. **Escape:** any argument, path or tar that reaches the provider leaving `/workspace`, or a
   registered or MCP tool that runs where a workspace tool should.
2. **Isolation:** any run reaching another run's or tenancy's workspace, files or records.
3. **The record:** written once, removal once, tenancy on every statement, both stores; a
   workspace or command that ran unrecorded; NFR-45 as amended.
4. **Terminal paths:** completed, failed, max turns, cancelled while creating, running, waiting for
   approval and removing; RunCancelled still last (FR-50); nothing left in the provider.
5. **Approval:** a command running before, without, or twice after its approval.
6. **The fake's contract:** timeout, kill, limits, output caps, environment, transfer types, size.
7. **Example 31's live clause:** a false pass or a false FAIL under DECISION-a00773b0 (a model
   quoting earlier results) and DECISION-a7d6a1f5; and the barrier against the timeouts in force.
8. **NFR-41 and regressions:** a run without a workspace unchanged beside a provider; the full
   suite; examples 27 to 32 live.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M37,** and the README's M38 rows.
- **Owner's choices:** DECISION-31b2c79d, DECISION-47158c90, DECISION-97fb116b, DECISION-bb81ebbe.
- **M39 to M42's requirements are not M38's:** Docker, transfer filtering on the host side,
  reaping, plan workspaces, cancellation killing a container.
- **Examples read private state** as earlier ones do: `runner._sessions_for` for histories.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter; latent, out-of-scope or
  cosmetic findings are caveats in your reason.
- **Clean up** every run, row, temp folder, process and lock you create, and say what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M38-workspace-contract --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M38-workspace-contract --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the nine attack items, and
your verdict.
