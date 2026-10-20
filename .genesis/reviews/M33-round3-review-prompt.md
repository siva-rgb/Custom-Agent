You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M33 was implemented by Opus 5.5. Opus 5 rejected round 1, and Fable 5.1 rejected round 2.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

Re-derive the requirements from SPEC.md: "#### M33" (FR-122 to FR-126), AC-89, AC-90, NFR-34,
NFR-35, NFR-37, NFR-38, NFR-39. Read the two rejections:
- KNOWLEDGE-34749df5, round 1: starting and listing a server had no bound;
- KNOWLEDGE-7d61cfab, round 2: a request write had no bound, and `aclose` was not final.

Read DECISION-7b366ac6, the owner's scope for this round. The earlier prompts in
`.genesis/reviews/` (`M33-review-prompt.md`, `M33-round2-review-prompt.md`) describe M33 in full;
read them first. Only what round 3 changed is below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass `--help` to a genesis command.**

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string.** Strip the quotes if you export .env by hand: round 2's reviewer lost
  three live tests to a quoted BASE_URL.
- **Host:** Windows, Python 3.11, `mcp==2.3.0` in the venv.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256, and never touch an applied migration.
- **MCP tests start real server processes, some of which never answer on purpose.** After every
  run, check for a leftover `mcp_servers.py` or `time.sleep(600)` process. Remove only those your
  own run started.
- **The full suite takes about 11 minutes; the unit gate about 80 seconds.**

**Gates:** `genesis gate . M33-mcp-stdio-client --timeout 1500000` in the background.

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id.

## Task under review

**M33-mcp-stdio-client**, **round 3**, risk **high**. HEAD is `ca8783d`; M33 is uncommitted.

### The invariant this round states

**The SDK owns the deadline of every wait that can block on a server.** Rounds 1 and 2 each listed
the awaits they could see and bounded those, and each missed one. Round 2 relied on the library's
read timeout for calls, but the library writes the request first (`jsonrpc_dispatcher.py`, around
line 410) and arms its read clock only after (around line 414). Every wait on a server now sits
under an SDK `asyncio` bound:
- **the start, the protocol exchange and the listing:** `asyncio.wait_for(ready, connect_timeout)`,
  with the connection task cancelled through `_end()` on failure;
- **every call:** `asyncio.wait_for(call_tool(...), call_timeout + 1)`;
- **closing:** `close()` and `_end()`, each bounded by `_CLOSE_SECONDS`;
- **the connection lock:** its holder is bounded by `connect_timeout`.

### What round 3 changed

- **`agentsdk/mcp/__init__.py`, `_call`:** the SDK deadline above.
  - It runs one second past `call_timeout`, so the library's own read timeout still decides a slow,
    honest tool, which keeps its connection.
  - When the SDK deadline fires first, the request write was blocked and the stream may hold half a
    message. The connection is ended, the call fails `ToolTimeout`, and the next call starts the
    server again under the restart-once rule.
- **`McpClients.aclose()`** adds its loop to `_closed`. `connection()` on a closed loop returns a
  dead `_Connection`, which never starts a server, so a run still going when its `Runner` closes
  gets no new server (NFR-39).
- **`tests/conftest.py`,** an approved file: a new autouse fixture, `asyncio_tasks_dumped_on_a_stall`.
  On `faulthandler_timeout` it prints every running event loop's tasks with their stacks, to the
  stream pytest's own fault handler writes to. faulthandler alone shows only thread stacks: the
  loop polling, not the coroutine that waits.
- **`tests/fixtures/mcp_servers.py`:** `freeze` gains its branch in `call()`. Round 2's reviewer
  noted it fell through to "no tool".

### Tests (`tests/test_mcp_client.py`, now 44; the unit gate is 50)

- **`test_every_call_after_a_wedged_write_returns_within_its_bound`,** at 70 KB and at 2 MB. The
  sequence is: freeze, then a large call, then two small calls. The large call fails `ToolTimeout`,
  and both small calls succeed after one restart. Two server processes were started, and both
  ended. The test bounds itself with `asyncio.wait_for`, so a missing bound fails it rather than
  hanging.
- **`test_a_run_in_flight_across_aclose_starts_no_server`.**
- **`test_a_stalled_test_dumps_its_asyncio_tasks_too`:** a nested pytest session with the suite's
  conftest copied unchanged. Its stuck test passes once its own wait times out, so no failure
  traceback can show the waiting line; only the dump can.
- **Round 2's single-call test is replaced** by the wedged-write test.

### What the author ran

1. **Tests first:**
   - both wedged-write cases hit the test's own bound;
   - the `aclose` test showed the run reopening a server to answer;
   - the stall test lacked the dump.
2. **Mutation run, round 3: 4 of 4 killed:**
   - the library timeout only;
   - the wedged connection kept;
   - `aclose` not closing the loop;
   - no task stack.

   The last first survived twice: a task's repr names its coroutine, and the inner test's failure
   traceback showed the line. The inner test now passes, and the assertion needs the stack's
   source line. Files were restored by SHA-256. Rounds 1 and 2 killed 22 and 6.
3. **Gates,** both green on source hash `eb33cbc8`:
   - `unit`: 50 passed in 79.99s.
   - `regression`: 2222 passed in 652.16s (started 07:42:32 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m33-r3-probes` in your own scratchpad; tenant ids starting `SYN-m33r3`.

1. **The invariant.** Find any wait that can block on a server, or on its process, that is not under
   an SDK bound. Work from the code and the library together, not from a list. Beyond the obvious
   waits, consider:
   - concurrent calls on one connection while one is wedged;
   - a slow listing during a turn's refresh;
   - process exit, stdin closing and pipe draining;
   - cancellation racing the deadline;
   - two runs waiting on one connection's lock.
2. **The discard.** Does ending a wedged connection fail other in-flight calls on it cleanly, leave
   no process, task or handle, and count correctly against the single restart? Can a slow but
   honest tool, finishing between `call_timeout` and `call_timeout + 1`, be mistaken for a wedge?
3. **`aclose` is final.** Runs on several loops; `aclose` from another loop; a run started after
   `aclose`; calls in flight at `aclose`.
4. **The stall dump.** Does it print what a real stall needs, and stay harmless? Check a test that
   finishes, a sync test, a loop in another thread, and many tests.
5. **Rounds 1 and 2's items,** briefly re-checked: connect bound, page cap, retry interval,
   wildcard, trust, tenancy, secrets.
6. **The approved-test edits,** now including `tests/conftest.py`.
7. **Regressions:** the full suite, and example 27 live if you can afford it.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M32,** rounds 1 and 2's clarifications, and the README's M33
  rows.
- **A server slower than its `connect_timeout`, or a request write slower than `call_timeout` plus
  one second,** is treated as unavailable or wedged. That is the bound working.
- **The library's fixed two-second termination grace** after stdin closes can add to a bound.

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
  control approve . M33-mcp-stdio-client --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M33-mcp-stdio-client --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.
