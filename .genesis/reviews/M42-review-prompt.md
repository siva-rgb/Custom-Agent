You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails to
start. M42 was implemented by Opus 5.5 and its pre-review run by a Sonnet 5 subagent; M41 was
approved by Opus 5 and M40 by Sonnet 5. Bring no memory of an earlier review into this one: if your
session holds any, say so and ask for a fresh one.

M42 is **Phase 5's exit signal** (P5-D14): hostile code in a plan's sandboxed children, contained,
recorded and cleaned up, on both stores. Re-derive the requirements from SPEC.md, section
"### Phase 5, increment 1":
- the decisions table, P5-D14 above all;
- "#### M42": FR-154 and AC-100, as amended at M42's pre-flight, quoted exactly -- do not take this
  brief's summary for them;
- FR-138 (its `CommandOutcome` sentence: `limit`), FR-148 (its first paragraph: the setup bound),
  the `workspace_commands` bullet (migrations `0021` and `0022`) and the `run_command` bullet, each
  as amended in M42;
- NFR-40 to NFR-45.

Read the records:
- KNOWLEDGE-64360f90, DECISION-b7423cae: M41's approval and the six items it carried into M42;
- DECISION-a00773b0, DECISION-a7d6a1f5, KNOWLEDGE-1afcf6b2: the standing rules for every exit
  signal -- checks hold under a model quoting earlier results; **no vacuous check: a result a check
  cannot judge is a failure**; each witness independent of the property, tested by construction;
- DECISION-94bfee7c: **no check keys on an error message's text**;
- DECISION-21e7f6fb: the owner's pre-flight choices (bound names, capped probes, the live model
  writes the probes verbatim and a decline fails, M41's items);
- DECISION-e799700b: a workspace's setup bounded on its own, 120 s;
- DECISION-4689af02: the pre-review's nine findings and the owner's repairs;
- DECISION-f4e7dd24: the gate timeout, 3600000 ms.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `08bdc39` (M41). M42 is uncommitted, with SPEC.md's and README.md's M42
amendments and migrations `agentsdk/migrations/0021_command_limits.sql` and
`0022_command_limits_null_safe.sql`, **both already applied to the dev database**. `0022` exists
because `0021`'s CHECK passed on NULL (`killed = 'limit'` is NULL when `killed` is NULL) and `0021`
was already applied; read both.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11, 16 CPUs. **This laptop sleeps and throttles when idle:**
  run long commands under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is running.** The tests and examples use
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df` (and one test
  `redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2`), already on the
  host. **Never pull, build, prune, stop or restart anything.** The host has 4 containers and 3
  volumes of other projects: touch only workspaces whose `agentsdk.tenant` starts with your tenant
  prefix, and raw containers or volumes you make yourself labelled `agentsdk-probe`. Report how
  many of either remain at the end (it must be 0). In Git Bash a path meant for inside a container
  is rewritten: use `MSYS_NO_PATHCONV=1` or PowerShell.
- **Reaping is live in every Runner you start,** and example 36 kills a real child `Runner`
  process: a workspace labelled with a dead owner of this host is removed by the next Runner of any
  test or probe. Make forged owners with your own tenant prefix, and expect them gone.
- **Example 36 tries `host.docker.internal:5432`** -- the dev database's port -- and a public
  address from inside its containers. With `--network none` neither connects; a removal test that
  takes the flag away connects to the port and closes at once, sending nothing.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
  Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **The gates run `FakeProvider` only:** `pytest.ini` deselects tests marked `docker`. Run M42's
  live with `pytest tests/test_sandbox_contention.py -m docker -p no:cacheprovider` (about 18
  minutes: the plan ten times on each store, the six removals, a real out-of-memory kill), and the
  rest of Phase 5's with `tests/test_workspace.py tests/test_workspace_tools.py
  tests/test_workspace_plan.py tests/test_docker_provider.py -m docker` (about 7 minutes).
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte for
  byte, verified by SHA-256. **Check that no repository file changed by content, never by mtime.**
- **Timings:** the full suite takes about 30 minutes; `tests/test_sandbox_contention.py` about 4
  minutes without Docker.

**Gates:** `genesis gate . M42-sandbox-contention --timeout 3600000` in the background. Both gates
record the content hash, which includes `.pytest_cache`: after a run with a failure, the next run's
two gates can record different hashes and one reads stale.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `workspace_commands`, then `workspaces`, then `interruption_decisions`,
`interruptions` and `standing_decisions`, then `artifacts` by `source_run`, `plan_node_states`,
`plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs, children first, by
run id. A plan's child runs own artifacts too: find them by `parent_run_id`. Example 36 prints its
run ids, the killed Runner's included (that run stays `running`: its process was killed). Use
tenant ids starting `SYN-m42r1`, and remove only rows of runs your probes recorded.

