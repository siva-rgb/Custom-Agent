You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is round 2. Use a fresh session: Fable 5.1, falling back to Sonnet 5.5** if Fable 5.1 fails
to start. Fable 5.1 rejected round 1 (below); M43 was implemented by Opus 5.5 and its pre-review run by
an Opus 5.5 subagent; M42 and M41 were approved by Opus 5, M40 by Sonnet 5. Bring no memory of an
earlier review into this one: if your session holds any, say so and ask for a fresh one. Judge the
whole task, not only the repair.

M43 opens **Phase 5's second increment**: M42's carried items, then compute measured and charged to
budgets. The requirements are quoted below **verbatim from SPEC.md as approved** (DECISION-97b301ea:
briefs quote, never summarise). Check each quote against SPEC.md yourself; where this brief and
SPEC.md differ, SPEC.md wins and the difference is a finding.

## The requirements, verbatim

### From the decisions table (P5-D17, P5-D18, P5-D23)

| Decision | P5-D16 to P5-D25, 2026-10-30 |
|---|---|
| P5-D17 Compute | Every command runs under a PID-1 `sh` wrapper that runs it, kills every other process, then prints the container's `cpu.stat` reading as its last line. The SDK reads that line from a tail it keeps apart from `max_output_bytes`, and nothing the command prints can follow it. `CommandOutcome.cpu_seconds`, the command's row and its `WorkspaceCommandEnded` carry it. A command killed before the wrapper reads is charged its wall time times its `cpus` limit, the most it could have used |
| P5-D18 Charging | `BudgetPolicy.cpu_second_usd` (default None: compute counted, not charged) prices a CPU-second. Each command's cost is drawn from its run's budget lease like a model call's, so a plan stops when compute exhausts its ceiling as it stops for tokens, and a run's `cost_usd` includes it |
| P5-D23 Records | One migration per milestone (DECISION-91ec4213): `0023` (M43) `workspace_commands.cpu_usec`; `0024` (M44) `workspace_connections` (each egress connection, allowed or refused, with its reason); `0025` (M46) `workspace_snapshots`. A sandboxed server's workspace is a `workspaces` row of scope `server`. The manifest records each workspace's `egress` and each sandboxed server's spec |

### "#### M43: the carried items, and compute in budgets"

- FR-155: The items carried from M42's review are settled first (DECISION-97b301ea). Each repair comes with a test that fails without it.
  - **Example 36's timeout check** compares each command killed at its timeout with the `max_command_seconds` in force for its own probe, and the stale comment beside `COMMAND_SECONDS` is corrected.
  - **Example 35 has no SKIP:** a check whose subject never happened fails in every mode, live included, as example 36's do.
  - **Example 36's memory check** requires at least one `WorkspaceCommandEnded` to judge, as its disk check requires a refusal.
  - **FR-148's deadline** counts both size reads a stale workspace takes: `SETUP_SECONDS + 2 x MEASURE_SECONDS + max_command_seconds + 30`.
  - **AC-100's bundled removals** each name the layer they take away: the kill on cancel is tested without the provider's kill, without the command's removal and without the workspace's sweep, one at a time, each failing a check or declared as covered by another layer.
