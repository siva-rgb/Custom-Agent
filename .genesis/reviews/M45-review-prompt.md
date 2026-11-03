You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M45, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M45 was implemented by Opus 5.5 and its pre-review run by an Opus 5.5 subagent. Bring no
memory of an earlier review into this one: if your session holds any, say so and ask for a fresh one.

M45 is **stdio MCP servers in the sandbox**: an MCP server the SDK launches over stdio can run over a
workspace of its own -- on Docker a hardened container, on FakeProvider a local process -- given its
files by the application, recorded, and removed with its Runner.

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved** (DECISION-97b301ea). Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table (P5-D21, P5-D23)

| Decision | P5-D16 to P5-D25, 2026-10-30 |
|---|---|
| P5-D21 Sandboxed MCP | `McpServer(..., workspace=WorkspaceSpec(...))`, configured in code (P4-D25): the server's command runs in a hardened container of its own, over a workspace of its own, with files the application gives in code imported first through P5-D8's checked transfer. It lives with its `Runner`, owned, recorded and reaped like a workspace, reaches out only through its spec's `egress`, and its results stay untrusted MCP content |
| P5-D23 Records | One migration per milestone (DECISION-91ec4213): `0023` (M43) `workspace_commands.cpu_usec`; `0024` (M44) `workspace_connections` (each egress connection, allowed or refused, with its reason), and `0025` (M44) its one `unrecorded` row per workspace, a second because `0024` was already applied when its review found the index missing (DECISION-7a3a2ae2); `0026` (M45) `workspaces` scope `server`, its `run_id` NULL for that scope only (DECISION-2a12196e), and `workspace_connections.run_id` NULL for a server workspace's connections only (DECISION-61aed587); `0027` (M46) `workspace_snapshots`. A sandboxed server's workspace is a `workspaces` row of scope `server`. The manifest records each workspace's `egress` and each sandboxed server's spec |

### "#### M45: stdio MCP servers in the sandbox"

- FR-164: `McpServer` gains `workspace: WorkspaceSpec | None` (P5-D21) and `files: Mapping[str, bytes]`, for stdio servers only. With a workspace, the server's `command` and `args` run in a hardened container of its own over a workspace of its own, created when its `Runner` connects to it, with `files` imported first through the checked transfer. The manifest records the spec; a server without one runs as before (FR-123). With a workspace, the server's own `env` or `cwd` raises `ValueError` naming `WorkspaceSpec.env`: its working directory is its workspace's root, its environment its spec's `env` alone (DECISION-b478705b).
- FR-165: Its lifetime. The container is attached (`docker run -i`), labelled as a workspace is with scope `server`, and recorded in `workspaces`. It is removed when its `Runner` closes the server, at `aclose()`, and when reaped after a dead `Runner`; a server that ignores its stdin's end is removed by the container's removal. A server's crash is FR-125's, unchanged. Its workspace counts against `max_concurrent_workspaces`. **As decided in the pre-flight (DECISION-b478705b):** a server's connection is shared per alias, tenant and project (P4-D28) and may open with no run, so its workspace belongs to its `Runner`'s tenancy and is recorded scope `server` with no run (migration `0026`, DECISION-2a12196e); a server's `WorkspaceSpec` with `keep=True` or scope `plan` raises `ValueError`; a crash's one restart starts a new container over the same workspace, its files kept, and a server dead with its restart spent has its workspace removed and its slot given back at once, recorded `aclose` (DECISION-5470ab57); a server waits for a workspace slot within its `connect_timeout`, past which its connection fails as any does. On `FakeProvider` a sandboxed server is a local process whose working directory is a temporary folder holding its files, with the same records, lifetime, slot and reaping.
- FR-166: Its trust. Results stay MCP content (FR-125's labels); a server reaches out only through its spec's `egress`; its environment is its spec's `env` alone; its files are those the application gave and what it writes itself. Its connections through its egress are `workspace_connections` rows with no run and no event, as its workspace has no run (FR-162's rules otherwise, its unrecorded row included; DECISION-61aed587).
- FR-167: Example 39 (`scripts/39_sandboxed_mcp.py`): the M33 fixture server, run sandboxed, serves a plan; it cannot reach the network, the host or a neighbour, and nothing of it remains after `aclose()`. Its server is a legacy-era stdio server of its own, standard library only, given through `files` and run by the image's `python3`, as example 27 carries its own, so the example runs outside the repository (AC-17); the M33 fixture itself is run sandboxed by M45's gate (DECISION-92ae6ae5). Its checks of the network, the host and a neighbour run on Docker only, each against a control (DECISION-b478705b).

