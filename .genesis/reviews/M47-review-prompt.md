You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M47, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M47 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M47 is **Phase 5 increment 2's exit signal**: example 41 runs one orchestrated plan whose children,
at once, try every way out of an egress allow list, use a hostile sandboxed MCP server, snapshot and
restore a workspace, and burn CPU into a compute ceiling -- each contained, recorded and cleaned up,
checked against a control, on both stores, ten times on Docker. Its gate repairs also touch
`DockerProvider._contained`.

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table (P5-D24)

| Decision | P5-D16 to P5-D25 |
|---|---|
| P5-D24 Exit signal | Hostile code with one host allowed tries every other way out; a hostile MCP server in the sandbox tries the host, the network and its neighbours and is killed with its `Runner`; a snapshot of a workspace holding links is restored; a plan is stopped by its compute budget. Each is contained, recorded and cleaned up, on both stores, ten times, under DECISION-a00773b0 and DECISION-a7d6a1f5 |

### "#### M47: the exit signal"

- FR-171: The exit signal (P5-D24) is a test and an example. `scripts/41_increment2_contention.py` runs an orchestrated plan whose children:
  - allowed one host, try an IP literal, a name resolving privately, a rebinding name, an unlisted host, another site behind the allowed one's address by SNI, plain HTTP, DNS and a neighbour's network;
  - use a sandboxed MCP server that tries the host's database port, the network and a neighbouring workspace, and is killed with its `Runner`;
  - snapshot a workspace holding links and a file past a later spec's bound, and restore it;
  - burn CPU until the plan's compute ceiling stops it.
  - **Checked against the store, the provider and the commands' and servers' own output, on both stores,** each check naming its subject with a witness independent of the property (KNOWLEDGE-1afcf6b2): no connection but to the allowed pair succeeded, each refusal recorded with its reason; the server reached nothing and nothing of it remained; the snapshot restored only in its tenancy and within bounds; the plan stopped within one command's cost of its ceiling, every command charged at least its reading.
  - **Repetitions:** ten times on `DockerProvider` with the children's start order shuffled and random delays, as a live-marked test before review; the gates run the same plan on `FakeProvider`. A check whose subject never happened fails in every mode (DECISION-a7d6a1f5).

### Non-functional

- NFR-46: Increment 2 adds no runtime dependency (NFR-40): the proxy program ships as SDK source run in the workspace's image, and every Docker operation stays the `docker` CLI.
- NFR-47: Nothing leaves a workspace except a tool result, an exported artifact, a snapshot inside Docker, or a connection the proxy allowed to a pair its spec names (amends NFR-43). No connection reaches a non-global address, another workspace or the host, from a command or a sandboxed server.
- NFR-48: No command is charged less than it could have used: its `cpu_seconds` is its own reading, or else its wall time times its `cpus` limit, and nothing a command prints changes it.
- NFR-49: Runs whose workspaces have no `egress`, no snapshot and no sandboxed server send the same requests and record the same events and rows as before M43, apart from the new columns, recorded empty, and `cpu_seconds`; their `agent_spec_hash` is unchanged.

### Acceptance

- AC-105: FR-171, the exit signal: example 41 holds every check ten times on `DockerProvider`, shuffled with random delays, and the gates hold its plan on `FakeProvider`; each check fails without its subject -- a test removes, in turn, the internal network, the proxy's address check, its SNI check, the server's container removal, the snapshot's tenancy check and the compute charge, and each removal fails the check that names it.

## The records