- FR-156: Compute is measured (P5-D17).
  - **The wrapper:** `DockerProvider` runs each command as `sh -c '<wrapper>' sh <argv...>`: it runs the argv, keeps its exit code, kills every other process with `kill -9 -1` until none is left, and prints a last line naming the container's `cpu.stat` `usage_usec` and the exit code, in a form the SDK alone writes after everything else. The exit code reported is the command's own. The image must provide `sh`, as it must for `list` (DECISION-0004d2dc), and `env`.
  - **Nothing the command does stops it or speaks for it** (DECISION-0883f7bd, DECISION-88395b13): the argv runs as `env -- <argv...>`, a program from PATH, so no shell builtin runs inside the wrapper; the wrapper ignores the signals a command could send it (INT, QUIT, TERM, HUP, USR1, USR2), its own shell's exports never reach the command, and its line carries a 128-bit nonce the SDK generates for that command and gives the wrapper alone, as stdin's first line. A line without the command's nonce is never a reading. A command the SDK did not kill that ends with no line carrying its nonce reports no exit code and is recorded `killed: error`, charged its ceiling.
  - **The tail:** the SDK keeps the last 4 KiB of stdout apart from what `max_output_bytes` keeps, reads the wrapper's line from it, and strips that line from what the model reads. A command that prints its own copy of the line before the wrapper's is read by the last line only.
  - **The outcome:** `CommandOutcome.cpu_seconds` is the reading. A command killed at its timeout, by cancellation or at a limit is charged its wall time times its `cpus` limit; a reading that is absent, malformed or above that ceiling is replaced by the ceiling, so no command is charged less than it could have used.
  - **`FakeProvider`:** a fake command declares its CPU with `FakeCommand.cpu(seconds)`; a killed one is charged the same ceiling as on Docker.
- FR-157: Compute is charged (P5-D18).
  - `BudgetPolicy.cpu_second_usd`, a `Decimal` or None, prices a CPU-second; None counts compute and charges nothing.
  - Each command's cost is drawn from its run's budget lease when it ends, before its result reaches the model. A lease that cannot cover it fails the run as a model call's would (`budget_exceeded`), after the command's record.
  - **Checked before it starts** (DECISION-91ec4213): `run_command` asks its lease first, as the loop does before each model call; a command whose lease is spent does not start, and the run ends `budget_exceeded`.
  - `WorkspaceCommandEnded` carries `cpu_seconds` and `cost_usd`, None when nothing priced it, as `ModelCalled` carries its cost (DECISION-91ec4213).
  - `RunResult.cost_usd` includes compute; the manifest's `budget_policy` records the price in force, and names `cpu_second_usd` only when one is set, so a plan without one records the manifest it did before (NFR-49, DECISION-91ec4213).
- FR-158: Example 37 (`scripts/37_compute.py`) runs a plan whose children burn CPU: their costs on the rows, in the results and in the plan's cost, and a second plan stopped by its compute ceiling, offline and on Docker.

### Non-functional

- NFR-23: Budgets and prices are honest. A run's recorded spend equals the sum of its `ModelCalled` costs and, since M43, its `WorkspaceCommandEnded` costs (FR-157), plus its children's, an unknown cost stays None rather than 0 (NFR-11), the shipped price table reports its own date wherever it is used, and the overshoot soft enforcement permits is bounded by `max_concurrent_subagents` and charged to the run rather than hidden.
- NFR-46: Increment 2 adds no runtime dependency (NFR-40): the proxy program ships as SDK source run in the workspace's image, and every Docker operation stays the `docker` CLI.
- NFR-48: No command is charged less than it could have used: its `cpu_seconds` is its own reading, or else its wall time times its `cpus` limit, and nothing a command prints changes it.
- NFR-49: Runs whose workspaces have no `egress`, no snapshot and no sandboxed server send the same requests and record the same events and rows as before M43, apart from the new columns, recorded empty, and `cpu_seconds`; their `agent_spec_hash` is unchanged.

### Acceptance

- AC-101: FR-155 to FR-158, on both stores:
  - **The carried items:** each repair's test fails without it.
  - **Compute:** a 2 s busy loop reads within 10 % of 2 CPU-seconds on Docker; a command that prints a forged reading, or whose detached child does, is charged its own; a command killed at its timeout is charged its ceiling; the model never reads the wrapper's line.
  - **Charging:** a plan with `cpu_second_usd` set stops `budget_exceeded` when compute exhausts its ceiling, the command that crossed it recorded; with None, compute is counted and nothing is charged.

## The records