## Task under review

### What M42 builds

- **Example 36** (`scripts/36_sandbox_contention.py`), the exit signal: an orchestrated plan of eight
  children, each with a role of its own whose instructions carry its exact calls (the planner copies
  only short objectives). They run probes that try the network, links out, a fork bomb, memory,
  disk and output without end, the environment, and a process that ignores SIGTERM and leaves its
  group -- that one last, cancelled with the plan. Before the plan a Runner is killed mid-command (a
  real child process on Docker, `--child-runner`); the plan's Runner reaps what it left. An operator
  approves every command. 14 checks offline (13 live), each naming its subject; a subject that never
  happened fails, live included. In memory a `RecordingRunner` keeps every child's events.
  A probe is recognised by `runs_probe`: `python -c` with the probe's syntax tree, whatever its
  spacing.
- **Bound names:** `CommandOutcome.limit`, the `WorkspaceCommandEnded` payload's `limit`, the
  command row's `limit_name` (migrations `0021`, `0022`), and the tool text `killed: limit
  (memory_bytes)`. Docker names `memory_bytes` only when `OOMKilled` and the command's own process
  ended by SIGKILL (exit 137); the fake names what `FakeCommand.limit(name)` was given.
- **The setup bound** (`RunWorkspace.ensure`): reaping and the slot wait under `asyncio.timeout`, a
  creation waited for the rest of the bound and otherwise left to finish (still waited out by a
  cancellation); past it the call fails `WorkspaceUnavailable`. Every workspace tool's deadline is
  `SETUP_SECONDS + MEASURE_SECONDS + max_command_seconds + 30` (120 + 120 + ... ).
- **The fake's links:** `FakeFiles.link`, packed as `docker cp` packs them, listed `link`; an import
  onto a link replaces it, one under a link parent is refused `ValueError`, as on Docker.
- **M41's carried items:** example 35's `aclose()` check given a subject; `_Slots.take(..., begun)`;
  `Runner._workspace_refusal()` without its unused argument; README declarations.

### Every finding so far, answered

| Finding | Where it came from | Answer |
|---|---|---|
| Example 35's `aclose()` check passed with `aclose` removing nothing | M41 review caveat 1 | A kept workspace stands before `aclose()`, witnessed by a listing; a test patches `aclose` away and the check fails |
| An export under a shared plan workspace can name a sibling's command | M41 caveat 2 | Declared (README) |
| A wait queued for a space a sibling on another loop already began | M41 caveat 3 | `take(..., begun)` checks under the lock; a test |
| `NotADirectoryError` vs `ValueError` for a write under a file | M41 caveat 4 | Declared |
| `_workspace_refusal(spec)` ignored its argument | M41 caveat 6 | Dropped; a test |
| FR-153 misquoted in M41's brief | M41 orchestrator | This brief quotes nothing: re-derive from the SPEC |
| `0021`'s CHECK passed on NULL | M42's own test | Migration `0022` |
| A name lookup with no network outlasted an 8 s timeout | Example 36 on Docker | Each attempt has 5 s in a thread; the probe ends in about 15 s |
| `run_command`'s deadline covered the workspace's setup: a started command was cut off, recorded `cancelled` (2 of 40 repetitions) | Example 36 ten times on Docker | DECISION-e799700b: the setup bound; tests on the fake |
| The live planner ran out of output, then mangled eight programs copied into its plan | Live runs | Each child's calls in its role's instructions; live output allowance |
| A live model dropped a blank line, then spaces after commas, copying a probe | Live runs | `runs_probe` compares syntax trees; a test |
| A plan that never made a workspace never reaped the killed Runner's leftovers | Live run | A closing reap, after the check read what remained |
| F1: `runs_probe` raised `MemoryError` on a deep argument and stopped the operator | Pre-review | Any parse failure is not the probe; a test |
| F2: the network check skipped a killed run of the probe | Pre-review | It reads every run; a test |
| F3: in memory the checks could not read a child's events | Pre-review | `RecordingRunner` (DECISION-4689af02); three violation tests |
| F4: the fork check accepted any early `OSError` | Pre-review | It needs the pid limit's `BlockingIOError` |
| F5: the fake's links differed from Docker's | Pre-review | Imports match; reads through a link parent declared |
| F6: a child out of memory marked the whole command `limit` | Pre-review | Exit 137 required; a live test |
| F7: a create that never returns blocks a run's end and `aclose()` | Pre-review | Declared |
| F8: the deadline left out the size read before a command | Pre-review | `MEASURE_SECONDS` in the deadline; a test |
| F9: a 135 s net command on a stalled daemon | Pre-review | M39/M40's declared timeout path |
| The fake's "killed" Runner ended its own run after 120 s when the plan ran slow | Full test session | Its command now outlives any plan (3600 s) |

### How the author checked it

- **Gates** on source hash `97078a81`, recorded 2026-10-30: unit 28 passed (10 deselected) in 3:50; regression 2911 passed (56 deselected) in 26:57. Verify both evidence files carry that hash.

- **Mutation:** 27 of 27 mutants killed, each by the test naming its property, three of them live on
  Docker (network, pid limit, OOM attribution).
- **Docker-marked:** all of Phase 5's, M42's ten-in-a-row and six removals included, passed on the
  live daemon (405 tests in the last full workspace session; the one failure there, the fake's
  reaping removal, came from the "killed" Runner ending its own run and is repaired above, with the
  11 removal tests rerun after).
- **Example 36:** offline 14 of 14 on both stores; scripted on Docker 14 of 14; live (gateway,
  PostgreSQL, Docker) 13 of 13 on the final code, its rows then removed by its printed run ids.
- **Store and Docker:** back at baseline after every run (runs 10351, every SYN tenant 0; 4
  containers, 3 volumes).

### Edits to approved tests

- `tests/test_distribution.py`: `EXPECTED_EXAMPLES` gains `36_sandbox_contention.py`.
- No other approved test changed. `scripts/35_workspace_plan.py` (not a test) gained a closing
  archivist run, so its `aclose()` check has a subject.

## Attack these first

0. **Vacuity, the standing rule.** For each of example 36's 14 checks, find a way its subject is
   witnessed while its property is not tested, or holds for the wrong reason. Build your own
   violations of the SDK -- not of the example -- and see which check fails. Re-run the quoting
   shape (DECISION-a00773b0) and a declining child.
1. **AC-100's removals.** Is each `without(...)` in `tests/test_sandbox_contention.py` a fair reading
   of the subject AC-100 names? "The container kill on cancel" takes away three mechanisms (the
   provider's kill, the command's removal, the workspace sweep by id); "the transfer filter" takes
   away `read_tar` and `one_file`'s refusal. Would a narrower or a different removal pass?
2. **The setup bound.** Slots (`held`) on every path through the bound; a creation left running;
   a cancellation in the bounded wait; FR-152's slot wait now ends at 120 s -- is that change
   stated where a user would read it?
3. **Bound names and the migrations.** `limit` only beside `killed='limit'` on every path; the row,
   event and text agree; apply `0021` and `0022` to an isolated schema, not the dev tables.
4. **Docker's OOM attribution** by exit code 137: a main process that exits 137 by itself; a main
   process OOM-killed with a child alive.
5. **The fake's links** against Docker's, by exception class.
6. **The killed Runner and reaping, live.**
7. **Secrets (NFR-45)** against every record the example reads and any it does not.
8. **The plan's cancellation, live.**
9. **M41's carried items,** each against its test.
10. **NFR-40 and NFR-41:** the runtime dependencies, and runs without a workspace unchanged.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M41,** and the README's M42 rows.
- **Owner's choices:** DECISION-b7423cae, DECISION-21e7f6fb, DECISION-e799700b, DECISION-4689af02,
  and every earlier one M41's brief listed.
- **M39/M40's timeout path on Docker** (pre-review F9): on a stalled daemon a timed-out command can
  take far past its timeout to end. Report it if reachable on a healthy daemon.
- **A killed process's in-memory rows die with it,** so the reaped row is checked on PostgreSQL only.
- **`runs_probe` ignores spacing and comments** by comparing syntax trees: the same program.
- **A live model that declines or alters a probe fails that check** (DECISION-21e7f6fb).
- **Examples read private state:** `runner._sessions_for`, `_artifacts_for`, `_workspace_store`,
  `_workspaces.reap()`, `orchestrator._runs`, and `RecordingRunner` overrides `Runner._open`.
- **The SPEC's M42 amendments await the owner's ratification** at close.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter; latent, out-of-scope or
  cosmetic findings are caveats in your reason.
- **Clean up** every run, row, container, volume, temp folder, process and lock you create, and say
  what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M42-sandbox-contention --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M42-sandbox-contention --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict.