### Non-functional

- NFR-46: Increment 2 adds no runtime dependency (NFR-40): the proxy program ships as SDK source run in the workspace's image, and every Docker operation stays the `docker` CLI.
- NFR-47: Nothing leaves a workspace except a tool result, an exported artifact, a snapshot inside Docker, or a connection the proxy allowed to a pair its spec names (amends NFR-43). No connection reaches a non-global address, another workspace or the host, from a command or a sandboxed server.
- NFR-49: Runs whose workspaces have no `egress`, no snapshot and no sandboxed server send the same requests and record the same events and rows as before M43, apart from the new columns, recorded empty, and `cpu_seconds`; their `agent_spec_hash` is unchanged.

### Acceptance

- AC-103: FR-164 to FR-167: a sandboxed server answers a plan's calls; it reaches no network, no host port and no neighbour; a server that ignores its stdin's end is gone after `aclose()`, and a dead `Runner`'s after reaping.

## The records

The owner settled M45 in these decisions, all in `.genesis/project.json`. Read them: a choice they
record is not a defect, but code that differs from them is.
- **DECISION-b478705b**: the pre-flight -- the legacy-era M33 fixture given through `files`; a crash's
  restart over the same workspace; a local process on FakeProvider; the server's own `env` and `cwd`
  refused with a workspace; a slot within `connect_timeout`.