- DECISION-97b301ea: M42's approval and the five items it carried into M43 (FR-155).
- DECISION-92986dbc, DECISION-082b52b3, KNOWLEDGE-f90ec78e: increment 2's direction, its decisions
  P5-D16 to P5-D25, and the study behind them.
- **DECISION-91ec4213**: the owner's M43 pre-flight -- one migration per milestone (`0023` holds
  `cpu_usec` only); `run_command` asks its lease before it starts; the manifest names
  `cpu_second_usd` only when one is set; `WorkspaceCommandEnded` carries `cpu_seconds` and
  `cost_usd`. NFR-23 was amended in the same pre-flight to count command costs, as FR-157 requires.
- **DECISION-0883f7bd**: the pre-review's findings and the owner's repairs (below).
- **DECISION-630a34ff, superseded by DECISION-d9a568e1**: the first said the workspace sweep alone
  does not end a cancelled command in time (1,158 s). Re-measured kept awake, it does in 3 of 3 runs;
  the 1,158 s came from a run without the keep-awake wrapper, the laptop sleeping. Each layer alone
  is enough, and nothing is carried to M44.
- **DECISION-88395b13**: round 1's rejection and the owner's repair -- the wrapper ignores the
  signals a command could send it, and its line carries a per-command nonce; a command with no line
  carrying its nonce reports no exit code, `killed: error`. FR-156 is amended to say so (quoted
  above).
- The standing rules for every example's checks: DECISION-a00773b0, DECISION-a7d6a1f5,
  KNOWLEDGE-1afcf6b2 (no vacuous check: a subject that never happened fails), DECISION-94bfee7c (no
  check keys on an error message's text).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `f4aa59f` (M42). M43 is uncommitted, with SPEC.md's increment-2 section and its
M43 amendments, README.md's M43 rows, and migration `agentsdk/migrations/0023_command_cpu.sql`,
**already applied to the dev database**.

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
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df`, already on the
  host (its `sh` and `env` are busybox). **Never pull, build, prune, stop or restart anything.** The
  host has containers and volumes of other projects: touch only workspaces whose `agentsdk.tenant`
  starts with your tenant prefix, and raw containers you make yourself labelled `agentsdk-probe`.
  Report how many of either remain at the end (it must be 0). In Git Bash a path meant for inside a
  container is rewritten: use `MSYS_NO_PATHCONV=1` or PowerShell.
- **Reaping is live in every Runner you start,** and example 36 kills a real child `Runner` process.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3. Check
  that no other python or node process is running first. Run probes with `-p no:cacheprovider`.
  **Do not kill a gate mid-run.**
- **The gates run `FakeProvider` only:** `pytest.ini` deselects tests marked `docker`. M43's own
  Docker tests: `pytest tests/test_compute.py -m docker -p no:cacheprovider`, **about 75 minutes**
  (the six cancel-layer tests run example 36 on Docker nine times). The measurement tests alone:
  `-m docker -k "not layer and not example_37"`, about 2 minutes.
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte for
  byte, verified by SHA-256. **Check that no repository file changed by content, never by mtime.**
- **Timings:** the full suite takes about 31 minutes; `tests/test_compute.py` about 30 s without
  Docker.

**Gates:** `genesis gate . M43-compute --timeout 3600000` in the background. Both gates record the
content hash, which includes `.pytest_cache`: after a run with a failure, the next run's two gates
can record different hashes and one reads stale.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `workspace_commands`, then `workspaces`, then `interruption_decisions`,
`interruptions` and `standing_decisions`, then `artifacts` by `source_run`, `plan_node_states`,
`plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs, children first, by
run id. Use tenant ids starting `SYN-m43r1`, and remove only rows of runs your probes recorded.

## Task under review

### What M43 builds

- **FR-155's five items:**
  - Example 36 judges each timeout by its own probe's `max_command_seconds` (`command_seconds`,
    `overran`), and the stale comment is corrected.
  - Example 35's `report` has no SKIP.
  - Example 36's memory check requires a `WorkspaceCommandEnded` to judge.
  - The tools' deadline counts two size reads.
  - The cancel layers are split one at a time (`CANCEL_LAYERS`, `CANCEL_ALONE`).
