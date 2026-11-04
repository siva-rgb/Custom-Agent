You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M46, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M46 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M46 is **snapshots**: the application copies a run's or a plan's workspace, between commands, into
a snapshot of the same tenancy, and a new workspace can start as that snapshot's copy -- measured,
recorded, refused past its bounds, released, kept past `aclose()` only for a kept workspace, and
reaped with a dead owner.

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table (P5-D22, P5-D23)

| Decision | P5-D16 to P5-D25 |
|---|---|
| P5-D22 Snapshots | `Runner.snapshot_workspace(...)` copies a workspace's volume into a snapshot volume inside Docker; `WorkspaceSpec(from_snapshot=...)` starts a workspace from one of the same tenancy. A snapshot counts against `max_workspace_bytes`, is recorded, released by `Runner.release_snapshot` and reaped with its owner. The application takes snapshots; no model tool does in this increment |
| P5-D23 Records | One migration per milestone (DECISION-91ec4213): `0023` (M43) `workspace_commands.cpu_usec`; `0024` (M44) `workspace_connections` (each egress connection, allowed or refused, with its reason), and `0025` (M44) its one `unrecorded` row per workspace, a second because `0024` was already applied when its review found the index missing (DECISION-7a3a2ae2); `0026` (M45) `workspaces` scope `server`, its `run_id` NULL for that scope only (DECISION-2a12196e), and `workspace_connections.run_id` NULL for a server workspace's connections only (DECISION-61aed587); `0027` (M46) `workspace_snapshots`. A sandboxed server's workspace is a `workspaces` row of scope `server`. The manifest records each workspace's `egress` and each sandboxed server's spec |

### "#### M46: snapshots"

- FR-168: `Runner.snapshot_workspace(tenant_id, project_id, workspace_id) -> SnapshotRef` copies the workspace's volume, between commands, into a snapshot volume by a hardened container over both (P5-D22). The snapshot is measured, recorded in `workspace_snapshots`, labelled as its workspace is, and refused with `WorkspaceRefused` past `max_workspace_bytes`. `Runner.release_snapshot` removes one; `aclose()` keeps it unless its workspace was not kept; reaping removes a dead owner's.
- FR-169: `WorkspaceSpec.from_snapshot` names a snapshot of the same tenant and project: the new workspace starts as its copy. Another tenancy's, a released one, or one whose size passes the new spec's bound fails the workspace `WorkspaceUnavailable`. The manifest records the snapshot's id.
- FR-170: Example 40 (`scripts/40_snapshots.py`): a workspace built up, snapshotted, changed, and a second workspace started from the snapshot reads the state at the snapshot; offline and on Docker.

### Non-functional

- NFR-47: Nothing leaves a workspace except a tool result, an exported artifact, a snapshot inside Docker, or a connection the proxy allowed to a pair its spec names (amends NFR-43). No connection reaches a non-global address, another workspace or the host, from a command or a sandboxed server.
- NFR-49: Runs whose workspaces have no `egress`, no snapshot and no sandboxed server send the same requests and record the same events and rows as before M43, apart from the new columns, recorded empty, and `cpu_seconds`; their `agent_spec_hash` is unchanged.

### Acceptance

- AC-104: FR-168 to FR-170, on both stores: a snapshot restores the state at its taking, links kept inside Docker; another tenancy's, a released one and one past the bound are refused; nothing of a snapshot remains after its release or reaping.

## The records

The owner settled M46 in these decisions, all in `.genesis/project.json`. Read them: a choice they
record is not a defect, but code that differs from them is.
- **DECISION-bafa7be8** (the pre-flight):
  - a kept workspace's snapshot survives `aclose()`, and any Runner of the same tenant, project and
    provider may start from it or release it; one of a workspace not kept goes with `aclose()`;
  - a snapshot is refused `WorkspaceRefused` while a command or a write is under way, and one asked
    while it is taken waits for it;
  - on Docker the copy runs `cp -a` in the workspace's own digest-pinned image, hardened as a command
    is, and nothing is pulled;
  - only run and plan workspaces: a sandboxed MCP server's is refused.