The owner settled M47 in these decisions, all in `.genesis/project.json`. Read them: a choice they
record is not a defect, but code that differs from them is.
- **DECISION-33856cec** (pre-flight): the live model writes each probe verbatim, and one that declines
  fails its check; the snapshot is taken by the operator while the child's next command waits for
  approval; the hostile server is a standard-library stdio fixture carried in example 41; AC-105's
  removals split by provider (network, address check, SNI check, server removal on Docker; the
  snapshot's tenancy check and the compute charge on both).
- **DECISION-18bd3f65** (pre-flight): the rebinding name is rbndr.us's `7f000001.01010101.rbndr.us`;
  its witness is a control seeing both answers; offline, FakeProvider rotates them.
- **DECISION-dda032f4:** gates run with `--timeout 7200000`.
- **DECISION-58c667e0** (the pre-review's findings and the live runs' shapes, below).
- **DECISION-429f053c** (two repairs after the first gates, below).
- **DECISION-fc8cb843:** the ten Docker runs per store are ten tests of one run each, under
  `faulthandler_timeout` (300 s), as M44 did (DECISION-f7114b8b).
- Standing: DECISION-a00773b0 (checks hold when a model quotes earlier results), DECISION-a7d6a1f5 (no
  vacuous live check), KNOWLEDGE-1afcf6b2 (each check names its subject with an independent witness),
  DECISION-94bfee7c (no check keys on an error's text).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `a7e46a2` (M46). M47 is uncommitted. It adds no migration and no runtime
dependency, and leaves SPEC.md unchanged.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** DATABASE_URL and the model gateway's settings from .env. **Never print
  credentials or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`. That
  does not stop a sleep the owner starts (lid, button): before calling any delay real, compare wall
  time with monotonic time, or read the Kernel-Power events (506/507).
- **Docker Desktop is running.** The tests use
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df` (python:3.12-alpine),
  on the host. **Never pull, build, prune, stop or restart anything.** Touch only what carries your own
  `agentsdk.tenant` prefix. Report how many remain at the end (it must be 0). In Git Bash use
  `MSYS_NO_PATHCONV=1` for a path meant for inside a container.
- **The network:** example 41 needs example.com and rbndr.us reachable, as example 38 does.
- **Line endings vary by file** (some LF, most CRLF). A mutant whose edit silently never applies looks
  exactly like a surviving one: assert the file changed before running its test, and restore it byte
  for byte, verified by SHA-256.

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. Three python processes (PIDs 27472, 6348, 17824) are the owner's Jupyter kernel: leave them.
  Run with `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M47-increment2-contention --timeout 7200000` in the background, kept awake:
  unit (`tests/test_increment2_contention.py`, about 1 minute), regression (the full suite, about 35
  minutes), and `docker` (every Phase 5 Docker-marked test, about 80 minutes).
- Example 41 by hand: `offline(store, provider, tenant=..., quiet=True, seed=...)` returns
  `(checks, run_ids)`; on Docker a run takes about 85 s. Live: `python scripts/41_increment2_contention.py`
  (gateway model, about 2.5 minutes); it prints the run ids it made.
- M47's own Docker tests: `pytest tests/test_increment2_contention.py -m docker -p no:cacheprovider`,
  about 38 minutes; one store's ten runs: add `-k "holds_every_check and memory"`.

**Cleanup:** the fixture in `tests/test_increment2_contention.py` shows the order: `workspace_snapshots`,
`workspace_connections`, `workspace_commands`, `workspaces`, `mcp_tool_acceptances`,
`mcp_tool_versions`, then `interruption_decisions`, `interruptions`, `standing_decisions`, `artifacts`
by `source_run`, `plan_node_states`, `plan_versions`, `run_events`, `messages`, `execution_manifests`,
then the runs, children first. Use tenant ids starting `SYN-m47r1`, and remove only rows your probes
recorded. A live run uses tenant `example-tenant` and a fresh project `contention2-*`: remove a live
run's rows by the run ids it printed and that project only.

## Task under review

### What M47 builds

- **`scripts/41_increment2_contention.py`.** One plan, children shuffled with random delays:
  - `egress`: allowed `example.com:443`, `localtest.me:443` and the rebinding name; a probe tries the
    allowed pair, an IP literal, an unlisted host, a privately resolving name, another site by SNI,
    plain HTTP, a second request on one tunnel, then the rebinding name twelve times 1.1 s apart, and
    a direct connection, the host and DNS from inside;
  - `server`: a hostile stdio MCP server (`SERVER`, carried as text) in a sandboxed workspace, whose
    tools try the network, the host's database port, a neighbour and DNS, each against a control run
    from a neighbour's proxy; killed with the Runner;
  - `snapshot`: builds a tree with a link and a 3 MiB file, prints a nonce; the operator snapshots it
    while the next command waits for approval; that command rewrites a file and adds another; restores
    are tried from another tenancy, under a smaller bound, and within bounds;
  - `burn`: burns CPU until the plan's compute ceiling stops it.
  - 18 checks on Docker, 14 offline (the network checks are Docker-only). Each names its subject; a
    check whose subject never happened fails.
- **`agentsdk/workspace/__init__.py`:** FakeProvider's resolver accepts a callable, so a name can
  rotate its answers offline.
- **`agentsdk/workspace/docker.py`** (DECISION-429f053c): `_contained` kills a `docker start -a`
  client still running after its first bound, then reads its pipes to their end.
- **README.md:** the examples table gains rows 40 (missing since M46) and 41, and a "Declared (M47)"
  row on the rebinding service.

### The pre-review's findings, answered (DECISION-58c667e0)

A fresh Sonnet subagent probed M47 at runtime. Each finding was reproduced by the author before it
was repaired, and each repair has a mutant in the appendix that a test kills.

| Finding | Repair |
|---|---|
| **F1 (blocking):** the snapshot child's second command changed nothing, so a snapshot aliasing the live tree passed every check. | The second command rewrites `a.txt`, adds `later.txt` and reads `a.txt` back; the restore must read the earlier state, and the check's subject is that command's own output, started after the snapshot was recorded. |
| F2: `aclose()` ran inline, so an exception or a cancel left workspaces. | `offline()` and `live()` close the Runner and remove the neighbour in a `finally`. |
| F3: server calls whose id began `q` were set aside as quotes. | Nothing is set aside by id. |
| F4 (note): server checks read the fixture's own result text; the restore check reads `list_workspace`'s format. | Neither is an SDK error message: DECISION-94bfee7c not engaged. |

The live runs found three shapes of a real model, handled in the example: both snapshot commands
asked in one response (the operator refuses a second command asked before the first ran; the child
asks again alone; the second needs the first's nonce); the egress probe run more than once (each run
judged on its own rows); and the egress child out of output at the default limit (live roles get 8000
tokens; the controls now always run).

### After the first gates (DECISION-429f053c)

1. Live round 4: 9 of 10 runs held all 18 checks; run 10 failed only REBOUND's witness -- the control's
   12 lookups over 13 s saw only `1.1.1.1` while the proxy was handed both answers. The control now
   looks up to 60 times, stopping at two answers.
2. The first Docker gate passed with two unclosed-transport warnings in `test_compute.py`'s
   provider-kill cancel case (absent from the M44 to M46 gates), reproduced in 4 of 6 runs. Two commands
   per run waited out both 60 s bounds in `_contained` before the client was killed (the case removes
   the command removal that ends a timed-out command). After the repair: 0 of 6, and the case fell from
   310-378 s to 124 s. **The warning's exact mechanism was not isolated:** a probe outside pytest saw no
   unclosed transport with or without the repair. Treat that as open to your probing.
3. The second Docker gate passed but its stderr held the M33 stall dump: the ten-run test ran about
   15 minutes per store, past `faulthandler_timeout`. It is now twenty tests of one run each
   (DECISION-fc8cb843); the gates below ran after that change.

### How the author checked it

- **Tests:** `tests/test_increment2_contention.py`, 56 tests: 28 offline (both stores, FakeProvider) and 28 Docker-marked (twenty single runs, ten per store; the six AC-105 removals; the egress child doing nothing; the scripted run); plus one Docker test in `tests/test_compute.py` for the `_contained` repair. Written before the code and run against it first; later tests were added
  for each repair.
- **Mutation:** 25 mutants, one exact edit each, every file restored byte for byte by
  SHA-256 (appendix).
- **AC-105's removals:** each of the six fails the check that names it, for its own reason (the test
  asserts the reason, not only the failure).
- **Live:** ten runs on PostgreSQL and Docker with the gateway model, all 18 checks each, on the final
  example (round 5). They ran before the `_contained` repair, which changes only a client outliving
  its bound; the Docker gate ran after it. Their rows were removed by run id and project.
- **Older suites:** every test file touching the changed modules, 831 passed before the gates.
- **Gates** on source hash `ef3328fe1dc4d8ecbebf80bdaf340ba046cad549bda943c4125f515c95567bb7`: unit 28 passed, 28 deselected in 48.28s; regression 3294 passed, 168 deselected in 2250.11s (0:37:30); docker 168 passed, 732 deselected in 4428.11s (1:13:48), each exit 0 with empty stderr and no warnings, kept awake.

### Edits to approved tests

- `tests/test_distribution.py`: `EXPECTED_EXAMPLES` gains `41_increment2_contention.py` (FR-171).
- `tests/test_compute.py`: one test added (`test_on_docker_a_client_outliving_its_bound_is_ended_then`)
  and `import time`; nothing removed or changed.

Check both by content (`git diff HEAD -- tests/test_distribution.py tests/test_compute.py`).

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **Vacuity.** Can any of the 18 checks pass when its subject never happened, or for a reason other
   than the property it names? Break the SDK at runtime and watch each check fail.
2. **Egress.** Can any way out succeed unrecorded, or be recorded under the wrong reason? Can the
   rebinding check pass if the proxy resolved again before connecting, or if the name never rebound for
   the proxy itself?
3. **The server.** Does it reach anything -- the network, the host's port, a neighbour, DNS? Is each
   control real? Does anything of it survive `aclose()`?
4. **The snapshot.** Is it always taken between the child's commands, whatever the order or batching?
   Can the restore check pass when the restored workspace is not the snapshot's copy, or another
   tenancy's restore succeed?
5. **Compute.** Can the stop check pass when the ceiling did not stop the burner? Is any command
   charged below its reading?
6. **AC-105.** Does each of the six removals fail the check that names it, for that reason?
7. **Quoting and live (DECISION-a00773b0, DECISION-a7d6a1f5).** Do the checks hold when a child quotes
   earlier results, and in live mode with a model that declines?
8. **`_contained`.** Is the repair sound: can killing a client after its first bound lose output a
   finished command printed, misreport `killed` or an exit code, or change a run with no expired bound
   (NFR-49)? Can you find what left the transports unclosed?
9. **Cleanup.** After any run -- passing, failing, raising, cancelled -- does anything remain?

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M46,** and the README's rows, M47's included (example 41 depends on
  rbndr.us and example.com).
- **Owner's choices:** the decisions above.
- **FakeProvider has no real network:** the Docker-only checks (direct connection, neighbour,
  gateway, the server's containment) do not run offline.
- **Examples and tests read private state:** the provider's `_spaces`, `_resolve` and `_snapshots`,
  `egress.SOURCE`, the Runner's sessions.

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
  control approve . M47-increment2-contention --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M47-increment2-contention --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- the fake never asks a callable
- the fake's site lookup reads a callable
- the rebinding witness unread
- the proxy need not be handed both answers
- an allowed attempt need reach nothing
- the refusals unread
- another tenancy unchecked
- the charge unread
- the stop unread
- the direct connection unread
- the server's remains unread
- the allowed check without its subject
- the snapshot taken before the build
- no snapshot taken
- any program is the probe
- the children never quote
- a batched second command approved
- only the first run of the probe judged
- the controls only after the egress probe
- F1: the restore's property unread
- F1: the change after the snapshot unwitnessed
- F2: no aclose when the example fails
- F3: calls set aside by their id
- live children at the default output limit
- the client outliving its bound not killed (docker.py, DECISION-429f053c): killed by test_compute.py's test_on_docker_a_client_outliving_its_bound_is_ended_then, 11.0 s against 1 + 2 x 5 s; run on its own, docker.py restored byte for byte
