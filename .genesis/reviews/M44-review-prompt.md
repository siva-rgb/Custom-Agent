You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is round 6. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M44 was implemented by Opus 5.5 and its pre-review run by an Opus 5.5 subagent.
Round 1 was reviewed by Sonnet 5.5, which rejected it (DECISION-a7a1b37b); its code has since
changed throughout. Round 2 was reviewed by Fable 5.1, which approved it with four low findings; the
orchestrating session added a fifth (DECISION-e510a5e3). Round 3 was reviewed by Opus 5, which
approved it with two findings. Round 4 was an advisory pass by Haiku 5.5, which found one more,
reproduced by the orchestrating session; all repaired (DECISION-f7114b8b). Round 5 was reviewed by
Sonnet 5.5, which rejected it with two blocking findings and eight others, all verified first-hand
by the author and all repaired in M44 (DECISION-0d5473ef, corrected where its mechanism could not be
built by DECISION-5c68ddfb and DECISION-7a3a2ae2). Bring no memory of an earlier review into this
one: if your session holds any, say so and ask for a fresh one.

**Round 6's focus is round 5's repairs** (attack item 0), and whether they broke anything that held
before. **Round 5's repair of round 3's cancelled end was itself defective, and its test never
reached the state it named** (B1 below): so for every round 5 repair, check that its test reaches
the state it names -- break the repair at runtime and watch the test fail -- not only that it
passes. Items 1 to 7 stand as before, for anything the repairs touched.

M44 is **egress by allow list**: a workspace reaches only the `host:port` pairs its spec names,
through a network and a proxy of its own, every connection recorded. It also closes the five items
M43's review carried (DECISION-91e5d73a). **The owner's standing rule since M44: every issue is fixed
in the milestone that found it; nothing is carried to the next** -- so report everything you find,
and say which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved** (DECISION-97b301ea). Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table (P5-D19, P5-D20, P5-D23)

| Decision | P5-D16 to P5-D25, 2026-10-30 |
|---|---|
| P5-D19 Egress | Off by default: `--network none` is unchanged. `WorkspaceSpec.egress` names `host:port` pairs, exact host names only, no IP literal, no wildcard. A workspace that has any gets its own `--internal` network, and a proxy container on it and the default bridge, hardened as a command is, in the workspace's own image, which must provide `python3` |
| P5-D20 The proxy | HTTPS `CONNECT` only, to a listed pair; it resolves the name, refuses the connection if any address is not global, connects to an address it checked, and requires the TLS ClientHello's SNI to equal the host. Commands get `HTTPS_PROXY`; nothing else leaves. Declared: only proxy-aware clients reach out, and no name resolves inside |
| P5-D23 Records | One migration per milestone (DECISION-91ec4213): `0023` (M43) `workspace_commands.cpu_usec`; `0024` (M44) `workspace_connections` (each egress connection, allowed or refused, with its reason), and `0025` (M44) its one `unrecorded` row per workspace, a second because `0024` was already applied when its review found the index missing (DECISION-7a3a2ae2); `0026` (M46) `workspace_snapshots`. A sandboxed server's workspace is a `workspaces` row of scope `server`. The manifest records each workspace's `egress` and each sandboxed server's spec |

### "#### M44: egress by allow list"