- **DECISION-9da244b8** (the pre-review's findings, below, and the owner's choices on F4 and F5).
- Standing: DECISION-a00773b0, DECISION-a7d6a1f5, KNOWLEDGE-1afcf6b2 (no vacuous check),
  DECISION-94bfee7c (no check keys on an error's text), DECISION-91ec4213 (one migration per milestone).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `60169f4` (M45). M46 is uncommitted, with README.md's M46 row, and migration
`agentsdk/migrations/0027_workspace_snapshots.sql`, **already applied to the dev database**. SPEC.md is
unchanged by M46.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** DATABASE_URL from .env. **Never print credentials or a connection string;** on a
  database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is running.** The tests use
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df` (python:3.12-alpine,
  so `cp`, `du`, `find`, `stat` and `awk` are busybox's), on the host. **Never pull, build, prune, stop
  or restart anything.** Touch only what carries your own `agentsdk.tenant` prefix, or a volume you
  named yourself. Report how many remain at the end (it must be 0). A snapshot's volume is
  `agentsdk-snap-<id>`; a workspace's `agentsdk-<id>`. In Git Bash use `MSYS_NO_PATHCONV=1` for a path
  meant for inside a container.
- **Line endings vary by file** (some LF, most CRLF). A mutant whose edit silently never applies looks
  exactly like a surviving one: assert the file changed before running its test, and restore it byte
  for byte, verified by SHA-256.

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first (a Jupyter kernel of the owner's may be: leave it). Run with `-p no:cacheprovider`. **Do not
  kill a gate mid-run.**
- **Gates:** `genesis gate . M46-snapshots --timeout 3600000` in the background, kept awake: unit
  (`tests/test_snapshots.py`), regression (the full suite, about 34 minutes), and `docker` (every
  Phase 5 Docker-marked test, about 47 minutes).
- M46's own Docker tests: `pytest tests/test_snapshots.py -m docker -p no:cacheprovider`, under 2
  minutes.

**Cleanup order:** `workspace_snapshots`, `workspace_connections`, `workspace_commands`, `workspaces`,
then `interruption_decisions`, `interruptions`, `standing_decisions`, `artifacts` by `source_run`,
`plan_node_states`, `plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs,
children first. Use tenant ids starting `SYN-m46r1`, and remove only rows your probes recorded.

## Task under review

### What M46 builds

- `Runner.snapshot_workspace(tenant_id, project_id, workspace_id) -> SnapshotRef` and
  `Runner.release_snapshot(...) -> bool` (`agentsdk/api.py`), over `WorkspaceManager.snapshot`,
  `release_snapshot`, `usable_snapshot` and `within_bound` (`agentsdk/workspace/manager.py`, the
  section "snapshots (FR-168, FR-169")`.
- **Between commands:** each `_Space` counts the commands and writes under way (`busy`) and holds a
  `concurrent.futures.Future` while a copy runs (`copying`), both under one `threading.Lock`
  (`use`), since a plan's workspace can be reached from several loops. `using()` wraps
  `RunWorkspace.run` and `import_`.
- **The providers:** `snapshot`, `remove_snapshot`, `list_snapshots`, and `create` honouring
  `WorkspaceSpec.from_snapshot`. FakeProvider keeps a copy of the tree in memory. DockerProvider makes
  a volume of its own, owns its root as a workspace's is, and runs one hardened container over both
  volumes (`_copied`, script `_COPY`): it sums apparent sizes first, then `cp -a`, then `du`. A
  restore copies into the new workspace after its empty baseline is measured, then checks the
  snapshot is still there (a mount of a removed volume makes an empty one).
- **Records:** `workspace_snapshots` (migration 0027): one row per snapshot, its removal written once
  -- `released`, `aclose` or `reaped`. Both stores.
- **Example 40** (`scripts/40_snapshots.py`), offline and on Docker, each check against a control.

### The pre-review's findings, answered (DECISION-9da244b8)

A fresh Sonnet subagent probed M46 at runtime. Each finding was reproduced by the author before it
was repaired, and each repair has a mutant in the appendix that a test kills.

| Finding | Repair |
|---|---|
| **F1 (blocking):** Docker's `--filter label=` ignores a value's trailing whitespace, so tenant `T` plus a space, tab or newline restored T's snapshot (reading its files) and released it, leaving T's row live. | Labels compared exactly in `DockerProvider.list_snapshots` and `list_owned` (`_labelled`), and again in the manager (`_snapshot_of`, `_refuse_if_server`). |
| **F2 (blocking):** a sandboxed server's spec with `from_snapshot` was opened by `open_server`, which calls `provider.create` directly: another tenancy's snapshot, past its bound too. | `server_files` refuses `from_snapshot` (`ValueError`), and `open_server` refuses it (`WorkspaceUnavailable`). |
| F3: a cancel during the record write left a live row of a removed snapshot (the write went on in its thread). | The write is shielded; a write that lands after its caller gave up is followed by its removal, recorded `aclose` as a server workspace's late record is; `aclose()` waits for it. |
| F4: busybox `cp -a` writes a sparse file out in full, so a workspace `du` counts as nearly empty made the host write gigabytes before the refusal. | **Owner:** count first. The copy sums apparent sizes of regular files, each inode once, and exits 3 before writing past the bound -- on a snapshot (`WorkspaceRefused`) and on a restore (`WorkspaceUnavailable`). |
| F5: a workspace holding what its own user cannot read, or a tree past PATH_MAX, cannot be copied. | **Owner:** declare. It fails closed `WorkspaceUnavailable`; README row. |
| F6: a failed taking whose removal also failed left a copy nothing tracked. | Kept tracked, so `aclose()` tries it again. |
| Example 40's control raised instead of failing when its writes never landed. | It reports a failed check. |

Found by the author before the pre-review: a snapshot released between Docker's check and the mount
made Docker create an empty, unlabelled volume, and the workspace started empty; the restore now
rechecks after the copy and removes that volume. And `workspace_connections` (M44) was never in
`tests/conftest.py`'s `_TRACKED`, so AC-44 never saw a leaked connection row: it is now, with
`workspace_snapshots`.

### How the author checked it

- **Tests:** `tests/test_snapshots.py`, 33 tests without Docker and 8 with it, all passing. Written before the code and run against it
  first (each failed on the missing API); later tests were added for each repair.
- **Mutation:** 49 mutants, one exact edit each, every file restored byte for byte by
  SHA-256 (appendix). One first killed only by a hang (a second snapshot unrefused waited out the
  600 s copy); its test now bounds the call.
- **Older suites:** every test file that imports the workspace package, plus `test_distribution.py`
  and `test_mcp_client.py`, 831 passed before the gates.
- **Gates** on source hash `d9200fa46cbc6a5803c60e6e672cc3abd7287bdf7b0d4e3ce731793ab69a1f67`: unit 33 passed, 8 deselected in 35.04s; regression 3260 passed, 136 deselected in 1826.04s (0:30:26); docker 136 passed, 698 deselected in 2840.11s (0:47:20), each exit 0 with empty stderr, kept awake.

### Edits to approved tests

- `tests/conftest.py`: `_TRACKED` gains `workspace_snapshots` (NFR-16, NFR-38's list) and
  `workspace_connections` (M44's table, missing since M44). No entry removed.
- `tests/test_distribution.py`: `EXPECTED_EXAMPLES` gains `40_snapshots.py` (FR-170).

Check both by content (`git diff HEAD -- tests/conftest.py tests/test_distribution.py`).

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **Tenancy.** Can another tenant or project snapshot, start from, or release a snapshot -- by any
   spelling Docker's filter or a label might match, through a plan, through a server, through a
   Runner of the same provider? Is every lookup exact?
2. **Between commands.** Can a copy and a command or write overlap -- across loops, a plan's
   children, a cancelled command still running past its kill bound, `export`/`list` (which `using()`
   does not wrap)? Can the hold leak?
3. **Bounds.** Can a snapshot exceed its workspace's `max_workspace_bytes`, or a restore its new
   spec's, unrefused -- or can either make the host write past the bound first? Hard links counted
   once by `du` and by the pre-count; sparse files; many files.
4. **Lifetime and leaks.** At every phase of a snapshot and a restore (creation, root preparation,
   pre-count, copy, measure, record), cancelled, timed out or failed: does anything remain -- volume,
   container, row, hold, trailing task? Does `aclose()` keep exactly a kept workspace's snapshots, and
   does reaping remove only a dead owner's?
5. **Records.** On both stores: one row per snapshot, its removal written once, the right reason;
   migration 0027's trigger and idempotence; the in-memory store being per Runner (declared).
6. **NFR-47:** does anything of a snapshot leave Docker, reach a network, or run unhardened?
7. **NFR-49:** with no snapshot, is anything different from before -- spec records, hashes, rows,
   labels, waits?
8. **Example 40 and the tests:** can any check pass when its subject did not happen? Break the code
   at runtime and watch each test fail.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M45,** and the README's rows, M46's included.
- **Owner's choices:** the decisions above.
- **FakeProvider has no sparse files or permissions;** those are checked on Docker only.
- **Examples and tests read private state:** `runner._sessions_for`, `_workspace_store`,
  `_workspaces`, the provider's `_spaces` and `_snapshots`.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. List every finding, and mark
  which you would block on.
- **Save every probe script** under a folder of your own in the system temp directory, and say where.
- **Clean up** every run, row, container, volume, network, folder, process and lock you create, and say
  what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M46-snapshots --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M46-snapshots --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- a snapshot taken while a command runs
- a second snapshot taken at once
- a write not waiting for the copy
- a write never counted
- no bound on a snapshot
- a snapshot never recorded
- a failed taking keeps its copy
- the copy's hold never let go
- a workspace of another tenancy snapshotted
- a server's workspace snapshotted
- another tenancy's snapshot found
- no check before a restore
- a restore never measured
- a restore at its bound refused
- a restore past its bound kept
- aclose removing a kept workspace's snapshot
- aclose keeping every snapshot
- a release never recorded
- no snapshot reaped
- a live owner's snapshot reaped
- the record omits the snapshot
- an empty snapshot name allowed
- the fake restores nothing
- the fake's snapshot shares the tree
- the fake starts from a snapshot it does not hold
- the copy unhardened
- the copy's source writable
- the copy's container kept
- docker restores nothing
- docker starts from a snapshot it does not hold
- docker restores a snapshot released while it is copied
- docker leaves the empty volume a mount made
- docker's snapshot measured with its baseline
- docker's snapshot root not prepared
- F1: the manager trusts the provider's filter
- F1: docker's snapshot filter trims
- F1: docker's workspace filter trims
- F2: a server's spec may name a snapshot
- F2: the manager opens a server from a snapshot
- F3: a late record never followed
- F3: aclose never waits for the follow-up
- F6: a stuck copy left untracked
- F4: no count before the copy
- F4: past the count read as a failure
- example 40: the control's read crashes
- example 40: the control unread
- example 40: the restore unread
- example 40: another tenancy unchecked
- example 40: a snapshot never taken passes
