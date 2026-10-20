You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Sonnet 5** if Fable 5.1 fails
to start. M33 was implemented by Opus 5.5. Rounds 1 to 4 were rejected: Opus 5, Fable 5.1, Sonnet
5 and Opus 5. Bring no memory of an earlier review into this one: if your session holds any, say so
and ask for a fresh one.

Re-derive the requirements from SPEC.md: "#### M33" (FR-122 to FR-126), AC-89, AC-90, NFR-34,
NFR-35, NFR-37, NFR-38, NFR-39, and P4-D16 to P4-D31. Read the four rejections:
- KNOWLEDGE-34749df5, round 1;
- KNOWLEDGE-7d61cfab, round 2;
- KNOWLEDGE-27138d74, round 3;
- KNOWLEDGE-30f62c4a, round 4: a server schema with no regular expression still validated in
  process, unbounded.

Read the owner's decisions:
- DECISION-7889f649: the lifecycle redesign;
- DECISION-7c9da7ae: server regexes in a subprocess, and four clarifications of the signed spec;
- DECISION-969d5d78: round 5, no server schema evaluated in process, with checkable bounds.

`.genesis/reviews/M33-review-prompt.md` describes M33 in full, and
`.genesis/reviews/M33-round4-review-prompt.md` describes round 4's lifecycle design, which this
round keeps. Only round 5's changes are below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. `--check` once ran a
gate and overwrote its evidence.

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
  run, check for a leftover `mcp_servers.py`, `time.sleep(600)` or `addsitedir` process. Remove
  only those your own run started.