- FR-159: `WorkspaceSpec.egress` (P5-D19) is a tuple of `"host:port"`, each host an exact DNS name, lower-case, without a trailing dot; an IP literal in any form an address parser reads (hex, octal and short forms included), a wildcard, a port outside 1 to 65535 or written with a leading zero, or more than 32 entries raises `ValueError` (DECISION-26009e04). It is part of the spec's record and the manifest, and part of the key that shares a plan workspace (FR-152).
- FR-160: The network and the proxy.
  - **Created with the workspace:** an `--internal` network whose gateway is isolated (`com.docker.network.bridge.gateway_mode_ipv4=isolated`, so no command reaches the host through it; a daemon older than Docker Engine 28 fails the workspace `WorkspaceUnavailable`, naming its version), and the proxy container, listening on its address on that network alone, both labelled as the workspace is, before its first command; each command container joins that network instead of `--network none`, and gets `HTTPS_PROXY` and `https_proxy` naming the proxy with a random token of its own as the password (`http://agentsdk:<token>@<proxy>:<port>`: a proxy-aware client sends it as `Proxy-Authorization`, which Python's urllib does only for a user and a password), which the proxy reports and the manager maps to the command (DECISION-88dea779). No other hardening changes.
  - **A command's token** names it only while it runs: retired when it ends (DECISION-26009e04).
  - **A proxy that dies** is recorded -- one `proxy_ended` row and event; a row the store refused is counted on the `unrecorded` row and its event still written (DECISION-0d5473ef) -- and its workspace refuses every later command `WorkspaceUnavailable`, naming it: egress never stops silently (DECISION-26009e04).
  - **Removed with it:** the proxy and the network go with the workspace, at its run's end, at `release_workspace`, at `aclose()` and when reaped; `list_owned` lists a workspace whose network or proxy remains. A run cancelled while its workspace is removed still has its plans' workspaces removed, and the cancel is raised once both are done; a failed run so cancelled still ends `failed`, its failure having come first (DECISION-0d5473ef).
  - **The proxy program** ships with the SDK as source and runs in the workspace's image under `python3`, 3.8 or later; an image without it fails the workspace `WorkspaceUnavailable` at creation. Its container is hardened as a command's is (P5-D6) with fixed limits of its own -- 64 MiB memory, 32 pids, 0.50 cpus -- and its CPU is not charged to any run (DECISION-88dea779).
- FR-161: The proxy's rules (P5-D20). It accepts only `CONNECT host:port` naming a listed pair. It resolves the host and refuses the connection if the answer is empty or any address is not global; it connects to one address it checked, never resolving again. It reads the ClientHello from the client and refuses the tunnel unless its SNI equals the host, and refuses a ClientHello naming more than one server or carrying a second `server_name` extension (DECISION-0d5473ef). It refuses anything else -- another method, a malformed request (a port written with a leading zero among them, DECISION-7a3a2ae2), a second request on one connection -- with `403`, and closes. It keeps no data. **Its bounds** (DECISION-88dea779): at most 32 tunnels open at once, past that refused `busy`; at most 64 connections waiting on their request head at once, each at most 10 s, past that refused `busy`; a tunnel closes after 60 s with no bytes either way; a connection its client resets is reported `client_reset`, and one the proxy itself fails on `proxy_error`; one still undecided when the proxy is stopped with its workspace is reported `stopped`; and at most 10,000 connections in its life -- fewer when its provider is given `max_connections`, as `FakeProvider` and `DockerProvider` both are (DECISION-f7114b8b) -- past that each refused at once, the count reported at the first refusal, then at most once a second, and when the proxy stops (DECISION-e510a5e3).
- FR-162: Every connection is recorded: a `workspace_connections` row (workspace, run, command where known, host, port, the address used, allowed or refused, the reason by name, the time, and for a tunnel the bytes each way, counted when it ends) and a `WorkspaceConnection` event under the run that made it, while that run is under way; once it has ended, as a kept workspace outlives it, the row alone, so nothing follows a run's terminal event (DECISION-a7a1b37b). Both are written by the manager from the proxy's own report on its attached output, never from a command's (migration `0024`). The command is known by its token; a connection with none, or an unknown one, is recorded with no command, under the run its workspace is recorded under: its run, or its plan's top-level run (DECISION-e510a5e3). Past the 10,000th, one `connection_limit` row, carrying the count of every connection refused at the limit, stands for them all (DECISION-88dea779); its one event marks the limit reached, at the first refusal, and carries no count (DECISION-a7a1b37b). Reports the manager could not record within its workspace's removal bound are counted in one `unrecorded` row, so each connection is recorded individually or counted there (DECISION-e510a5e3); so is a connection whose row the store refused, and one reported after its workspace's recorder ended. A workspace has at most one `unrecorded` row, its count updated as more is counted; past the bound a tunnel's end still writes its bytes and a proxy's death its `proxy_ended` row (DECISION-f7114b8b); the store holds the one row by a unique index (migration `0025`), its id fixed before its first write so an insert that committed and then raised is updated, never written twice; and `aclose()`, past its bound, tells a late count to stop after its current write and cancels it, so it ends unless inside a store write that never returns (DECISION-7a3a2ae2). An event already being written when its run ends lands before the terminal event, even if the run is cancelled again meanwhile (DECISION-0d5473ef); its write asks, on its worker thread just before its transaction, whether its run has ended, so past the removal bound one not yet begun is shut out, its row standing alone, and only one already inside its transaction is waited for (DECISION-5c68ddfb).
- FR-163: Example 38 (`scripts/38_egress.py`): a child allowed `example.com:443` fetches it through the proxy and fails every other way out, each refusal on its row; offline on `FakeProvider` (a fake command's `connect(host, port)` meets the same rules) and live on Docker. Its checks of a direct connection, the host, the network's gateway, a name lookup and a neighbour run on Docker only, where they can fail (DECISION-26009e04); each has a control: the neighbour's proxy, on the bridge as well, reaches the host's address and port and answers on its own, so neither failure inside is a dead address's; and the neighbour check counts only a timeout or an unreachable network as failed, never a refused connection (DECISION-0d5473ef).

### Non-functional

- NFR-46: Increment 2 adds no runtime dependency (NFR-40): the proxy program ships as SDK source run in the workspace's image, and every Docker operation stays the `docker` CLI.
- NFR-47: Nothing leaves a workspace except a tool result, an exported artifact, a snapshot inside Docker, or a connection the proxy allowed to a pair its spec names (amends NFR-43). No connection reaches a non-global address, another workspace or the host, from a command or a sandboxed server.
- NFR-49: Runs whose workspaces have no `egress`, no snapshot and no sandboxed server send the same requests and record the same events and rows as before M43, apart from the new columns, recorded empty, and `cpu_seconds`; their `agent_spec_hash` is unchanged.

### Acceptance

- AC-102: FR-159 to FR-163, on both stores:
  - **The spec:** each refused `egress` value raises `ValueError`.
  - **The proxy:** the allowed pair connects; an IP literal, an unlisted host, a listed name resolving to a private address, a ClientHello whose SNI differs, plain HTTP and a second request are each refused and recorded by reason; a direct connection and a DNS lookup from a command fail; a neighbour's network is unreachable.
  - **Lifetime:** no network or proxy remains after the run's end, `release_workspace`, `aclose()` or reaping.

## The records

- **DECISION-91e5d73a**: M43's close, and the five items M44 had to finish first: `take_reading` no
  longer returns an exit code it discarded; the no-authentic-line test gains a wrapper stopped
  mid-line; DECISION-630a34ff marked superseded by DECISION-d9a568e1; M43's brief corrected; a
  `docker` gate on M44 to M47.
- **DECISION-88dea779**: the owner's M44 pre-flight -- a per-command proxy token; a 10,000-connection
  cap with one `connection_limit` row; the proxy's fixed limits, not charged; 32 tunnels, 60 s idle;
  the two manual edits of `.genesis/project.json` (the `docker` gate on M44 to M47 with
  `-p no:cacheprovider` on all their gates; DECISION-630a34ff's status), as DECISION-11af2ee2 did.
- **KNOWLEDGE-bd42619c**: the probe behind FR-160's token form -- Python's urllib sends a proxy
  token only as the URL's password.
- **DECISION-26009e04**: the pre-review's eight findings and their repairs (below).
- **DECISION-a7a1b37b**: round 1's seven findings and their repairs (below), and FR-162 amended for
  F3 and F4. SPEC.md was re-approved after it.
- **DECISION-e510a5e3**: round 2's five findings and their repairs (below); FR-161 and FR-162
  amended, and SPEC.md re-approved. **Migration 0024 was amended in place** (the owner's choice, to
  keep one migration per milestone): its count CHECK now allows a count on `unrecorded` rows too. The
  dev database was brought in line in one transaction: that one constraint replaced (it only widens)
  and 0024's recorded checksum updated. Check both against the file yourself.
- **DECISION-f7114b8b**: the repairs for rounds 3 and 4 (below), FR-161 and FR-162 amended, and
  SPEC.md re-approved. No migration changed: the `unrecorded` row's count is updated through the
  same `update_connection` the limit row uses.
- **DECISION-0d5473ef**: round 5's repairs, as the owner chose them (below); FR-159 to FR-163
  amended. **DECISION-5c68ddfb** corrects its B1/N1 mechanism: an event write already begun runs
  as one transaction on a worker thread through `RunControl.store`, which absorbs cancels so the
  write lands (FR-50), so it cannot be cancelled; instead the write asks its run's gate on that
  thread just before it begins. **DECISION-7a3a2ae2** corrects N6 and N2: 0024 was already applied
  with its checksum recorded, so **migration `0025`** adds the unique index (P5-D23 amended: M46's
  snapshots move to `0026`), and a late pass inside a store write that never returns cannot be ended,
  so `aclose()` tells it to stop after its current write and cancels it. SPEC.md was re-approved
  after each.
- The standing rules for every example's checks: DECISION-a00773b0, DECISION-a7d6a1f5,
  KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's text).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5`; HEAD is `d4b5f0c` (M43). M44 is uncommitted, with SPEC.md's M44 amendments, README.md's
M44 rows, and migrations `agentsdk/migrations/0024_workspace_connections.sql` and
`0025_workspace_connections_unrecorded.sql`, **both already applied to the dev database** (the test
session's schema step applied 0025).

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** on a database error print the exception's type only.
- **Host:** Windows 11 Home, Python 3.11, 16 CPUs. **This laptop sleeps when idle, and an unguarded
  run once produced a false finding:** run anything over a few minutes under a wrapper holding
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.
- **Docker Desktop is running, Engine 29.6.2** (egress needs 28 or later). The tests use
  `python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df` and, as an image
  without python3, `redis@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2`,
  both on the host. **Never pull, build, prune, stop or restart anything.** The host has containers,
  volumes and networks of other projects: touch only what is labelled with your own
  `agentsdk.tenant` prefix, or `agentsdk-probe`. Report how many remain at the end (it must be 0).
  In Git Bash a path meant for inside a container is rewritten: use `MSYS_NO_PATHCONV=1`.
- **The internet:** the Docker tests and example 38 fetch `https://example.com` through the proxy,
  and resolve `localtest.me` (a public name for 127.0.0.1).

**Running tests**
- **One test session at a time.** Check that no other python or node process is running first.
  Run probes with `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M44-egress --timeout 3600000` in the background, kept awake: unit
  (`tests/test_egress.py`), regression (the full suite, about 32 minutes), and **`docker`** -- every
  Phase 5 Docker-marked test, `tests/test_egress.py` included, about 45 minutes.
- M44's own Docker tests: `pytest tests/test_egress.py -m docker -p no:cacheprovider`, about 4 minutes.
- **Prefer runtime mutation,** leaving no repository file changed; restore any edited file byte for
  byte, verified by SHA-256.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `workspace_connections`, `workspace_commands`, `workspaces`, then
`interruption_decisions`, `interruptions`, `standing_decisions`, `artifacts` by `source_run`,
`plan_node_states`, `plan_versions`, `run_events`, `messages`, `execution_manifests`, then the runs,
children first. Use tenant ids starting `SYN-m44r6`, and remove only rows your probes recorded.

## Task under review

### What M44 builds

- **`agentsdk/workspace/egress.py`** (new): the spec's parsing; the `Proxy` and its rules; `sni_of`
  and `client_hello`. It is also the very program the proxy container runs, as
  `python3 -c SOURCE CONFIG`, and the rules `FakeProvider` applies.
- **`DockerProvider`:** for a spec with `egress`, it creates an `--internal` network with an isolated
  gateway, and a proxy container hardened as a command with fixed limits. The proxy is started
  attached, so only the SDK reads its reports, and it listens on that network alone. Each command
  joins the network with its own token in `HTTPS_PROXY` and `https_proxy`, and the token is retired
  when the command ends. Removal stops the proxy gracefully, and the network goes with the
  workspace, including when it is found by listing.
- **The manager's connection book:** a recorder task writes a `workspace_connections` row and a
  `WorkspaceConnection` event for each report, under the command's own run. It drains before the
  workspace's removal is recorded. A dead proxy is recorded `proxy_ended` and fails its workspace
  closed.
- **`FakeProvider`:** `resolve`, `sites` and `max_connections`; `FakeCommand.connect`, `proxy`,
  `resolve_name`, `connect_address` and `egress_token`; and `kill_proxy`.
- **Example 38** (`scripts/38_egress.py`) and the gate, `tests/test_egress.py`.

### Every finding so far, answered

| Finding | Repair |
|---|---|
| **M43's carried items** (DECISION-91e5d73a) | All five closed in M44, as listed above. |
| **F1 (high, reproduced first-hand):** from an `--internal` network a command reached the Docker host through the network's gateway; rpcbind on 172.19.0.1:111 was open. | The network's gateway is isolated (Engine 28 or later, checked by version). A scan of the network's first eight addresses finds only the proxy's own port. Docker refuses an isolated gateway on a network that is not internal. |
| **F2:** 9,000 connections that never finished their request ran the proxy out of its 64 MiB, and egress then ended silently. | At most 64 connections may wait on their head at once. A proxy that dies anyway is recorded `proxy_ended`, and its workspace refuses later commands. |
| **F3:** a connection reset by its client left no record. | It is reported `client_reset`; a failure of the proxy's own is reported `proxy_error`. |
| **F4:** a finished command's token still named it, so a later command could borrow it. | A token is retired when its command ends. |
| **F5:** the proxy listened on the bridge too, so any bridge container could use it. | It listens on its workspace's network address alone. |
| **F6:** an image's python3 older than 3.11 passed creation, and the proxy then dropped every connection. | The proxy needs nothing past Python 3.8 (syntax checked at 3.8), and the image's version is checked at creation. |
| **F7:** hex IP literals passed as names. | Any form that `inet_aton` reads is refused. |
| **F8:** example 38's offline checks of a direct connection, a name lookup and a neighbour could never fail. | They run on Docker only. |
| **Found by the author after the probe:** urllib sends no `Proxy-Authorization` for a URL with only a user. | The token is the URL's password (KNOWLEDGE-bd42619c). FR-160 says so. |
| **Round 1 F1 (blocking):** the run, `release_workspace` and `aclose()` hung forever when the proxy's reports took more than 30 s to record. `_booked` cancelled the recorder, `RunControl.store` absorbed the cancel so its write would land, and the recorder parked on its empty queue. | Repaired as a pattern: `agentsdk.handle.settle(task, seconds, stop)` waits for a task that writes through the store within a bound, tells it to stop past the bound, waits once more, then leaves it. The recorder ends at a stop marker queued behind its reports, or after its current write once told to; nothing cancels it. No other site cancels and then awaits a task that writes through the store; the rest cancel runs, which stop at their own checkpoints, or tasks that never write. |
| **Round 1 F2:** example 38 and the Docker probe dialled 93.184.215.14 for the "direct connection" check, an address dead even from an unisolated container, so the check could not fail. | The probe dials every address example.com has, as the host resolves it. The check passes only if the address the allowed connection reached, as its row records, is among them, the host reaches it, and every direct attempt from inside failed. Docker tests make it fail on a network that is not isolated, without the address, and without the host's control; a container on the bridge reaches the address. |
| **Round 1 F3:** a kept workspace's `WorkspaceConnection` events were written after its run's `RunCompleted`. | FR-162 amended: an event only while its run is under way, then the row alone. A run's end first books, within the bound, what its proxy reported so far, so those keep their events, and waits for an event being written. |
| **Round 1 F4:** the `connection_limit` event carried count 1 while its row reached the true count. | FR-162 amended: the one event marks the limit reached and carries no count; the row carries the count. |
| **Round 1 F5:** a valid maximal egress spec (32 names of 253 characters) passed the Windows command line on the proxy's `create` and raised a raw ValueError. | The proxy and its network no longer carry the `agentsdk.spec` label, which repeated the allow list (a listing finds them by `agentsdk.workspace`). A line past the bound is `WorkspaceUnavailable`. A test holds a maximal spec under the bound with 2,048 characters to spare. |
| **Round 1 F6:** a port of 4,301 digits made `int()` raise: in the proxy a `proxy_error` row where `malformed` belongs, and in `parse_egress` the wrong message. | Five digits at most, checked before `int()`, in both. |
| **Round 1 F7:** `space.commands` was never pruned, and the proxy's stderr pipe never read. | A command is forgotten by a marker queued behind its reports once its provider's call ends, so no report loses its run. The proxy's stderr is drained: a test writes 256 KiB there before the proxy starts. |
| **Round 2 (orchestrator):** past the removal's bound the recorder returned with its queue unread: those connections had no row, against FR-162. | Past the bound, the recorder counts every connection left in its queue, and every refusal at the limit its row does not yet count, in one `unrecorded` row (one bounded write, under the workspace's owning run). FR-162 amended: each connection recorded individually or counted there. |
| **Round 2 (1):** past the 10,000 cap each refusal cost one store update (297 for 298). | The proxy reports the limit at the first refusal, then the count at most once a second from one timer, and the final count when it stops. Lines, queue and writes are bounded together. FakeProvider's removal now stops its proxy as Docker's does, so the final count arrives there too. |
| **Round 2 (2):** a connection still undecided when the proxy stopped was counted but never reported. | Reported `stopped` (FR-161 amended), on the fake and on Docker. |
| **Round 2 (3):** example 38's direct check can fail falsely if the CDN rotates the address. | Declared in the README: it can only fail, never pass, and its message names the cause. |
| **Round 2 (4):** a connection of no command on a plan workspace was recorded under the creating child run. | Recorded under the plan's top-level run, row and event, while that run is under way. `for_run` takes the top-level run's emitter and store from the Runner, and `end_plans` ends that binding as a run's end does. |
| **Round 3 A (Opus 5):** `RunWorkspace.end`, cancelled during the removal, marked its run ended and re-raised without `settled()`: no barrier, no booking lock, so an event already being written was not waited for. | `end` calls `settled()` in a `finally`, on every path: what was reported is booked first, within the bound, and an event being written lands before the terminal event. A second cancel during `settled()` still marks the run ended (its own `finally`). |
| **Round 3 A, older half (orchestrator):** `Runner._end_workspace` said "Never raises", but passed a cancel through, and `_cancelled` called it unguarded, so a second cancel as the workspace ended skipped `RunCancelled`. | `_cancelled` absorbs a `CancelledError` from `_end_workspace` and ends the run cancelled as it would have; the docstring now says what it raises. A Runner test lands a cancel as the removal ends and gets `RunCancelled` last. |
| **Round 3 B (Opus 5), three degradations past the removal bound:** (a) a queued tunnel end left its allowed row with no bytes; (b) a queued proxy death wrote no `proxy_ended` row; (c) reports behind the stop marker, or arriving after a removal that passed its bound, were neither recorded nor counted. | (a) and (b): past the bound, the queued ends and deaths are still written, after the count. (c): a recorder's end counts what is queued behind its marker, and from then on each report the provider delivers starts, if none is running, a pass of its own that counts it on the same row (`WorkspaceManager.arrived`). `aclose()` waits for those passes within the bound. |
| **Round 3 (orchestrator):** `max_connections` could be set on `FakeProvider` only, so the final limit count was never exercised on Docker. | `DockerProvider(max_connections=...)`, passed in the proxy's config; a Docker test lowers it to 1 and gets the final count 3 from four connections. |
| **Round 4 (Haiku 5.5), reproduced:** a connection whose row the store refused was neither recorded nor counted (`book()`'s `except Exception: pass`). 4 of 6 accounted with writes 2 and 3 failing. | Counted on the `unrecorded` row. A workspace has at most one such row, its count updated as more is counted; refusals at the limit the limit row does not count are on it too, never twice. If the store refuses that row as well, it is missing, never guessed (README row, DECISION-f7114b8b). |
| **Round 4's lead (orchestrator):** the docker gate's faulthandler dumped `test_compute`'s cancel-layer `[command removal]` test at 300 s, inside `WorkspaceManager._remove`. | Measured, not a hang: it passed in 348.61 s on M44 and 348.37 s on HEAD (M43) in a worktree, so M44 adds nothing. It ran example 36 twice; with by-name removal off, the sweep does that work. The test is split into one example run per test (edit declared below), each about 175 s. No unclosed-transport warning in either run. |
| **Round 5 B1 (blocking, Sonnet 5.5, reproduced):** `settled()` waited for the booking lock after its `finally`, so a second cancel during the barrier wait skipped it, while `_book` was already past its `ended` check and writing: the event landed 2.4 s after `RunCancelled`. Round 3 A's test held the lock by hand and patched the removal to raise, so no real write was ever in flight. | `settled()` runs its work through `agentsdk.handle.through`, which holds a cancel until the work ends and then raises it. Two new tests reach the state: a manager-level one with a real 1.5 s write on a worker thread and a real second cancel during `settled()`, and a Runner-level one by the reviewer's route. A removal waits for the recorder, and with it the event, before `settled()`, so that route uses a `keep=True` workspace. The author's first Runner-level test passed on the unrepaired code for exactly that reason; it was caught by running the new tests before the repair, and changed. |
| **Round 5 N1:** `settled()` waited without a bound on an event that never returned. | Each connection event is written through `StoredEmit`, which the Runner now gives a run's binding. On its worker thread, just before the write begins, it asks the run's `_Gate`. `settled()` waits for the lock within the removal bound; past it, it shuts the gate. A write not yet begun is then shut out, its row standing alone; only one already inside its transaction is waited for, bounded as every store write is (DECISION-5c68ddfb). An emitter that is not a `StoredEmit` is gated where it is called. **The gate replaced round 1's `binding.ended` flag outright:** the mutation run showed the two round 1 F3 mutants on that flag surviving, because the gate already shut out what the flag did. Two mechanisms for one rule, so the flag is gone: `settled()` shuts the gate where it set the flag (after the barrier, before the lock), and only a write already inside is waited for. |
| **Round 5 B2 (blocking):** `_end_workspace` ran `end` then `end_plans` with no `finally`; the failed path's call sat in `except Exception`, so a cancel during a failed run's removal escaped with no terminal event; and a cancel out of `end` skipped `end_plans` silently. | `_end_workspace` runs both steps through `through`, `end_plans` in a `finally`: a cancel is held until both are done, then raised. The failed path absorbs it and ends `failed` with `RunFailed` and its row, its failure having come first; `_cancelled` keeps its guard; the completion path's cancel reaches `_cancelled` as before. |
| **Round 5 N3:** a refused `proxy_ended` row was neither recorded nor counted. | Counted on the `unrecorded` row, and its event still written. |
| **Round 5 N2:** a late pass waiting on a store that never answered outlived `aclose()`. | Past its bound, `aclose()` sets a stop flag and cancels each late pass, then waits once more within the bound. The flag is checked before each write, because `RunControl.store` uncancels a cancel it absorbed and the pass would otherwise carry on through every queued tunnel end; a test shows that. A pass inside a store write that never returns runs on alone (README). |
| **Round 5 N6:** the one-`unrecorded`-row rule lived in memory alone, and an insert that committed and then raised would have been written again. | Migration `0025`: a unique partial index, one `unrecorded` row per workspace; the in-memory store refuses a second as the index does. The row's id is fixed before its first write, and each write tries an update on it before an insert. |
| **Round 5 N4:** the SNI check read only the first `server_name` entry; a two-name ClientHello tunnelled to the listed host. | `sni_of` reads every name in every `server_name` extension; more than one gives them comma-joined, which no host equals, so the tunnel is refused `sni_mismatch`. |
| **Round 5 N5:** example 38's `host_direct` had no control, and `neighbour` counted `ConnectionRefusedError` as failed. | Both have a control run in the neighbour's proxy, on the bridge as well: the host's address and port answer it, and the neighbour's proxy answers on its own address. A refused connection to the neighbour fails the check, as it proves the network was reached. Docker tests break each control. |
| **Round 5 N8:** `CONNECT example.com:00443` matched `example.com:443`. | The proxy refuses a port written with a leading zero, `malformed` (FR-161); `WorkspaceSpec` already did. |
| **Round 5 N7:** no test overlapped the recorder's last counting pass with a late one. | A test runs both at once on a slow store and gets one row with every report counted once. |
| **Round 5, governance (Sonnet 5.5):** after the reject, `.genesis/project.json`'s `independent-review` gate record still read `pass` with round 3's reason while the task was rejected; only KICKOFF showed it stale. | Not in this repository's code: genesis-kit writes that record. Read the gate state from KICKOFF.md's gates line, and report it if it misleads you again. |

### How the author checked it

- **Tests:** `tests/test_egress.py` has 163 tests without Docker and 28 with it, all
  passing; the Docker tests ran kept awake. Every round 5 test was run on the unrepaired code first
  and failed for the reason it names, except the overlap test (N7, coverage) and two controls.
- **Mutation:** 123 mutants (56 before round 1, 18 for its repairs, 11 for round 2's, 11 for
  rounds 3 and 4, 27 for round 5's), one exact edit each, all killed by the tests they name. Six
  earlier mutants were retargeted where round 5 moved the code (the two on round 1's `ended` flag
  now edit the gate that replaced it). In round 5, after the gate change, "events after the run's
  end" first survived: no test booked a report after the run's end through an emitter that is not a
  `StoredEmit`; one does now. "No barrier before the end" was killed in two runs and survived the
  third: round 1's test reached it only by timing; a deterministic test now kills it every time.
  In round 3 "refusals the limit row missed unseen" first errored
  instead of failing (the edit left an empty block) and was fixed. "A backlog past the bound dropped
  uncounted" first survived: the recorder's new end-of-life pass counted the queue anyway, losing only
  the one report taken as the stop was seen, which the round 2 test could not tell from a tunnel's
  end. A deterministic test of that report now kills it. In round 2, "a run never marked ended" first survived:
  the line it edited had become the cancelled-end path, which no test reached; it now edits the line
  that does the work, and a new mutant and test cover the cancelled end. Some kills are worth knowing. "Commands forgotten" needed a new test with
  two runs on one plan workspace. "No graceful stop" needed the stop to be timed, because an idle
  tunnel closes by itself after 60 s. In round 1, "the direct attempt unread" first survived: on an
  open network the host's port also opened, so the check failed for another reason, and the test
  now requires the direct attempt's own failure. "No drain before removal" names the F1 tests too,
  because a run's end now books what was reported, and the drain is what ends the recorder. The list
  is in this brief's last section.

### Edits to approved tests

- `tests/test_distribution.py`: `EXPECTED_EXAMPLES` gains `38_egress.py` (FR-163).
- `tests/test_compute.py` (M43's carried items, DECISION-91e5d73a): `take_reading` now returns
  `(found, usec)`, so its test asserts `(True, 5)` and `(False, None)` where it asserted `(5, 3)` and
  `None`, and gains an authentic line with no CPU figure, `(True, None)`. The Docker test of a
  command with no authentic line is parametrized: a wrapper stopped before its line, as before, and
  one stopped mid-line, its nonce cut in half with no newline. Both strengthen coverage; nothing is
  asserted less.
- `tests/test_sandbox_contention.py`: in `without()`, the "network" and "pid limit" stand-ins for
  `hardening` now take `network="none"` and pass it on. M44 gave `hardening` that argument, and the
  one-argument stand-ins raised TypeError at every command's creation. The first `docker` gate
  failed both tests with a check of None instead of False. Each stand-in still removes exactly its
  one flag; nothing else in the test changed.
- `tests/test_compute.py` (round 4's lead, DECISION-f7114b8b):
  `test_on_docker_the_cancel_check_without_one_layer_fails_or_is_covered` ran example 36 twice per
  layer and passed faulthandler's 300 s. It now runs the first half only: without the one layer,
  the check passes exactly when `CANCEL_LAYERS` names a cover. The second half is
  `test_on_docker_the_cancel_check_without_a_layer_and_its_cover_fails`, with the same three
  layers: without the layer and its cover, the check fails. The assertions are the ones the test
  made; a layer with no cover is skipped in the second, as the first already asserts its failure.
- `tests/test_egress.py`, M44's own (round 5): two round 3 A tests asserted `binding._binding.ended`;
  with the flag gone they assert `binding._binding.gate.enter() is False` -- that the run's end
  lets no event in, which is what the flag stood for.

## Attack these first

0. **Round 5's repairs, first.** Is each closed, did a repair open anything, and does each repair's
   test reach the state it names?
   - **The gate (B1, N1):** can a `WorkspaceConnection` event, a limit event or a `proxy_ended` event
     still land after a terminal event: by a third cancel, on a plan's child or its top-level run
     (`top=`), with a kept workspace, through `end_plans`, or between `settled()` shutting the gate
     and the write asking it? Can `settled()` wait on the booking lock for another run's write? Is
     the run's end bounded whenever the store answers?
   - **`through` (B1, B2):** can it swallow a cancel, raise a second one, leave its work running
     unowned, or turn the work's own exception into something else? Does the failed path absorbing
     a cancel hide one that should have ended the run `cancelled`?
   - **`aclose()` and late passes (N2):** can the stop flag, which is the manager's own, end a
     counting pass that should still have run, such as a recorder's last pass for a space `aclose()`
     is finishing?
   - **The `unrecorded` row (N3, N6):** with migration 0025, can a count be lost, or the index
     refuse a write the manager meant as an update? Do the file, its recorded checksum and the live
     index agree?
   - **SNI (N4) and the port (N8):** can a ClientHello or a `CONNECT` line still front another
     server, or a valid one now be refused?
   - **Example 38's controls (N5):** can a check pass with its control broken, or its control pass
     when the address is dead?
   - **Left unprobed by round 5:** the Windows 32,767-character command-line bound (round 1 F5),
     example 38 live on Postgres in a fixed tenancy, the true 10,000 cap on Docker, and the
     commit-then-raise insert (now N6's). Probe what you can.
   **Rounds 3 and 4's repairs.** Is each still closed?
   - **The cancelled end:** with a cancel during the removal, and a second one during `settled()`,
     can an event still land after the terminal event, or `RunCancelled` be skipped? Does
     `_cancelled` absorbing a `CancelledError` hide a cancel that should have propagated?
   - **One `unrecorded` row:** with a store that refuses some writes, one that never returns, and
     reports arriving after the recorder ended, is every connection recorded or counted exactly once?
     Can two passes write two rows, or the count go down? Can a refusal at the limit be counted on
     both rows?
   - **The late pass (`WorkspaceManager.arrived`):** can it be left running, outlive `aclose()`
     unbounded, or strand a report in the queue with no pass to read it?
   - **Past the bound:** a queued tunnel's bytes and a proxy's death are written after the count.
     Can those writes hold anything up when the store never returns?
   - **`DockerProvider(max_connections=...)`:** does the cap reach the proxy, and is the final count
     right on Docker?
   - **The split `test_compute` test:** does it assert exactly what the old one did?
   **Round 2's repairs.** Is each of the five still closed?
   - The `unrecorded` row: can a connection still go uncounted, or be counted twice (recorded and
     also counted)? Does the one write respect the bound when the store never returns?
   - The limit timer: can a count be lost, or the timer outlive the proxy? Is the final count right
     on Docker, where the proxy stops on SIGTERM?
   - `stopped`: can a connection be reported twice, or a decided one re-reported?
   - The top-level run: can a connection, event or row land under the wrong run now, in a plan or
     outside one? Can an event follow the top-level run's terminal event?
   - Migration 0024 and the dev database: do the file, its recorded checksum and the live constraint
     agree?
   **Round 1's repairs.** Is each of F1 to F7 still closed?
   - F1: can any path still wait unbounded on a task that writes through `RunControl.store`? Think of
     a store that never returns, a recorder told to stop mid-write, and M45 to M47's recorders.
   - F3: can a `WorkspaceConnection` event still land after its run's terminal event, a plan's
     child included? Is any connection made during a run left with no event?
   - F2: can example 38's direct check pass when the workspace's isolation is gone?
1. **Getting out (NFR-47), on Docker.** Use `DockerProvider` directly with your own tenant label.
   The pre-review's report covers what held up. Look for any other way:
   - the gateway, now isolated;
   - IPv6;
   - the embedded resolver at 127.0.0.11;
   - parsing tricks in the request head and the ClientHello;
   - the proxy's listening address;
   - a neighbour workspace;
   - the host.
2. **The proxy under attack:** can a command crash it, wedge it, exhaust it, or forge a report line?
   Does every connection still end up recorded?
3. **Attribution and records (FR-162):**
   - Is a connection ever recorded under another command or run, a plan workspace included?
   - **A race the author found and measured, not proven absent:** the proxy prints a decision before
     answering, but its line reaches the SDK through `docker start -a` while the command's end comes
     through its own `docker run`. If the line were read after the command's token was retired, the
     row would name no command. 40 commands refused and exiting at once were all named correctly
     (`commands 40 ... named to their command 40 ... with no command 0`). Nothing orders the two
     streams by construction: try to make it happen.
   - Does a token appear in any row, event, manifest, log or error?
   - Do the limit row, the bytes and `proxy_ended` hold on both stores?
4. **Lifetime (FR-160):** after the run's end, `release_workspace`, `aclose()`, reaping and a
   failed creation, does any network or proxy remain?
5. **NFR-49:** with no egress, is anything different from before?
6. **The fake against Docker, and example 38:** can any check pass when its subject did not happen?
7. **M43's carried items:** are all five truly done?

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M43,** and the README's M44 rows.
- **Owner's choices:** DECISION-88dea779, DECISION-26009e04, and every earlier one M43's brief listed.
- **Egress reaches out only for a proxy-aware client,** and no name resolves inside a workspace (P5-D20).
- **A token is on the host's docker command line** for that container's `-e HTTPS_PROXY`, because the
  CLI reads `HTTPS_PROXY` from its own environment.
- **A store that never returns** can hold a run's end, as it would hold the run's terminal event;
  and a late pass inside such a write runs on after `aclose()` (README, DECISION-5c68ddfb,
  DECISION-7a3a2ae2).
- **Examples and tests read private state:** `runner._sessions_for`, `_workspace_store`, the
  provider's `_spaces`, and `RecordingRunner`'s override of `Runner._open`.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Under the owner's rule,
  every finding is still repaired in M44, so list each one, and mark which you would block on.
- **Clean up** every run, row, container, volume, network, temp folder, process and lock you create,
  and say what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M44-egress --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M44-egress --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict.

## Appendix: the author's mutants

- proxy: an IP literal unchecked
- proxy: the allow list unchecked
- proxy: one global address enough
- proxy: an unresolved name passes
- proxy: the SNI unchecked
- proxy: no SNI unchecked
- proxy: a second request unchecked
- proxy: not TLS unchecked
- proxy: any method
- proxy: never busy
- proxy: never idle
- proxy: no connection limit
- proxy: no end report
- proxy: a token of any user
- proxy: a token of any characters
- proxy: dials the name again
- proxy: close does not wait for the tunnels
- spec: a numeric last label
- spec: more than 32
- spec: repeats
- spec: the record names an empty egress
- fake: tokens unmapped
- fake: a proxy without egress
- manager: commands forgotten
- manager: no drain before removal
- manager: the limit's count not kept
- manager: no connection event
- manager: never watching
- docker: commands off the network
- docker: the proxy's memory
- docker: tokens unmapped
- docker: no graceful stop
- docker: the network left
- docker: orphan networks unlisted
- docker: the network not internal
- docker: the proxy off the bridge
- docker: python3 unchecked
- docker: no proxy URL
- docker: no egress joins the bridge
- example 38: refusals counted, not named
- F7: inet_aton forms accepted
- F3: a reset reported as the proxy's error
- F3: the proxy's own failure unreported
- F2: heads wait unbounded
- F6: the version unnamed
- F6: any version ready
- F5: listening everywhere
- F5: listening everywhere (docker)
- F1: the gateway not isolated
- F1: any engine
- F4: docker tokens kept
- F4: fake tokens kept
- F2: commands after a dead proxy
- F2: a dead proxy unreported
- F2: a removal reported as a death
- F8: Docker-only checks offline
- r1 F1: the recorder cancelled, then awaited
- r1 F1: no stop marker
- r1 F1: never told to stop
- r1 F1: told to stop, but carries on
- r1 F3: events after the run's end
- r1 F3: a run never marked ended
- r2: a cancelled end leaves its run under way
- r1 F3: no barrier before the end
- r1 F4: the limit event carries the count
- r1 F5: the spec label on the proxy
- r1 F5: the proxy line unchecked
- r1 F6: a spec port unbounded
- r1 F6: a request port unbounded
- r1 F7: commands never forgotten
- r1 F7: a command forgotten before its reports
- r1 F7: the proxy's stderr undrained
- r1 F2: any address the proxy reached
- r1 F2: no host control
- r2: a backlog past the bound dropped uncounted
- r2: refusals at the limit left uncounted
- r2: the limit reported on every refusal
- r2: no limit timer
- r2: no final count at close
- r2: undecided at close unreported
- r2: the fake never stops its proxy
- r2: no command, the creating child's run
- r2: the Runner passes no top-level run
- r2: the top-level run never ended
- r1 F2: the direct attempt unread
- r3: a refused row dropped
- r3: refusals the limit row missed unseen
- r3: the recorder's end counts nothing
- r3: never marked booked
- r3: nothing counted after the recorder ended
- r3: a new unrecorded row each time
- r3 B: a queued tunnel end or death unwritten past the bound
- r3 A: the cancelled end without the lock
- r3 A: _cancelled unguarded
- r3: the cap not in the proxy's config
- r3: Docker ignores its cap
- r5 B1: settled not held through a cancel
- r5 B1: through lets a cancel straight out
- r5 B1: through swallows the cancel
- r5 N1: past the bound, the lock waited for anyway
- r5 N1: a write inside not waited for
- r5 N1: no bound on the lock
- r5 N1: the gate never asked on the thread
- r5 N1: a shut gate lets writes in
- r5 N1: busy never says one is inside
- r5 N1: the Runner's events ungated
- r5 N1: a plain emitter unaccounted
- r5 B2: end_plans skipped on a cancel
- r5 B2: a cancel during the workspace end not held
- r5 B2: the failed path unguarded
- r5 N3: a refused death raised
- r5 N3: a refused death uncounted
- r5 N2: late passes never cancelled
- r5 N2: never told to stop
- r5 N2: told to stop, but only between drains
- r5 N6: the memory store allows a second unrecorded row
- r5 N4: only a list's first name read
- r5 N4: a second server_name extension unread
- r5 N8: a CONNECT port with leading zeros
- r5 N5: a refused connection counted as failed
- r5 N5: no neighbour control
- r5 N5: the host control unread
- r5 N5: the neighbour control unread
