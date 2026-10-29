You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Sonnet 5** if Opus 5 fails to
start. M41 was implemented by Opus 5.5 and its pre-review run by a Fable 5.1 subagent; M40 was
approved by Sonnet 5. Bring no memory of an earlier review into this one: if your session holds
any, say so and ask for a fresh one.

M41 is **Phase 5's fourth milestone**: files out of a workspace as artifacts, workspace lifetime,
reaping, the concurrency bound and plans. Re-derive the requirements from SPEC.md, section
"### Phase 5, increment 1":
- the decisions table P5-D1 to P5-D15, P5-D11 and P5-D12 above all;
- "#### M41": FR-150 to FR-153, as amended at M41's pre-flight and pre-review;
- FR-139 (the fake's transfers), FR-140 (migration `0020`) and FR-148 ("A provider that fails"),
  each as amended in M41;
- NFR-22, NFR-42 and NFR-44, and AC-99.

Read the records:
- KNOWLEDGE-fed41aae, DECISION-89f2b384: M40's approval, and what it carried into M41 -- its
  caveats 1 to 3, and **contract parity between `FakeProvider` and `DockerProvider` as the first
  thing to hunt**;
- DECISION-94bfee7c: the owner's standing rule that **no check keys on an error message's text**;
- KNOWLEDGE-87741d74, DECISION-825b81a6: what M38 to M40 left for M41, process start times measured,
  and the owner's four decisions (the command id in the artifact's provenance; the owner label
  `hostname:pid:start`; a provider failure recorded `killed='error'` through migration `0020`;
  M40's caveats 1 and 3 declared);
- KNOWLEDGE-46ddc562, DECISION-b1498111: the pre-review's findings and the owner's repairs;
- DECISION-f4e7dd24: the gate timeout, now 3600000 ms.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `04f83f6` (M40). M41 is uncommitted, with SPEC.md's and README.md's M41
amendments and migration `agentsdk/migrations/0020_command_errors.sql`, **already applied to the
dev database** (it widens `workspace_commands.killed`'s CHECK; `0019` is untouched).

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11, 16 CPUs. **This laptop sleeps and throttles when idle,**
  which stalls a gate mid-run (twice in M41: a 40-minute timer fired after 64 and 51 minutes of
  wall clock). Run long commands under a wrapper holding
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is running** (engine 29.6.2 in WSL2). The tests and examples use
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df`, and one test
  `redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2`, both already on
  the host. **Never pull, build, prune, stop or restart anything.** Other projects' images,
  containers and volumes are on this host, among them two dangling anonymous volumes from January
  2026 (3 volumes in all before you start): touch only workspaces whose `agentsdk.tenant` label
  starts with your tenant prefix, and raw containers or volumes you make yourself labelled
  `agentsdk-probe`. Report how many of either remain at the end (it must be 0). In Git Bash a path
  meant for inside a container is rewritten: use `MSYS_NO_PATHCONV=1` or PowerShell.
- **Reaping is live in every Runner you start.** Before its first workspace, a Runner removes
  workspaces whose `agentsdk.owner` names this host and a process that is provably gone. A label you
  forge with a dead owner is removed by the next Runner of any test or probe, yours or not; make
  forged owners with your own tenant prefix, and expect them gone.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **The gates run `FakeProvider` only:** `pytest.ini` deselects tests marked `docker`. Run those
  live with `pytest tests/test_workspace_plan.py tests/test_workspace_tools.py
  tests/test_docker_provider.py tests/test_workspace.py -m docker -p no:cacheprovider` (about 7
  minutes on a warm daemon; 11 to 20 just after Docker Desktop starts).
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256. **Check that no repository file changed by content, never by
  mtime.**
- **Timings:** the full suite takes about 29 to 32 minutes; `tests/test_workspace_plan.py` about 80
  seconds without Docker.

**Gates:** `genesis gate . M41-workspace-plans --timeout 3600000` in the background
(DECISION-f4e7dd24). Both gates record the content hash, which includes `.pytest_cache`: after a run
with a failure, the next run's two gates can record different hashes and one reads stale.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `workspace_commands`, then `workspaces`, then `interruption_decisions`,
`interruptions` and `standing_decisions`, then `artifacts` by `source_run`, `plan_node_states`,
`plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs, children first, by
run id. A plan's child runs own artifacts too: find them by `parent_run_id`. Use tenant ids starting
`SYN-m41r1`, and remove only rows of runs your probes recorded.

## Task under review

### What M41 builds

- **`export_workspace_file(path, mime_type)`** (FR-150), a fifth workspace tool, `WRITE`: the file,
  read through the provider's export and `one_file`, stored through the run's `ToolScope.artifacts`
  (so `source_run` and `source_task` are the run's and node's), with NFR-42's labels and a
  provenance source `workspace://<workspace_id>/<path>?command=<id>` -- the path normalised and
  percent-encoded, the last command that *ended* in the workspace before the export began, no
  `command` part when none had. `mime_type` is `type/subtype` only. A folder, whichever way its
  provider packs it, is refused, by `read_workspace_file` too.
- **Lifetime** (FR-151, `agentsdk/workspace/manager.py`): a workspace is a `_Space`, bound per run
  by a `RunWorkspace`. A `"run"` workspace is removed when its run ends; a `"plan"` one when its
  top-level run ends (`WorkspaceManager.end_plans`, called from `Runner._end_workspace`); a kept one
  (`keep=True`) at `Runner.release_workspace(tenant, project, workspace_id) -> bool` or `aclose()`.
  `Runner.aclose()` now also calls `WorkspaceManager.aclose()`: every held workspace removed,
  recorded `aclose`, then whatever the provider still lists under this Runner's `agentsdk.runner`
  label; no creation after.
- **Owner and reaping** (`agentsdk/workspace/owner.py`): `agentsdk.owner` is `hostname:pid:start`;
  `is_dead` is True only for this host and a process gone, exited, or started at another time.
  Before a Runner's first workspace it lists every SDK workspace and removes the dead owners' --
  each in isolation, retried before the next creation if the listing fails -- recording `reaped`
  by the labels' tenant and project, and emitting nothing on any run's stream.
- **The bound** (FR-152): `_Slots`, thread-safe, one waiter per loop future. A run beyond
  `max_concurrent_workspaces` waits, cancellably, and `WorkspaceWaiting` (`limit`, `held`) is
  emitted from its own task. Each wait names its workspace: when one of a plan's children begins
  creating a shared workspace, its waiting siblings are released without a slot.
- **Plans** (FR-152): plan children share a workspace keyed by tenant, project, top-level run and
  spec (env included), recorded under the top-level run. The top-level run is the child's
  `ledger_run_id` only if that run is under way on this Runner in the same tenancy, and has not yet
  ended its plan; otherwise the run is its own top.
- **M40's caveat 2** (FR-148): a provider `run` that raises is recorded `killed='error'`, no exit
  code, its class as `kill_error`, the workspace marked to read its size first; the provider's
  error still reaches the tool.
- **Parity** (FR-139, FR-151): `FakeProvider`'s import and export go through `DockerProvider`'s
  `read_tar`; `DockerProvider.list_owned` also lists a labelled container whose volume is gone.
- **`scripts/35_workspace_plan.py`** (FR-153): two analysts sharing a plan workspace, a writer with
  its own, exports, removal at the plan's end, a kept workspace released, `aclose()`.

### Every finding so far, answered

A missing or false row here is itself a finding.

| Source | Finding | Answer | Pinned by (`tests/test_workspace_plan.py` unless named) |
|---|---|---|---|
| M40 caveat 2 | provider `run` raising leaves Started with no Ended or row | fixed: `killed='error'`, migration 0020 | `test_a_provider_run_that_raises_is_ended_and_recorded_as_an_error`, `..._the_next_use_reads_the_size_first` |
| M40 caveat 1 | a cancel absorbed by the store while Started is written | declared (README, DECISION-825b81a6) | -- |
| M40 caveat 3 | `RuntimeError` instead of the cancel when the record fails | declared (README, DECISION-825b81a6) | -- |
| M40 coverage | fake versus Docker parity unprobed | hunted at pre-review (finding 8 below); yours to hunt again | -- |
| Pre-flight | a folder holding one file read and exported as that file (since M38; fake packs a folder as `.`) | fixed in `one_file` | `test_a_folder_holding_one_file_is_neither_read_nor_exported_as_that_file` |
| Pre-review 1 | a plan's children deadlock behind one free slot | fixed: siblings released; a second look after the reaping | `test_a_plans_children_behind_one_free_slot_...`, `..._queued_behind_a_held_slot_...`, `..._arriving_during_the_first_reaping_...` |
| Pre-review 2 | a wait cancelled after its grant, while recorded, leaks its slot | fixed | `test_a_wait_cancelled_after_its_grant_while_it_is_recorded_gives_the_slot_back` |
| Pre-review 3 | a run naming an ended or never-run top-level run gets a plan workspace nothing ends | fixed: it is its own top | `test_a_run_naming_a_top_level_run_not_under_way_is_its_own_plans_top`, `test_a_plan_whose_top_level_run_ended_its_workspaces_is_never_joined_again` |
| Pre-review 4 | `is_dead` raised on a `²` pid and one such label stopped all reaping | fixed: ASCII digits; each workspace reaped in isolation | `test_an_owner_label_whose_pid_is_not_ascii_digits_is_alive`, `test_one_unreadable_owner_label_does_not_stop_reaping_the_rest` |
| Pre-review 5 | a provider raising `CancelledError` uncancelled ends the run cancelled | declared (README) | -- |
| Pre-review 6 | `mime_type` admitted parameters the store refuses | fixed | `test_an_export_mime_type_with_parameters_is_refused_by_the_schema` |
| Pre-review 7 | `one_file` named every non-single-file stream a folder | fixed, message only (class unchanged) | -- |
| Pre-review 8 | fake accepts duplicates, a file under a file, exports past Docker's bounds | fixed: the fake calls `read_tar` | `test_the_fake_refuses_the_transfers_docker_refuses` |
| Pre-review 8 | Docker's listing missed a labelled container whose volume is gone | fixed | `test_on_docker_a_dead_owners_volume_and_container_are_reaped` (docker) |
| Pre-review 8 | the docker reaping test mounted `agentsdk-ws-<id>`, not the SDK's `agentsdk-<id>` | fixed | same test |

### How the author checked it

- **Tests first:** `tests/test_workspace_plan.py` was written before the code (33 of its first 39
  failed on M40's code; the rest held already); each pre-review repair's test failed before its
  repair (10 of 11; the two-slot case never deadlocked).
- **Mutation:** 38 of 38 killed by named tests, each file restored by SHA-256, on the final code --
  including each half of the deadlock repair separately, and, live on Docker, the container listing.
  Five survivors of the first pass were answered: four by sharper tests, one by removing a
  redundant check after `create` (what `aclose()` already covers).
- **Gates,** on source hash `728ea046`, at `--timeout 3600000`:
  - `unit`: 57 passed, 8 deselected (the docker-marked tests) in 76.16s;
  - `regression`: 2883 passed, 46 deselected in 1721.23s.
  Earlier runs on the same code: one killed at 64 minutes of wall clock for a 40-minute timer (the
  machine slept), one with two MCP failures under load (a temp port file refused, a fixture server
  late; both pass alone), one passing in 30:44, one killed while Docker Desktop's VM booted.
- **Docker, live, on the final code:** the command above: 46 passed in 6:49. Two earlier runs, just
  after Docker Desktop started, each failed once in example 34's scripted Docker test (M40's): a
  workspace volume left behind once, and once a 2 s command timeout that took over 150 s to end, so
  the executor raised `ToolTimeout`. See the declared items.
- **Live examples,** against the gateway model on PostgreSQL and Docker: example 35 all 7 live
  checks PASS (offline and scripted 8 of 8). Its rows were removed by its two printed run ids and
  their three children (5 runs, 5 artifacts counted first). The store is at the baseline: 10351
  runs, 19 under example-tenant, 0 SYN, none running, 0 artifacts, 0 workspaces, 0 commands, 0
  interruptions, 2 plan versions. No `agentsdk.workspace`-labelled container or volume remains.

### Edits to approved tests

- **`tests/test_workspace.py`:** M38's `test_a_scope_or_keep_m41_brings_is_refused_until_then`
  became `test_a_scope_or_keep_m41_brings_is_served`: a plan or kept workspace's run completes and
  its model is called, where it was refused before its first call.
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `35_workspace_plan.py`.
- No other approved test changed; no new table, so `tests/conftest.py`'s tracked tables are
  unchanged (NFR-16, NFR-38).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m41-r1-probes` in your own scratchpad.

0. **The table above:** check every row is true, and every approved-test edit by content.
1. **Contract parity** (carried from M40, still first): every behaviour `FakeProvider` and
   `DockerProvider` differ on that the contract, the SPEC or the README does not name -- export,
   import, `list_owned`, `remove`, labels on volumes versus containers, a container without its
   volume, a volume without its containers. Two milestones running, this is where real defects hid.
2. **The slot bound:** `held` leaking or going negative on any path; a run waiting for ever with
   no workspace held; a wait that cannot be cancelled; races between a plan's siblings, the first
   reaping, a cancel, `aclose()` and `release_workspace`. Check `runner._workspaces.slots.held`
   against what is really held after each.
3. **Plan workspaces:** removed exactly when the top-level run ends; any child reaching another
   plan's, another spec's or another tenancy's; a child of a child; a cancelled plan; `keep=True`
   with `scope="plan"`; a late or forged `ledger_run_id`.
4. **Reaping and the owner:** anything removed whose owner may be alive, of another host, or
   unlabelled; a dead owner's container or volume left on Docker after reaping (NFR-44); another
   workspace's id on a run's events; a reaping failure failing a run.
5. **`aclose()`:** anything the Runner created remaining (NFR-44), on Docker; a creation after it;
   another Runner of the same process losing its workspaces; a run under way left hanging.
6. **Export:** an artifact without NFR-42's labels or with a wrong source or command id (under a
   plan, with commands running concurrently too); a link, FIFO, folder or oversized file leaving a
   workspace on either provider; a cancel during the export.
7. **M40's caveat 2:** a provider failure without exactly one Ended and one `killed='error'` row on
   both stores; migration `0020` on a database at `0019`.
8. **Failure honesty:** a check anywhere in M41 keyed on an error's text (DECISION-94bfee7c); a
   check in example 35 that passes when its subject never happened, offline.
9. **Regressions:** the full suite; examples 32 to 34 offline; example 35 `--offline`,
   `--scripted` and live.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M40,** and the README's M41 rows.
- **Owner's choices:** DECISION-31b2c79d, DECISION-47158c90, DECISION-97fb116b, DECISION-bb81ebbe,
  DECISION-504c7f04, DECISION-0004d2dc, DECISION-94bfee7c, DECISION-d4ede901, DECISION-fe7243f1,
  DECISION-f3e99c07, DECISION-89f2b384, DECISION-825b81a6, DECISION-b1498111, DECISION-f4e7dd24.
- **A plan workspace is per spec:** children whose `WorkspaceSpec` differs, env included, get
  separate plan workspaces (FR-152 as amended; the author's design, written into the SPEC for the
  owner to ratify).
- **M39/M40's timeout path on Docker** can wait about 240 s after a command's own timeout (an `rm`
  and two 60 s waits for the attached client, then another `rm`), past the executor's
  `max_command_seconds + 30` deadline; on a stalled daemon a timed-out command then ends as
  `ToolTimeout`. Observed twice just after Docker Desktop started; M41 does not touch that code.
  Report it if you find it reachable on a healthy daemon.
- **M42's requirements are not M41's:** the exit signal (FR-154, AC-100).
- **The SPEC's M41 amendments await the owner's ratification** at close (the approval names
  SPEC.md's hash before them).
- **Examples read private state** as earlier ones do: `runner._sessions_for`,
  `runner._artifacts_for`, `runner._workspace_store`.

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
  control approve . M41-workspace-plans --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M41-workspace-plans --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the ten attack items, and
your verdict.
