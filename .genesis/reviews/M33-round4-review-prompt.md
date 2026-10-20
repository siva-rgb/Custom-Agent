You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Fable 5.1** if Opus 5 fails to
start. M33 was implemented by Opus 5.5. Earlier rounds were all rejected: Opus 5 (round 1), Fable
5.1 (round 2), Sonnet 5 (round 3). Bring no memory of an earlier review into this one: if your
session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md: "#### M33" (FR-122 to FR-126), AC-89, AC-90, NFR-34,
NFR-35, NFR-37, NFR-38, NFR-39, and P4-D16 to P4-D31. Read the three rejections:
- KNOWLEDGE-34749df5, round 1: start and listing unbounded;
- KNOWLEDGE-7d61cfab, round 2: an unbounded request write, and `aclose` not final;
- KNOWLEDGE-27138d74, round 3: a held connection restarting a server after `aclose`.

Read the owner's decisions for this round:
- DECISION-7889f649: the lifecycle redesign;
- DECISION-7c9da7ae: server regular expressions in a subprocess, and four clarifications of the
  signed spec.

Read `.genesis/reviews/M33-review-prompt.md` for M33's full description, eight clarifications and
approved-test edits. Only round 4's changes are below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass `--help` to a genesis command.**

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string.** Strip the quotes if you export .env by hand.
- **Host:** Windows, Python 3.11, `mcp==2.3.0` in the venv.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256, and never touch an applied migration (`0014` to `0016`).
- **MCP tests start real server processes, some of which never answer on purpose.** After every
  run, check for a leftover `mcp_servers.py` or `time.sleep(600)` process. Remove only those your
  own run started.
- **A probe that runs a catastrophic regular expression in your own process freezes it,** because
  Python's `re` holds the GIL. Run such a probe in a child process you can end.
- **The full suite takes about 10 minutes; the unit gate about 130 seconds.**

**Gates:** `genesis gate . M33-mcp-stdio-client --timeout 1500000` in the background.

**Please complete every attack item.** This round is the fourth; a reviewer who stops at the first
defect leaves the next round to find the rest. Verify the gate evidence yourself rather than taking
it as given.

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id.

## Task under review

**M33-mcp-stdio-client**, **round 4**, risk **high**. HEAD is `ca8783d`; M33 is uncommitted.

### Why rounds 1 to 3 failed, and what changed in the approach

Each round fixed the path the reviewer named, and left a sibling path through the same state. The
connection's lifecycle was spread across five flags (`client`, `dead`, `starts`, `_failed_at`, and
a set of closed loops), and different paths checked them differently.

Round 4 redesigned the lifecycle rather than patching a site. Before this review, three fresh
agents attacked the new code with runtime probes, as an L4 review would, and each found defects:
- **First:** four loop-affinity defects (an idle loop, a tool used on another loop, a second
  `aclose`, a creation race). The redesign below removes the class.
- **Second:** a cancelled `aclose` cancelling the shared close, and a finalizer deadlock.
- **Third:** a server regex freezing the whole process past every bound (the GIL), the `__*`
  wildcard granting registered tools, and restart events dropped.

All are fixed, each with a test. Their probes are in the author's scratchpad (`m33/prereview`,
`prereview2`, `prereview3`); you may rerun them.

### Round 4's design (`agentsdk/mcp/__init__.py`)

- **One private event loop per Runner (`_Engine`, thread `agentsdk-mcp`, started lazily):**
  - every connection lives there, keyed by alias, tenant and project;
  - runs on any loop submit to it, through `run_coroutine_threadsafe` and `wrap_future`;
  - a tool's `fn` submits its `_call`.

  So a server never depends on a caller's loop.
- **Closing is one act, under one lock:**
  - `_Engine.close()` takes an `RLock`, which `submit` also takes, and sets `closed`. The lock is
    reentrant so that a GC finalizer running inside `submit` cannot deadlock.
  - `close()` returns a future that only `_close_all` resolves. `aclose` awaits it through
    `asyncio.shield`, so cancelling one `aclose` cancels only its own wait.
  - `_close_all` closes every connection, then cancels and drains every task left on the loop
    (several passes, bounded), resolves the future, and stops the loop.
  - A caller whose engine task the close cancelled gets `_Closed`, a `ToolExecutionError`, never a
    cancellation of its own run.
  - `aclose` on the engine loop itself raises.
  - `weakref.finalize` closes the servers of a Runner collected without `aclose`.