- **Expensive probes go in a child process.** A probe that runs exponential validation or a
  catastrophic regex in your own process freezes it (Python's `re` holds the GIL). Start the child
  with `sys._base_executable`, so a timeout ends the real process and not a launcher.
- **Timings:** the full suite takes about 12 minutes, and the unit gate about 195 seconds.
- **Live tests:** one live-gateway test (`test_golden_eval.py`, Bedrock Haiku) failed once during
  this round with a provider outage, "Bedrock is unable to process your request", and passed on
  the rerun.

**Gates:** `genesis gate . M33-mcp-stdio-client --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id.

## Task under review

**M33-mcp-stdio-client**, **round 5**, risk **high**. HEAD is `ca8783d`; M33 is uncommitted.

### Why round 4 failed

Round 4 isolated only server schemas carrying a regular expression, on the premise that the rest
could not stall. A 1.4 KB schema of `$ref`-chained `anyOf` with none is exponential to validate.
That is the fourth round running of one family: a property enforced on the path someone enumerated,
and missed on its sibling.

### Round 5: the invariant stated, and checked by tests

**1. No schema a server supplies is evaluated in this process.**
- **Every evaluation goes to the isolated checker,** `validate_untrusted` and `_check_listing`
  over `_isolated_or_raise`. It runs in a short-lived process of the real interpreter, `-I`, ended
  at `_VALIDATION_SECONDS` = 10. This covers:
  - the model's arguments, in the executor at step 2;
  - every result with a declared output schema, null `structuredContent` included, through a
    replacement of the session's `validate_tool_result`; nothing is handed to the library;
  - the listing's input schemas, one batch per listing, where a verdict must answer every schema.
- **References stay inside the schema:** the child validates with an empty `referencing.Registry()`,
  so a `$ref` to a URL, a file or a UNC share is unresolvable, never fetched.
- **It fails closed:** a child that exits non-zero, prints nothing, or prints anything but one JSON
  object is "could not be checked", never a pass.
- **A declared output that was not returned is refused,** as the library refuses it.

**2. Every wait has an SDK-owned bound, including the caller's.**
- `_Engine.submit(coroutine, timeout)` waits through `asyncio.wait` with a timeout, not `wait_for`.
  An honest `TimeoutError` from the call itself then reaches the caller as itself, and a missed
  deadline does not. Its bounds are `_ensure_bound` and `_call_bound`, sized from every inner bound.
  `aclose` waits within a bound as well.
- **Any exit from a start ends the session it began,** cancellation included. A cancelled first
  start once left a server connected with no tools, for good.
- **The MCP loop's default executor is shut down with it.**

**3. Two tests make it checkable** (`tests/test_mcp_client.py`):
- **`test_no_server_schema_is_ever_evaluated_in_this_process`:** a run of MCP tools of both eras
  (echo, structured output, a mismatch, a regex schema, a deep schema, a null result) with
  `jsonschema.validators.validator_for` and `jsonschema.validate` patched in this process. Any
  schema evaluated here other than JSON Schema's own metaschemas fails it.
- **`test_every_wait_in_the_mcp_module_names_its_bound`:** it walks `agentsdk/mcp`'s syntax tree.
  - Every `await`, `async with`, `with`, and blocking call (`time.sleep`, `.result`, `.join`,
    `.communicate`, `.acquire`, `.wait`) must appear in a table naming its bound.
  - Every `subprocess` call must carry a timeout that is not `None`.
  - An unlisted wait fails it, and so does a stale entry.

### How the author checked it before this review

- **Two fresh agents ran robustness passes** after round 4: runtime probes only, with no pytest,
  database or repository edits.
- **The first found three problems,** each now fixed and tested: the null-result path, remote
  `$ref` reach (an SMB attempt to an arbitrary host), and failing open on a dying child. It also
  found that the bounds table was circular: the caller had no deadline of its own.
- **The second confirmed those fixes, with no sibling bypass,** and found the cancelled-start
  half-open session, now fixed. Its caveats are in the README rows:
  - bounded but uncapped sizes;
  - a very long line read in quadratic time, by the library;
  - deep listings making a server unavailable.
- **Their probe scripts** are in the author's scratchpad (`m33/robust`, `m33/robust2`); you may
  rerun them.
- **Mutation, round 5: 9 of 9 killed by named assertions:**
  - arguments, null results and the listing checked in process;
  - the default reference registry;
  - fail-open;
  - no caller deadline;
  - a caller deadline swallowing the call's own timeout;
  - a cancellation skipping the start's cleanup;
  - a short listing verdict.

  Rounds 1 to 4 killed 22, 6, 4 and 16.
- **Gates,** both green on source hash `71672b3e`:
  - `unit`: 81 passed in 196.74s.
  - `regression`: 2253 passed in 718.84s (started 14:20:06 UTC).

  An earlier run failed only the Bedrock live test, on a provider outage. The two runs after it
  landed on alternating hashes because of a `lastfailed` cache entry, cleared by the third run.

### Edits to approved tests

As in round 4: `tests/conftest.py` (the asyncio stall dump), on top of round 1's list.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m33-r5-probes` in your own scratchpad; tenant ids starting `SYN-m33r5`.

1. **The general invariant, not just schemas.** Find any work in this process whose cost a server
   controls and grows faster than its data, or holds the GIL, or any wait without an SDK bound.
   Work from the code and the library together.
2. **Isolation:**
   - can a server schema still be evaluated here, by any path, in either era?
   - can the child reach anything outside the schema, or return a pass that did not happen?
3. **Do the two invariant tests check what they claim?** Where could each miss a regression?
4. **The caller's deadline and cancellation:**
   - does every failure reach the caller as itself;
   - is engine-side work cancelled with its caller;
   - is anything left behind when a deadline fires?
5. **NFR-39 and the lifecycle** (round 4's design), briefly re-checked on the new code.
6. **Round 1's items:** trust, who sees what, tenancy, secrets, hostile listings.
7. **Regressions:** the full suite, and example 27 live if you can afford it.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M32,** round 1's clarifications, DECISION-7c9da7ae, and the
  README's M33 rows.
- **The bounds working:** a server slower than `connect_timeout`, a request not taken within
  `call_timeout + 5`, or a check slower than 10 s is treated as unavailable, wedged or invalid.
- **Sizes:** a server's data is bounded by deadlines and memory, not capped by size.
- **The cost of isolation:** each check of a server schema starts a process, about 0.15 s.

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
