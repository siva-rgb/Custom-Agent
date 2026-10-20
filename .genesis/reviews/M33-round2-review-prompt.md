You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Sonnet 5** if Fable 5.1 fails
to start. M33 was implemented by Opus 5.5. Opus 5 rejected round 1. Bring no memory of an earlier
review into this one: if your session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md: "#### M33" (FR-122 to FR-126), AC-89, AC-90, NFR-34,
NFR-35, NFR-37, NFR-38, NFR-39. Read KNOWLEDGE-34749df5 (the round 1 rejection) and
DECISION-4b1ea0b8 (the owner's scope for this round). Round 1's prompt,
`.genesis/reviews/M33-review-prompt.md`, holds the full description of M33, its eight
clarifications and its approved-test edits; read it first. Only what round 2 changed is below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass `--help` to a genesis command.**

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string.**
- **Host:** Windows, Python 3.11, `mcp==2.3.0` in the venv.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256, and never touch an applied migration.
- **MCP tests start real server processes, some of which never answer on purpose.** After every
  run, check for a leftover `mcp_servers.py` or `time.sleep(600)` process. Remove only those your
  own run started, by command line.
- **The full suite now takes about 12 to 13 minutes, and the unit gate about 85 seconds.** The suite
  took about 8 minutes at round 1. Round 2 adds about 35 s of tests; the rest of the increase came
  while the host was measurably slower.

**Gates:** `genesis gate . M33-mcp-stdio-client --timeout 1500000` in the background.

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id.

## Task under review

**M33-mcp-stdio-client**, **round 2**, risk **high**. HEAD is `ca8783d`; M33 is uncommitted.

### The defect round 1 found

Starting a server, its protocol exchange and its listing had no bound. Only `call_tool` carried a
timeout. A server that started but never spoke MCP hung the run before its manifest was written:
- a launcher printing a banner line;
- a server reading every request and answering none.

The listing loop counted tools, not pages, so empty pages carrying a cursor never ended. Round 1
also promoted a caveat: `srv__*` allowed a tool named exactly `srv`.

### What round 2 changed, in `agentsdk/mcp/__init__.py` unless named

- **`StdioServer.connect_timeout`:** default 30 s, validated as `call_timeout` is, and recorded in
  the manifest. It bounds the server's start, the discover or initialize exchange and the whole
  listing: `asyncio.wait_for(ready, connect_timeout)`.
- **When a connect fails:** the connection's task is cancelled through `_end()`, and the cancelled
  `Client` ends the server.
- **The listing** reads at most `_MAX_PAGES` = 100 pages, as well as `_MAX_TOOLS` tools. Past the
  last page it raises, and the server is reported unavailable.
- **Closing:** `close()` waits `_CLOSE_SECONDS` = 10 for the task, then cancels it and waits again,
  bounded.
- **A failed connection is not retried for `_RETRY_SECONDS` = 60,** so a silent server costs one
  `connect_timeout` a minute, not one per turn.
- **Every other await in the module was swept.** A call to a server that has stopped reading its
  input was already bounded by the library's read timeout, and a test now pins it.
- **`agentsdk/permissions.py`:** `<alias>__*` allows a name only with a tool part after the `__`.
- **`README.md`:** the inherited environment is declared, the library's twelve names, without TMP.
- **`pytest.ini`:** `faulthandler_timeout = 300`. One round 2 regression gate stalled at 68% with no
  trace and was killed at its limit. A clean full run with a 120 s fault handler did not reproduce
  it, so a stalled test now dumps every thread's stack into the gate's own output.

### Tests (`tests/test_mcp_client.py`, now 41; the unit gate is 47)

- **New fixture modes** in `tests/fixtures/mcp_servers.py`:
  - `--banner`, `--silent` and `--endless-pages`;
  - `--deaf`, which ignores its input ending;
  - a `freeze` tool, which answers and then never reads its input again.
- **`test_a_server_that_never_answers_is_unavailable_within_its_bound`,** for three modes:
  - each bounded by `asyncio.wait_for` in the test itself, so a missing bound fails it;
  - the endless listing runs with a 120 s `connect_timeout`, so only the page cap can end it;
  - two turns, with exactly one `McpServerUnavailable`, which pins the retry interval.
- **Three more tests:** a call to a frozen server; closing a deaf server; the wildcard.
- **Ordinary tests use `connect_timeout=120`.** One gate run failed a modern-server test with its
  tools absent, while the host was slow: that unit gate took 270 s against a usual 85 s, and the
  modern fixture normally starts in about 1.5 s. Its assertion now prints the MCP events.
- **The output-mismatch test is marked as the pin** of the library-message mapping.

### What the author ran

1. **Tests first:** the new tests failed before the fix (the banner case hung).
2. **Mutation run, round 2: 6 of 6 killed:**
   - no connect bound;
   - no page cap;
   - a retry every turn;
   - no cancel after the close bound;
   - any prefix for the wildcard;
   - `connect_timeout` unchecked.

   Files were restored by SHA-256. Round 1's 22 mutants were killed before.
3. **Gates,** both green on source hash `960ad6a3`:
   - `unit`: 47 passed in 82.35s.
   - `regression`: 2219 passed in 751.43s (started 17:52:41 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m33-r2-probes` in your own scratchpad; tenant ids starting `SYN-m33r2`.

1. **Is every wait on a server now bounded?** Find any await on server-controlled input still
   without one: the start, the exchange, every page, every call, the input closing, process exit,
   cancellation, `aclose` with calls in flight, two runs waiting on one connection's lock.
2. **The bounds' side effects.** Does a timed-out or cancelled connect leave a process, a task, a
   file handle or a lock held? Is the retry interval right across tenancies and loops? Can a slow
   but honest server be cut off in a way the README does not state?
3. **The round 2 hang.** Can you find what stalled the regression gate once? Look especially at
   anything the MCP tests leave running.
4. **The wildcard** against every name shape.
5. **The new tests and fixtures:** does each fail without its bound, rather than hang?
6. **Round 1's items,** briefly re-checked on the new code: trust, who sees what, tenancy, secrets.
7. **Regressions:** the full suite, and example 27 live if you can afford it.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M32,** round 1's clarifications, and the README's M33 rows.
- **A server slower than its `connect_timeout`** is reported unavailable. That is the bound working.

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