- **Per connection:**
  - `_ensure` is the only place a server starts, under the connection's lock, refusing when
    `closed` or `dead`;
  - `close()` sets `closed` and takes the same lock;
  - a call captures its session (client, task), and `_discard` ends only that session;
  - the serve task unsets the client only while it is the current session;
  - events of a start that a call made are kept, and reported by the next `ensure`.
- **Calls:**
  - each runs under `asyncio.wait_for(call_timeout + 5)`, the margin widened from 1 s;
  - reaching that SDK deadline means the request was never taken, whichever exception the library
    raised (it reports our cancellation as its own request timeout), so the session is discarded;
  - an honest library timeout at `call_timeout` keeps the connection.
- **Server regexes:**
  - a schema carrying `pattern` or `patternProperties` is validated by `validate_untrusted` in a
    process of the real interpreter, `-I`, ended at 10 s;
  - this covers input in the executor, and output through a replacement of the session's
    `validate_tool_result`, which is private API pinned by `mcp==2.3.0`;
  - any other schema is validated in process; an unreadable schema is a skip or an error, never a
    crash.
- **The wildcard:** `<alias>__*` lives in the MCP view. `McpClients.checker` wraps a default
  allowlist (`_McpAllowlist`) for configured aliases only. `agentsdk/permissions.py` is back to its
  committed state, so runs without MCP are unchanged (NFR-38).
- **Smaller changes:**
  - a server tool never replaces a base tool of the same name;
  - `StdioServer.env` has `repr=False`;
  - `agentsdk/api.py` uses the MCP-aware checker when the Runner has servers.

### Edits to approved tests, in addition to round 1's

`tests/conftest.py`, as in round 3: the asyncio stall dump.

### What the author ran

1. **Tests first:**
   - every new test failed for its stated reason before its fix;
   - the regex tests froze the process before the fix, as the defect predicts.
2. **Mutation run, round 4: 16 of 16 killed by the tests:**
   - two first "killed" only because their selector matched no test; the harness now reports that
     as an error;
   - four survived because the engine's refusal hid the connection-level guards, and the drain was
     unexercised; two tests now reach those guards directly;
   - the two regex mutants were first killed only by a hang, and the test now runs that scenario in
     a child process, so a freeze fails it within 120 s.

   Files were restored by SHA-256. Rounds 1 to 3 killed 22, 6 and 4.
3. **Gates,** both green on source hash `56539250`:
   - `unit`: 71 passed in 130.85s.
   - `regression`: 2243 passed in 595.94s (started 10:45:04 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m33-r4-probes` in your own scratchpad; tenant ids starting `SYN-m33r4`.

1. **The invariant: the SDK owns every wait that can block on a server or on the engine.** Work from
   the code and the library together. Include a caller awaiting a submitted future across a close,
   and the engine's own close.
2. **NFR-39 for every holder and timing:**
   - retained tools, views and connections;
   - other loops and threads, and idle loops;
   - garbage collection without `aclose`, and interpreter exit;
   - `aclose` cancelled, concurrent or repeated;
   - runs started after `aclose`.
3. **The engine itself:**
   - deadlocks, a caller left waiting on a stopped loop, threads or loops left behind;
   - its effect on runs that use no MCP (none should exist);
   - cancellation from a run through to the server.
4. **Server regexes:**
   - can any server-supplied regex still run in this process: input, output, `$ref`ed definitions,
     `propertyNames`, a library path we do not intercept?
   - is the subprocess bounded and always ended?
5. **Round 1's items:** trust (NFR-35), who sees what (P4-D30, the wildcard, shadowing), tenancy
   (P4-D28), secrets (NFR-37), hostile listings.
6. **The clarifications** in DECISION-7c9da7ae and round 1's prompt, and the approved-test edits.
7. **Regressions:** the full suite, and example 27 live if you can afford it.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M32,** round 1's clarifications, and the README's M33 rows.
- **The bounds working:** a server slower than its `connect_timeout`, a request not taken within
  `call_timeout + 5`, or a server regex slower than 10 s is treated as unavailable, wedged or
  invalid.
- **The cost of isolation:** a call to a tool whose schema carries a regular expression starts a
  validation process.
- **The library's fixed two-second termination grace** after stdin closes.

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
