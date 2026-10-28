You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Sonnet 5** if Opus 5 fails to
start. M40 was implemented by Opus 5.5 and its pre-review run by a Fable 5.1 subagent; M39 was
approved by Sonnet 5. Bring no memory of an earlier review into this one: if your session holds
any, say so and ask for a fresh one.

M40 is **Phase 5's third milestone**: timeouts and cancellation. Re-derive the requirements from
SPEC.md, section "### Phase 5, increment 1":
- the decisions table P5-D1 to P5-D15, P5-D6 as amended at M40's pre-flight;
- "#### M40": FR-148 and FR-149, as amended at M40's pre-flight and pre-review;
- FR-137's `cpus`, FR-138's protocol (`list`'s errors), FR-139's fake size and FR-142's command
  container, each as amended at M40's pre-flight;
- NFR-42 and NFR-44, and AC-97 and AC-98 (as amended).

Read the records:
- KNOWLEDGE-ddac8822, DECISION-bf71f0f2: M39's approval and the six items it carried into M40;
- DECISION-94bfee7c: the owner's standing rule that **no check keys on an error message's text**;
- KNOWLEDGE-e011b0c9, DECISION-d4ede901: what FR-148 already had at pre-flight, `--log-driver none`
  measured, and the owner's four decisions (no daemon log; `cpus` in hundredths; a failed kill on
  the event as `kill_error`; the fake's size as `du` counts);
- KNOWLEDGE-92de4ac9, DECISION-fe7243f1: the pre-review's findings, and the owner's decision that a
  cancelled command is recorded without a size reading, the next use reading it first.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `8fdc553` (M39). M40 is uncommitted, with SPEC.md's M40 amendments. No
migration is added.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11, 16 CPUs. **This laptop enters Modern Standby on battery
  when idle,** which suspends a gate mid-run; run long commands under a wrapper holding
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is running** (engine 29.6.2 in WSL2). The tests and examples use
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df`, and one test
  `redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2` (it declares
  `VOLUME /data`), both already on the host. **Never pull, build, prune, stop or restart anything.**
  Other projects' images, containers and volumes are on this host, among them two dangling
  anonymous volumes from January 2026: touch only workspaces whose `agentsdk.tenant` label starts
  with your tenant prefix, and raw containers or volumes you make yourself labelled
  `agentsdk-probe`. Report how many of either remain at the end (it must be 0). In Git Bash a
  path meant for inside a container is rewritten: use `MSYS_NO_PATHCONV=1` or PowerShell.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **The gates run `FakeProvider` only:** `pytest.ini` deselects tests marked `docker`. Run those
  live with `pytest tests/test_workspace_tools.py tests/test_docker_provider.py -m docker
  -p no:cacheprovider` (about three minutes); AC-97 and AC-98 are held there.
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256. **Check that no repository file changed by content, never by
  mtime.**
- **Timings:** the full suite takes over 25 minutes; `tests/test_workspace_tools.py` about 40
  seconds without Docker (one test waits out the 10 s kill bound); a Docker command about 0.5 s.

**Gates:** `genesis gate . M40-workspace-tools --timeout 2400000` in the background. The suite now
runs past 25 minutes: at `--timeout 1500000` the regression gate is killed (SIGTERM, recorded as a
failure) at about 95%.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `workspace_commands`, then `workspaces`, then `interruption_decisions`,
`interruptions` and `standing_decisions`, then `artifacts` by `source_run`, `plan_node_states`,
`plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs, children first, by
run id. Use tenant ids starting `SYN-m40r1`, and remove only rows of runs your probes recorded.

## Task under review

**M40-workspace-tools**, round 1, risk **high**.

### What M40 builds

**1. Cancellation kills through the provider (FR-148),** `agentsdk/workspace/manager.py`.
`RunWorkspace.run` runs the provider's `run` as its own task and awaits it shielded. When the call
is cancelled, `_cancelled` calls `provider.kill(handle, command_id)` and then waits for the
command's task, both within `_KILL_SECONDS` (10 s) together, cancelling the task if it has not
ended. It records the command `killed="cancelled"`, with `kill_error` -- the class of what `kill`
raised, or `TimeoutError` -- on `WorkspaceCommandEnded` (always present, None when the kill
returned). It does not read the workspace's size: it marks the workspace stale, and the next
command or write reads the size first in `_refuse_if_full`, failing closed (DECISION-fe7243f1).

**2. A kill that says whether it worked,** `agentsdk/workspace/docker.py`. `DockerProvider.kill`
sets the command's kill event and runs `docker rm -fv` by name; a non-zero exit while the container
still exists (`_alive`, by state, not by message) raises `WorkspaceUnavailable`. The command's own
`_contained` still removes its container on every path, as in M39.

**3. M39's carried items (DECISION-bf71f0f2, DECISION-d4ede901):**
- `WorkspaceSpec.cpus` from 0.01 to 16 in whole hundredths, refused otherwise; passed as `%.2f`.
- Every command container gets `--log-driver none`.
- Every container removal is `rm -fv`, so an image's declared volume goes with it.
- `FakeProvider.size` counts 4 KiB per folder and each file in whole 4 KiB blocks, the root apart.
- `DockerProvider.list` raises `PermissionError` (the script's exit 22 or 23; it now also exits 22
  for a folder it can enter but not read) and `WorkspaceUnavailable` for any other failure; a
  failed `du` reading raises `WorkspaceUnavailable`. No bare `RuntimeError` remains in either.
- A test pins that example 32's refusal check reads the error's class: it swaps the executor's
  `describe_exception` so a result's text holds no class name.

**4. Example 34 (FR-149):** an agent writes `src/squares.py`, runs it with `python`, reads back
`out.txt`; the operator approves each command but refuses `rm`; `sleep 30` with a 2 s timeout is
killed and the model told; then a second run is cancelled mid-command and reports cancelled within
10 s, its command recorded cancelled, no container or workspace of it left. `--offline` runs on
`FakeProvider` (a stand-in `python` that gives that one program's effect and refuses any other);
`--scripted` on Docker with a scripted model; live with the gateway model.

**Choices the author made that the SPEC leaves open** (judge each):
- `kill_error` is on every `WorkspaceCommandEnded` payload, None unless a cancellation's kill
  failed; a timeout's own removal is not reported by it.
- A cancelled command's row carries none of its output, even when the command had printed some
  before the kill (pre-review F6).
- The cancel path's 10 s covers the kill and the wait for the command together; the run's removal
  of its workspace keeps its 30 s bound (declared).
- `cpus` accepts a float within 1e-9 of a hundredth and records it as given (pre-review F7).
- NFR-44's `aclose()` and reaping clauses are FR-151's, M41.

### Every finding so far, answered

A missing or false row is itself a finding.

| Source | Finding | Answer |
|---|---|---|
| M39 review, caveat 1 | A command's output flood grew the daemon's json-file log for its whole life | Owner: `--log-driver none`, P5-D6 amended (DECISION-d4ede901). `test_a_command_container_keeps_no_log_on_the_daemon`; `exactly_the_p5_d6` edited |
| M39 review, caveat 2 | `rm -f` left an image's declared VOLUME, one per command | `rm -fv` everywhere. `test_every_container_is_removed_with_the_volumes_its_image_declares`; live `test_an_images_declared_volume_goes_with_its_command` |
| M39 review, caveat 3 | `cpus=1e-06` reached docker in scientific notation and gave no CPU limit; `1/3` failed every create | Owner: hundredths, `%.2f` (DECISION-d4ede901). `test_cpus_outside_whole_hundredths_or_its_range_is_refused`, `test_cpus_in_whole_hundredths_reach_docker_as_fixed_decimals` |
| M39 review, caveat 7 | Example 32's refusal check had no test that failed when it read text | `test_example_32_judges_the_refused_bound_by_its_error_class_not_its_text` |
| M39 review, owner's addition | `size()` meant different things on the two providers | Owner: the fake counts as `du` does (DECISION-d4ede901). Contract case `a_workspaces_size_counts_its_folders_as_du_does`; `test_the_fake_counts_a_workspaces_size_as_du_does` |
| M39 review, owner's addition | An unreadable folder's listing raised a bare `RuntimeError` | `PermissionError` / `WorkspaceUnavailable`. `test_a_listing_that_fails_raises_a_documented_error`, `test_a_disk_reading_that_fails_raises_workspace_unavailable`; live `test_a_folder_the_measurement_cannot_read_refuses_the_next_command` edited |
| M38 review, smaller | An approver approves a `run_command` a full workspace then refuses; `_end` awaits creation unbounded | Not changed: the refusal is the workspace's answer to an approved call, recorded with its bound. `_end`'s wait for a creation under way has no bound of its own; on Docker each call a creation makes is bounded (60 s), so the wait is bounded by their sum |
| Pre-flight | A cancelled command on Docker waited on `rm -f` and the client up to 120 s | The manager's 10 s kill. `test_a_kill_that_never_returns_is_bounded_at_10_s_and_recorded` |
| Pre-flight | A failed kill was recorded nowhere | Owner: `kill_error` on the event (DECISION-d4ede901). `test_a_kill_that_fails_is_recorded_and_the_run_still_reports_cancelled`, both stores; live `test_a_docker_kill_that_fails_is_recorded_and_the_container_still_goes` |
| Pre-flight | Nothing tested a process ignoring signals that left its group | Live `test_a_cancelled_run_reports_cancelled_only_once_its_stubborn_command_is_gone`, both stores; `test_a_stubborn_command_past_its_timeout_is_gone_when_its_outcome_returns` |
| Pre-review F1 | After the kill, the size was read (up to 120 s) before the run reported cancelled: 20.9 s with the `du` container held 20 s | Owner: no reading on the cancel path, the next use reads first (DECISION-fe7243f1). `test_a_cancellation_reads_no_size_so_a_slow_reading_cannot_hold_it`, `test_after_a_cancelled_command_the_next_use_reads_the_size_first_and_fails_closed` |
| Pre-review F2 | A folder with search but no read permission listed as empty | `_LIST` exits 22 when it cannot read the folder. Live `test_a_folder_that_can_be_entered_but_not_read_is_refused_not_listed_empty` |
| Pre-review F3 | A folder under an unreadable parent raises `FileNotFoundError` | Declared (README) |
| Pre-review F4 | `cpus` above the host's CPU count fails every create, named as the image unable to measure | Declared (README) |
| Pre-review F5 | The fake's size still differs: a folder's own growth, sparse files | Declared (README); the contract's lower bound holds on both |
| Pre-review F6 | A cancellation landing as the command ends records it cancelled with no output | Declared (README) |
| Pre-review F7 | `cpus` within 1e-9 of a hundredth is accepted and recorded unrounded | Noted: docker receives the rounded value |
| Pre-review F8 | Example 34 live turns an unexercised check into a SKIP | Noted: as example 33; offline modes fail on it |
| Pre-review F9 | The kill's `rm` and the command's own race | Noted: 0 of 10 spurious `kill_error`; decided by `_alive` |

### How the author checked it

- **Tests first:** `tests/test_workspace_tools.py` was written before the code; 33 of its 40 gate
  tests failed against M39's code.
- **Mutation:** 29 of 29 killed by named tests, each file restored by SHA-256: 25 against the
  first build (among them M39's code paths the carried items changed), and 4 that revert each
  pre-review repair; the 25 were run again on the final code.
- **Gates,** on source hash `b2fc39c2`:
  - `unit`: 43 passed, 9 deselected (the docker-marked tests) in 30.41s;
  - `regression`: 2818 passed, 36 deselected in 1557.71s.

  A first run at `--timeout 1500000` was killed at about 95% with no failure; it left no row,
  container or process. The gates were run again at `--timeout 2400000`.
- **Docker, live, on the final code:** `pytest tests/test_workspace_tools.py
  tests/test_docker_provider.py -m docker`: 36 passed in 188.48s -- M39's 26 (the contract's new
  case included) and M40's 10: the tools and their approval on both stores, a stubborn command past
  its timeout, a cancelled run with a stubborn command on both stores, a failing kill, a declared
  volume, an unreadable folder, and example 34 scripted on both stores.
- **Live examples,** against the gateway model on PostgreSQL and Docker, after the gates:
  - example 34: 10 of 10 PASS. The model ran the whole task (it issued the three commands in one
    turn); the cancelled run reported cancelled 0.36 s after the cancel;
  - example 33: 10 of 10 PASS; example 32: 4 of 4 PASS.
  - **Cleanup:** the four runs' rows removed by their printed run ids, their artifacts counted
    first (0). The store is at the baseline: 10351 runs, 19 under example-tenant, 0 SYN, none
    running, 0 artifacts, empty catalog, interruption and workspace tables. No container or volume
    labelled `agentsdk.workspace` remains; the host holds the same 3 volumes as before.

### Edits to approved tests

- **`tests/workspace_contract.py`:** a new case, `a_workspaces_size_counts_its_folders_as_du_does`
  (DECISION-d4ede901). No existing case is changed.
- **`tests/test_docker_provider.py`:**
  - `test_a_command_container_gets_exactly_the_p5_d6_limits_and_the_argv_after_the_image`:
    `--cpus` expects `0.50` (was `0.5`), and `--log-driver none` follows it;
  - `test_a_folder_the_measurement_cannot_read_refuses_the_next_command`: `unread` expects
    `WorkspaceUnavailable` (was `RuntimeError`).
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `34_sandboxed_tools.py`.
- No other approved test is edited. NFR-16 and NFR-38: M40 adds no table, so `tests/conftest.py`'s
  `_TRACKED` is unchanged.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m40-r1-probes` in your own scratchpad.

0. **The table above:** check every row is true, and every approved-test edit by content.
1. **FR-148, cancellation:** a container outliving a cancelled run; a run reporting cancelled
   before its command's container is gone, or more than 10 s after the cancel on a healthy daemon;
   races between the cancel and `docker create`, `start`, the command's own end, and the
   workspace's removal.
2. **FR-148, timeout:** a command past its timeout whose container outlives its outcome, or whose
   timeout the model does not read as its result.
3. **Stubborn processes:** anything a command starts -- ignoring signals, forking, leaving its
   group, writing the workspace -- that survives its container or acts after its kill.
4. **`kill_error`:** None while a container survived, set while it was removed, or carrying a
   message or an env value rather than a class name.
5. **The deferred reading:** a command or write that runs after a cancelled command without the
   size being read first; a reading that fails and does not refuse; a stale mark that leaks to a
   workspace it does not belong to.
6. **The carried items:** a `cpus` value that reaches docker as anything but its spec's limit
   (read the cgroup from inside a command); a daemon-side copy of a command's output; an
   anonymous volume left behind; a listing or reading that fails with a type outside the protocol.
7. **Failure honesty:** a check anywhere in M40 keyed on an error's text (DECISION-94bfee7c); a
   check in example 34 that passes when its subject never happened, offline.
8. **The contract:** every case on both providers; a behaviour the two providers differ on that
   neither the contract nor the README names.
9. **Regressions:** the full suite; examples 32 and 33 offline; example 34 `--offline`,
   `--scripted` and live.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M39,** and the README's M40 rows.
- **Owner's choices:** DECISION-31b2c79d, DECISION-47158c90, DECISION-97fb116b, DECISION-bb81ebbe,
  DECISION-504c7f04, DECISION-0004d2dc, DECISION-94bfee7c, DECISION-bf71f0f2, DECISION-d4ede901,
  DECISION-fe7243f1.
- **M41 and M42's requirements are not M40's:** `export_workspace_file`; plan and kept workspaces,
  `max_concurrent_workspaces`, `Runner.aclose()` removing workspaces of runs under way, reaping a
  dead owner's (FR-150 to FR-153); the exit signal.
- **Examples read private state** as earlier ones do: `runner._sessions_for` for histories.

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
  control approve . M40-workspace-tools --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M40-workspace-tools --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the ten attack items, and
your verdict.