- **DECISION-2a12196e** corrects it: the `workspaces` table allowed only scope `run`/`plan`, and a
  connection opens with no run, so **migration 0026** adds scope `server` with `run_id` NULL for it
  alone (P5-D23 amended: M46's snapshots move to 0027); a server spec with `keep` or scope `plan` is
  refused.
- **DECISION-61aed587**: a server's egress connections are rows with no run and no event.
- **DECISION-92ae6ae5**: example 39 carries its own small legacy-era server (AC-17 runs examples
  outside the repository, where tests/fixtures is absent); the M33 fixture itself is sandboxed by the
  gate tests.
- **DECISION-5470ab57**: the adversarial pre-review's seven findings and their repairs (below), and a
  dead server releasing its workspace at once.
- Standing: DECISION-a00773b0, DECISION-a7d6a1f5, KNOWLEDGE-1afcf6b2 (no vacuous check),
  DECISION-94bfee7c (no check keys on an error's text).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `62067ab` (M44). M45 is uncommitted, with SPEC.md's M45 amendments, README.md's
row for example 39, and migration `agentsdk/migrations/0026_server_workspaces.sql`, **already applied
to the dev database**.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is running, Engine 29.6.2.** The tests use
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df`, on the host.
  **Never pull, build, prune, stop or restart anything.** Touch only what is labelled with your own
  `agentsdk.tenant` prefix. Report how many remain at the end (it must be 0). In Git Bash use
  `MSYS_NO_PATHCONV=1` for a path meant for inside a container.
- **Line endings vary by file** (some LF, most CRLF). A mutant whose edit silently never applies looks
  exactly like a surviving one: assert the file changed before running its test, and restore it byte
  for byte, verified by SHA-256.

**Running tests**
- **One test session at a time.** Check that no other python or node process is running first. Run
  with `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M45-sandboxed-mcp --timeout 3600000` in the background, kept awake: unit
  (`tests/test_sandboxed_mcp.py`), regression (the full suite, about 32 minutes), and `docker` (every
  Phase 5 Docker-marked test, about 45 minutes).
- M45's own Docker tests: `pytest tests/test_sandboxed_mcp.py -m docker -p no:cacheprovider`, about
  2 minutes.

**Cleanup order:** `workspace_connections`, `workspace_commands`, `workspaces`, then
`interruption_decisions`, `interruptions`, `standing_decisions`, `artifacts` by `source_run`,
`plan_node_states`, `plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs,
children first. Use tenant ids starting `SYN-m45r1`, and remove only rows your probes recorded. A
FakeProvider server leaves a folder `agentsdk-server-*` in the temp directory until its workspace is
removed: remove any your probes leave.

## Task under review

### What M45 builds

- **`StdioServer(..., workspace=WorkspaceSpec(...), files={path: bytes})`** (`agentsdk/mcp`): checked
  by `agentsdk.workspace.server_files` -- its own `env`/`cwd`, `keep`, scope `plan`, an unclean path,
  a path that is also another's folder or differs from another only in case, and non-bytes are
  refused; `record()` adds the spec and the file names. A Runner with a sandboxed server and no
  `workspace_provider` raises.
- **The connection** (`_Connection._launch`): at a sandboxed server's first start, its workspace is
  opened by the Runner's `WorkspaceManager` on the MCP engine's own loop, then its provider gives a
  `ServerLaunch` the MCP library starts over stdio. A restart reuses the workspace. Its close -- or
  its death with its restart spent -- removes the workspace (`_end_workspace`).
- **`WorkspaceManager.open_server` / `server_launch` / `close_server`**: a slot within
  `connect_timeout`; create and import, `SETUP_SECONDS` each; the record scope `server`, no run,
  bounded; egress booked as rows with no run and no event; kept out of the per-run `spaces`, which the
  Runner's loop ends -- `aclose()` sweeps what is left by the Runner's label.
- **`FakeProvider.server_launch`**: a folder made from the workspace's files at the first start, kept
  across a restart, removed with the workspace; its spec's env alone.
- **`DockerProvider.server_launch`**: `docker run -i` (`server_args`), hardened as a command, its
  entrypoint cleared, labelled `agentsdk.role=server`; a previous server container removed first.
- **Bounds**: `_ensure_bound` and the engine close bound include a sandboxed start and close
  (`SERVER_START_SECONDS`, `SERVER_CLOSE_SECONDS`).
- **Migration 0026**, the PostgreSQL readers' NULL run, **example 39**, and the gate
  `tests/test_sandboxed_mcp.py`.

### The pre-review's findings, answered (DECISION-5470ab57)

| Finding | Repair |
|---|---|
| **F1 (blocking):** `server_args` alone of every container launch kept the image's entrypoint, so an image with one ran it before the server, on the protocol's stdout. | `--entrypoint=` before the image; the test now requires it. |
| **F2 (blocking):** `_ensure_bound` and the engine close bound left out every wait a sandboxed start and close add; a start inside its own bounds was cut as `McpLoopUnanswered` and restarted with no back-off. | Both bounds include them; a test ties the constants to the timeouts they are made of. |
| **F3:** no test of the slot after a close, of a server's egress, of the connections trigger, or of a closed manager. | Each tested; the egress test runs on Docker on both stores. |
| **F4:** example 39's cleanup check passed with the server's folder still on disk. | It requires the folder gone; a test leaves it and sees the check fail. |
| **F5:** a file and a folder of one name, or a case-only duplicate, passed construction and failed at every connect. | Refused at construction. |
| **F6:** a workspace whose record write timed out but landed stayed unremoved. | Its removal is recorded after the removal. |
| **F7 (owner, DECISION-5470ab57):** a dead server held its workspace and slot until `aclose()`. | Released at once, recorded `aclose`. |

### How the author checked it

- **Tests:** `tests/test_sandboxed_mcp.py` has 46 tests without Docker and 11
  with it, all passing. The first 27 were written before the code and failed against it for the
  reason each names. Each guard added after them -- the pre-review's repairs included -- has a mutant
  in the appendix that a test kills; a few later tests (the example's listing, its program run) have
  none.
- **Mutation:** 45 mutants, one exact edit each, all killed by the tests they name. Five
  first survived and each gained a test: a connection's close removing its workspace (the Runner's
  sweep had hidden it); env values on the docker line (the test checked only the bare value); the old
  server container kept on a restart; a closed manager waiting for a slot; and AC-103's reaping.
- **Gates** on source hash `aaf18244`: unit 46 passed, regression 3,189 passed (33:09), docker 126 passed (46:43), all exit 0 with no warnings, kept awake.

### Edits to approved tests

- `tests/test_mcp_client.py` (M33's invariant tests, `test_every_wait_in_the_mcp_module_names_its_bound`
  and `test_every_loop_in_the_mcp_module_names_what_it_iterates`): entries added for M45's new waits,
  calls and loops in `agentsdk/mcp`, each naming its bound -- `_launch`'s two provider calls, `_open`'s
  `_launch()`, `_end_workspace` and its two callers, `server_files`, the spec's `record`, and the
  launch's args and env. No entry was removed or loosened.
- `tests/test_distribution.py`: `EXPECTED_EXAMPLES` gains `39_sandboxed_mcp.py` (FR-167).
- `tests/test_egress.py` (M44): `test_r5_n6_migration_0025_adds_the_one_unrecorded_row_index`
  asserted 0025 was the latest migration; M45's 0026 failed the regression gate. It now asserts 0025
  is present, as M45's own test does for 0026 (nothing about 0025's content is asserted less).
- `tests/test_compute.py` (M43/M44): `test_on_docker_the_cancel_check_without_a_layer_and_its_cover_fails`
  gains `filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")` for that test alone, the
  owner's choice: it takes away every layer that ends a cancelled command, so its docker client is
  left unended by design, and its pipe transport warned when collected after its loop had gone -- in
  M44's gate under `[provider kill]` and in M45's first under `[command removal]`. Its assertion is
  unchanged. Judge whether anything else could hide behind it.

## Attack these first

1. **Lifetime and leaks.** Can a server's workspace (folder, container, volume, network, proxy) or its
   slot outlive its Runner's `aclose()`, a failed or cancelled start, a crash restart, a retry after
   back-off, its death, or a Runner collected without `aclose()`? Can one connection hold two?
2. **Two loops.** The server's workspace lives on the MCP engine's loop; `WorkspaceManager.aclose`,
   `reap` and `finish` run on the Runner's. Can anything be awaited or resolved across them -- the
   slot, the egress recorder, `aclose()`'s sweep meeting a close still under way?
3. **Records.** Scope `server`, no run, removed `aclose` or `reaped`: on both stores, does every path
   leave a row written once and removed once? Is migration 0026 right and idempotent, and its trigger?
   Does anything assume a workspace or connection has a run?
4. **Trust and isolation (FR-166):** only the spec's env; files confined to the workspace; the
   container hardened as a command's; egress only through the spec's pairs, with a token naming no
   command; results still untrusted MCP content.
5. **The bounds:** can a sandboxed start or close still exceed what its caller waits for?
6. **Example 39 and the tests:** can any check pass when its subject did not happen? Does each test
   reach the state it names -- break the code at runtime and watch it fail.
7. **NFR-49:** with no sandboxed server, is anything different from before?

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M44,** and the README's rows.
- **Owner's choices:** the decisions above.
- **FakeProvider gives a sandboxed server no isolation:** it is a local process; containment is
  checked on Docker only (DECISION-b478705b).
- **A store that never returns** can hold a run's end, as declared for M44 (DECISION-5445c8d0).
- **Examples and tests read private state:** `runner._sessions_for`, `_workspace_store`,
  `_workspaces`, `_mcp`, the provider's `_spaces`.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. List every finding, and mark
  which you would block on.
- **Clean up** every run, row, container, volume, network, folder, process and lock you create, and say
  what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M45-sandboxed-mcp --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M45-sandboxed-mcp --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- config: own env or cwd allowed
- config: kept or plan allowed
- config: files without a workspace
- config: unclean parts allowed
- config: a backslash allowed
- config: a drive allowed
- config: text allowed for bytes
- config: the record names no workspace
- config: a Runner with no provider
- mcp: the launch ignored
- mcp: no workspace removal at close
- mcp: a new workspace each start
- manager: no slot taken
- manager: recorded scope run
- manager: recorded under a run
- manager: files never imported
- manager: a failed creation left in place
- manager: a failed creation keeps its slot
- manager: removed as reaped
- fake: its folder kept after removal
- fake: not its spec's env
- fake: a new folder each start
- docker: the server unhardened
- docker: the server removed as it exits
- docker: env values on the line
- docker: the old server container kept
- postgres: no run read back as text
- example 39: no control
- example 39: a refused connection counted as failed
- example 39: the echo unread
- example 39: the network control unread
- example 39: the host control unread
- example 39: the neighbour control unread
- F1: the image's entrypoint kept
- F2: no sandbox term in the start bound
- F2: no sandbox term in the close bound
- F3: the slot never marked held
- F3: a closed manager makes one
- F3: a server's egress never recorded
- F4: the folder check unread
- F5: a path also a folder
- F5: case-only duplicates
- F6: a late record never removed
- F7: a dead server keeps its workspace
- AC-103: a server workspace with no owner label