- **Measurement:**
  - `agentsdk/workspace/docker.py`: `WRAPPER`, `MARK`, `reading`, `_Kept.tail` and `take_reading`.
  - `agentsdk/workspace/__init__.py`: `charged`, `CommandOutcome.cpu_seconds`, `FakeCommand.cpu`.
- **Charging:** `agentsdk/workspace/manager.py` (the run's account: asked before a command, charged
  after its record, the event's `cpu_seconds` and `cost_usd`, the ceilings on cancel and provider
  error) and `agentsdk/api.py` (`_Compute`, the manifest's price).
- **Records:**
  - `BudgetPolicy.cpu_second_usd`;
  - `CommandRecord.cpu_usec` and its PostgreSQL column;
  - migration `0023`.
- **Example 37** (`scripts/37_compute.py`) and the gate, `tests/test_compute.py`.

### Every finding so far, answered

The adversarial pre-review probed at runtime and found five. The owner's repairs are in
DECISION-0883f7bd, each with a test, and a mutant that test kills.

| Finding | Repair |
|---|---|
| **F1 (high):** an argv naming a shell builtin (`eval`, `exec`, `exit`) ran inside PID 1's shell. It printed a forged reading and left before the kill loop: a 2 s burn was charged 0.0 CPU-seconds. | The wrapper runs the argv as `env -- "$@"`, a program from PATH, as docker ran it before M43. No builtin is reachable. |
| **F2 (medium):** a wrapper line printed just before a timeout's kill stayed in the model's stdout. | The killed paths take the line off too. |
| **F3 (medium):** two timing tests flaked on Windows. | Each asserts against its command's own recorded duration. |
| **F4 (low):** the layer test never showed that any single layer is enough. | Per layer, a Docker run with the other two removed. Measured kept awake: each layer alone is enough (DECISION-d9a568e1, superseding DECISION-630a34ff). |
| **F5 (low):** example 37's "no command starts after it" could not fail. | The ceiling child asks for two burns in one turn, so only the check before a command stops the second. |
| **Round 1 (Fable 5.1, rejected):** a command printed a forged line naming exit 130, then sent SIGINT to PID 1. busybox ash, which keeps a SIGINT handler, exited before the kill loop and the reading: 2 s of CPU was charged 1 us, and the exit code reported was PID 1's. | The wrapper traps INT, QUIT, TERM, HUP, USR1 and USR2 with a no-op (handlers reset on exec, so the command keeps its own). The SDK writes a 128-bit nonce, new for each command, as stdin's first line; the wrapper reads it into an unexported variable and prints it on its line. A line without it is never a reading, and a command with no such line reports no exit code, `killed: error`, charged its ceiling. Each signal of the six is tested on Docker; the nonce is shown absent from everything a command can read. |
| **Found by the author after the pre-review:** Phase 5's Docker contract test caught the wrapper's shell exporting `SHLVL` and `PWD` to every command (P5-D6: an empty environment apart from what `WorkspaceSpec` names). | The wrapper unsets both before `env`; a command sees what it saw before M43. |

### How the author checked it

- **Tests:** `tests/test_compute.py` has 60 tests without Docker and 27 with it, all passing on a
  real daemon after round 2's repair, kept awake. Phase 5's other Docker tests (`test_workspace*.py`,
  `test_docker_provider.py`, `test_sandbox_contention.py -m docker`, M42's ten-times run included)
  pass under the final wrapper.
- **Mutation:** 37 mutants, one exact edit each, all killed by the tests they name. The list is in
  this brief's last section.
- **A test of the author's own was wrong,** and was narrowed before the rerun: its "command removal"
  layer had also stopped the volume and size-read helpers from removing themselves.

### Edits to approved tests

- `tests/test_sandbox_contention.py`: the deadline test now expects `2 * mgr.MEASURE_SECONDS`
  (FR-155).
- `tests/test_distribution.py`: `EXPECTED_EXAMPLES` gains `37_compute.py` (FR-158).
- `tests/test_budget.py`: `POLICY_FIELDS` gains `cpu_second_usd` (FR-157).
- `tests/test_docker_provider.py`: the argv after the image is now `sh -c <WRAPPER> sh <argv...>`, still
  never docker's flags (FR-156).

## Attack these first

1. **Charged less than it used (NFR-48), on Docker.** Use `DockerProvider` directly with your own
   tenant label. The pre-review found the builtin route and round 1 the SIGINT route; look for any
   other way to stop the wrapper before its line, to learn the nonce, or to make a line count without
   it. Candidates:
   - `env`'s own parsing (`NAME=value` as argv[0], `-` or `--` arguments);
   - a process that outlives `kill -9 -1`;
   - stdout tricks;
   - a reading the wrapper cannot take;
   - exit-code games.
2. **The wrapper's line read by the model,** on any path: completed, timeout, cancelled, limit,
   provider error, and output past `max_output_bytes`.
3. **Charging (FR-157, NFR-23 as amended):**
   - Do a run's `cost_usd`, its lease's spend, the governor's spend and the sum of its `ModelCalled`
     and `WorkspaceCommandEnded` costs agree in every ending, on both stores?
   - Is any command charged twice, or to another run's lease, under `scope="plan"`?
   - Can a command start once its lease is spent?
   - Does a None price or an unpriced model ever become 0?
4. **NFR-49:** a run without a price, or without a workspace: anything recorded differently from
   M42 besides the new column, `cpu_seconds` and `cost_usd`? `agent_spec_hash` unchanged?
5. **FR-155:** does each repair do what FR-155 says, and does its test fail without it? Check the
   five items against DECISION-97b301ea's wording too.
6. **Example 37,** offline and `--scripted`: can a check pass when its subject did not happen, or
   for the wrong reason?
7. **The declared rows:** is anything declared that the requirements forbid?

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M42,** and the README's M43 rows.
- **Owner's choices:** DECISION-91ec4213, DECISION-0883f7bd, DECISION-88395b13, DECISION-d9a568e1, and every earlier
  one M42's brief listed.
- **Examples read private state:** `runner._sessions_for`, `_workspace_store`, the governor through
  `config.budget_lease`, and `RecordingRunner` overrides `Runner._open`.

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
  control approve . M43-compute --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M43-compute --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict.

## Appendix: the author's mutants

- F1: the argv in PID 1's shell
- wrapper: its shell's exports left
- F2: a killed command's line left in
- F5: example 37's child burns once a turn
- charged: a killed command charged its reading
- charged: a reading past the ceiling kept
- reading: the first line, not the last
- take_reading: the line left in
- round 2: the nonce not required
- round 2: no trap
- round 2: a line without its nonce reports the container code
- round 2: the nonce not on stdin
- round 2: one nonce for every command
- tail: never kept
- wrapper: no kill loop
- docker: the reading ignored
- fake: a killed command charged what it declared
- fake: the declaration ignored
- manager: a cancelled command charged nothing
- manager: a provider failure charged nothing
- manager: no pre-check
- manager: the pre-check after the workspace
- manager: never charged
- manager: the row without its CPU
- manager: the event's CPU is the wall time
- manager: the event without its cost
- account: not in the run's cost
- account: not drawn from the lease
- manifest: the price always named
- store: the CPU not written
- policy: the price unchecked
- tools: one size read in the deadline
- example 36: overran by COMMAND_SECONDS
- example 36: the check by COMMAND_SECONDS
- example 36: the memory check judges no event
- example 35: SKIP again
- example 35: never failed
