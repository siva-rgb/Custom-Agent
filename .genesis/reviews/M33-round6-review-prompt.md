You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M33 was implemented by Opus 5.5. Rounds 1 to 5 were rejected: Opus 5, Fable 5.1, Sonnet 5,
Opus 5 and Fable 5.1. Bring no memory of an earlier review into this one: if your session holds
any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md: "#### M33" (FR-122 to FR-126), AC-89, AC-90, NFR-34,
NFR-35, NFR-37, NFR-38, NFR-39, and P4-D16 to P4-D31. **Read all five rejection reasons**
(`control` entries for M33 in `.genesis/project.json`, rejects at 2026-10-18T09:41:56Z,
2026-10-19T06:52:05Z, 08:06:26Z, 12:20:07Z and 15:44:25Z), and the knowledge records:
- KNOWLEDGE-34749df5, round 1;
- KNOWLEDGE-7d61cfab, round 2;
- KNOWLEDGE-27138d74, round 3;
- KNOWLEDGE-30f62c4a, round 4;
- KNOWLEDGE-73342492, round 5: the alias wildcard allowed a registered tool under a configured alias.

Read the owner's decisions:
- DECISION-7889f649: the lifecycle redesign;
- DECISION-7c9da7ae: server regexes in a subprocess, and four clarifications of the signed spec;
- DECISION-969d5d78: round 5, no server schema evaluated in process, with checkable bounds;
- DECISION-e8746c66: round 6, every guard an asserted property, and every earlier finding answered.

`.genesis/reviews/M33-review-prompt.md` describes M33 in full, `M33-round4-review-prompt.md` the
lifecycle design, and `M33-round5-review-prompt.md` the isolation of server schemas. This round
keeps all three. Only round 6's changes are below.

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
- **Host:** Windows, Python 3.11, `mcp==2.3.0` in the venv, 16 logical processors.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256, and never touch an applied migration (`0014` to `0016`).
- **MCP tests start real server processes, some of which never answer on purpose.** After every
  run, check for a leftover `mcp_servers.py`, `time.sleep(600)` or `addsitedir` process. Remove
  only those your own run started.
- **Expensive probes go in a child process,** started with `sys._base_executable`, so a timeout
  ends the real process and not a launcher.
- **Timings:** the full suite takes about 12 minutes, and the unit gate about 200 seconds.
- **Live tests:** `test_golden_eval.py` (Bedrock Haiku) failed once in round 5 with a provider
  outage, "Bedrock is unable to process your request", and passed on the rerun.

**Gates:** `genesis gate . M33-mcp-stdio-client --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id.

## Task under review

**M33-mcp-stdio-client**, **round 6**, risk **high**. HEAD is `ca8783d`; M33 is uncommitted.

### Why round 5 failed, and why it reached a reviewer

`_McpAllowlist.check` allowed a name when the text before its first `__` was a configured alias
whose wildcard the profile held. It never asked whether the name was one of that server's tools,
so a registered first-party `host__wipe` ran for a profile of `host__*` once a server aliased
`host` was configured. Pre-review 3 fixed the unconfigured alias only.

**Round 4's rejection had already named this, as its third finding, and round 5's brief dropped
it without a word.** Nothing checked that a round answered every item of the rejection before it.
That is the process failure; the defect is the sixth instance of one family: a property enforced
on an enumerated path, missed on its sibling.

### Round 6: guards that assert the property

**1. The wildcard asks what the tool is, not what its name looks like.**
- `RunTools.served_by_wildcard(name)` is true only when the tool the run executes under `name`,
  this turn, **is the very object** a connection built from its server's listing (`is`, not a
  name lookup), and the profile holds that server's `<alias>__*`.
- The checker reads the run's own view, the registry the executor resolves from, so a registered
  tool, which always wins a name clash in that view, can never be one, whatever its name.
- `api.py` passes the run's view to `McpClients.checker`, not the profile.

**2. FR-124's event is asserted of the outcome.** Whenever `ensure` leaves a server with no
client, and the Runner is not closed, the run is told `McpServerUnavailable`, whatever kept it
down: a failure just now, the retry interval, or the restart-once rule (`ServerExited`). The run's
view reports it once a run, not once a turn.

**3. One entry the SDK reads never fails its server.**
- An entry whose description or schemas cannot be stored (a NUL, a lone surrogate) is skipped,
  with a reason.
- So is an empty name, which the qualified-name pattern let through as `<alias>__`.
- So is an output schema the validator refuses. It was offered before, and every call of it
  failed.
- Anything else that refuses to become a tool skips that tool alone.
- **Entries the `mcp` library refuses still refuse the whole listing,** as declared in the README:
  a non-object type, a non-text `$schema`, about 60 levels of nesting, a lone surrogate in a name
  or key.

**4. The listing is checked 50 tools to a process, up to twenty side by side, within the one deadline.**
- A chunk with no full verdict refuses its own schemas, never another's, and never passes one.
  A verdict that is not text or null for every schema is no verdict.
- An honest 600-tool × 120-property listing (72k properties) now passes; one process would
  need about 24 s. In parallel each 50-tool chunk takes 4.5 to 6.2 s here, against 8 to 8.5 s
  for 100-tool chunks, which the author tried first and found too close to the deadline under
  load (a targeted run measured one such failure).

**5. The bounds test is inverted.**
- Every `await`, `async with`, `async for` and `with` must name its bound.
- So must every call, unless it is the module's own code, itself checked in turn, or is named in
  a table of calls that wait on nothing.
- An unknown call form fails the test, and so do `asyncio.wait` or `wait_for` with no timeout, a
  subprocess call with none, and a stale entry in either table.
- `test_the_bounds_test_fails_on_a_wait_form_it_does_not_know` adds each form round 5's review
  named to a copy of the module and requires the test to fail on it: `async for`, `.get()`,
  `os.read`, `run_until_complete`, `subprocess.check_output`, and `wait_for(..., None)`.

**6. Found by round 6's own pre-review, and fixed.**
- An error from the model's arguments failing a server schema quotes that schema. It was
  labelled as the SDK's own trusted text. Every failure of an MCP tool now carries the tool's
  declared untrusted provenance, before it ran too (`executor.py`, `_failed`).
- An image or blob whose data is not base64 failed the whole result. It is named as such now.
- A start the MCP loop did not answer in time read as a closed Runner, so the run lost the
  server's tools with no event. It is reported as `McpLoopUnanswered` now.
- A skip reason quoting a server schema is cut at 500 characters, as the name is at 200.
- `test_every_loop_in_the_mcp_module_names_what_it_iterates`: every `for`, comprehension and
  iterable-consuming builtin must name what it iterates, and every `while`, nested loop,
  recursion and import inside a function its bound. The pre-review had added `for line in f`,
  `list(f)`, `while True`, a recursion, a quadratic comprehension and a local import to a copy of
  the module, and the call-only test passed them all. The test of the test now carries all of
  them.

**7. Smaller items.**
- Structured content is appended to an image's placeholder, not put in its place.
- The output check raises a private `_InvalidOutput`, which `_call` catches by type.
- `aclose()` returns once the engine thread has ended.
- `tests/conftest.py` guards its private pytest import.
- The two stale claims that the library validates output are corrected.

### Every earlier finding, answered

This table is the contract. **If any finding of rounds 1 to 5 is missing from it, or its status
is false, that alone is a finding.**

| Round | Finding | Status |
|---|---|---|
| 1 | Connect and listing unbounded: banner, silent, endless pages | Fixed in round 2; pinned by `test_a_server_that_never_answers_is_unavailable_within_its_bound` |
| 1 | The alias wildcard in `permissions.py` granting by prefix, and a tool named exactly the alias | Fixed in pre-review 3 (permissions.py restored), and in round 6 for configured aliases |
| 1 | Structured content dropped an image placeholder | Fixed in round 6 (`test_an_entry_the_sdk_cannot_hold_skips_that_tool_alone`) |
| 1 | The library's twelve inherited environment names | Declared (README) |
| 1 | `UnknownMcpServer` absent | Deferred: AC-90's clause on MCP methods needs M34 to M36 |
| 2 | A call after a wedged write unbounded | Fixed in round 3 (the SDK's call deadline); `test_every_call_after_a_wedged_write_returns_within_its_bound` |
| 2 | aclose with a run in flight reopened a connection | Fixed in rounds 3 and 4 (the lifecycle redesign) |
| 2 | The retry interval is per tenancy | Declared (README, round 6) |
| 2 | A server's own env echoed by the library's parse-error log | Declared (README, round 6) |
| 2 | An empty tool name offered, then denied | Fixed in round 6 |
| 2 | faulthandler dumped threads only | Fixed in round 3 (the asyncio task dump) |
| 3 | A held tool restarted a server after aclose | Fixed in round 4 (one engine, `closed` final under the start lock) |
| 3 | An old serve task unset a newer session's client | Fixed in round 4 (sessions by identity) |
| 3 | A run-loop stall across call_timeout discarded a healthy connection | Addressed in round 4: the deadline now lives on the Runner's own MCP loop, not the run's |
| 3 | A crashed and restarted session's first task outlived aclose | Fixed in round 4; round 5's review found NFR-39 holding |
| 3 | conftest's unguarded private pytest import | Fixed in round 6 |
| 4 | A server schema with no regex validated in process | Fixed in round 5 (every check isolated) |
| 4 | Concurrent `_discard` calls left a server after aclose | Fixed in round 5; ten calls in flight re-measured in round 6 (`test_every_call_in_flight_when_its_server_exits_is_told_it_exited`) |
| 4 | **The wildcard granted a registered tool under a configured alias** | **Dropped from round 5's brief; fixed in round 6** |
| 4 | Ten calls in flight: none told the server exited | Fixed (round 5's redesign), re-measured and pinned in round 6 |
| 4 | aclose returned before its thread exited | Fixed in round 6 (`test_aclose_returns_once_the_mcp_thread_has_ended`) |
| 4 | No unavailable event inside the retry window | Fixed in round 6 (`test_a_server_that_stays_down_is_reported_to_every_run_once`) |
| 4 | The output mismatch matched on a message string | Fixed in round 6 (`_InvalidOutput`) |
| 5 | **The wildcard, as round 4's third finding** | **Fixed in round 6** |
| 5 | A NUL in one schema made the server unavailable | Fixed in round 6 |
| 5 | Honest listings refused past about 45k properties | Fixed in round 6 (`test_a_large_honest_listing_is_checked_whole_within_the_deadline`) |
| 5 | The checker child's stdin is written before its clock | Not reachable from a server; the constraint is now a comment at the call |
| 5 | The static test misses `async for`, `.get`, `os.read` and more | Fixed in round 6 (inverted, with a test of the test) |
| 5 | The runtime test misses a flat schema | Disputed: a flat schema cannot stall, and every schema still goes to the child |
| R6 pre-review | MCP argument errors quoting a server schema labelled trusted | Fixed (`test_round_6_pre_review_siblings_hold`) |
| R6 pre-review | An invalid output schema offered, then failing every call | Fixed (same test) |
| R6 pre-review | Data that is not base64 failing a whole result | Fixed (same test) |
| R6 pre-review | A stalled MCP loop dropping a server's tools in silence | Fixed (`test_a_stalled_mcp_loop_is_reported_not_silent`) |
| R6 pre-review | The static test blind to loops, recursion and imports | Fixed (the loop test, and the test of the test) |
| R6 pre-review | Library-level refusals of one entry still fail the whole listing | Declared (README) |
| R6 pre-review | A hostile tool takes its 49 chunk neighbours; fewer processors slow a large listing | Declared (README) |
| R6 pre-review | Skip reasons quoting schema text uncapped | Cut at 500 characters; not pinned by a test |
| 5 | `secret.json` at the repository root | The owner's, outside M33; it is in the content hash and must not be committed |

### How the author checked it before this review

- **Every open item was measured first-hand on round 5's tree before it was fixed.**
  - The wildcard: `host__wipe` allowed with a `host` server, denied without one, and denied with
    only `other`.
  - A NUL in one schema: the whole server unavailable, with `ToolError`.
  - Ten calls in flight when the server exits: all ten answered "exited" within 0.3 s, so round 5
    had already fixed it. It is now pinned by a test.
- **A fresh pre-review subagent** of the author's session (runtime probes only) found no way to run an unnamed
  tool through `<alias>__*`. It tried a registered tool, one registered after construction, a
  late shadow, a `ContextPolicy` run, two tenancies, and `legacy__a__b`. It found the ledger
  complete and true. Its findings are under item 6 above, and its probes are in the author's
  scratchpad, `m33/prereview6`.
- **Mutation, round 6: 22 of 22 killed by named assertions:**
  - the round 5 prefix check put back, and an identity check turned into a name lookup;
  - no event inside the retry interval, and one event a turn;
  - each per-tool skip removed;
  - the image placeholder replaced;
  - the output mismatch caught by the wrong type;
  - aclose not joining the thread;
  - one process for the listing, chunks run one after another, a chunkless verdict raising, and
    a non-text verdict accepted;
  - the trust label, the output-schema check, the base64 guard and the stall event;
  - four unbounded forms added to the module.
- **Gates,** both green on source hash `6ab24b2c`:
  - `unit`: 94 passed in 263.21s.
  - `regression`: 2266 passed in 989.80s (started 03:54:48 UTC on 2026-10-20).

  An earlier run was killed at 55% of `regression`. The laptop had entered Modern Standby on an
  idle timeout while on battery, two minutes into the run (Kernel-Power 506 at 23:51 IST), and
  stayed suspended until the gate's process was ended. It was not a hang in the code. The green
  run was made with a wrapper holding `SetThreadExecutionState`. Timings on this host vary by
  power state: the suite took 12 minutes in round 5 and 16.5 minutes here.

### Edits to approved tests

As in rounds 3 to 5: `tests/conftest.py`. Round 6 adds a guard around its private import; the
file is still purely additive against HEAD (+42 / -0). Round 1's list is otherwise unchanged:
`test_approval_contention.py`, `test_distribution.py`, `test_adr12_comparison.py`,
`test_builtin_tools.py`.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m33-r6-probes` in your own scratchpad; tenant ids starting `SYN-m33r6`.

1. **The ledger above.** Check that every finding of the five rejection reasons is in it, and that
   each status is true. A missing or false row is a finding.
2. **The wildcard, and permission generally.**
   - Can any tool the profile does not name outright run through `<alias>__*`: a registered tool,
     a tool registered after the Runner was built, a tool from an earlier turn's listing, or one
     from another tenancy's connection?
   - Is the view the checker reads always the one the executor resolves from?
   - Are siblings of this check still enumerating shapes anywhere else: approval, `allow` and
     `deny`, `_named`, or the refresh?
3. **Find a guard that still enumerates.** For every property this milestone claims (bounds,
   isolation, NFR-39, trust, the events), look for a check written as a list of cases rather than
   as the property, and for the sibling case it misses.
4. **The inverted bounds test.** Can a wait, or an unbounded cost a server controls, enter the
   module without failing it? Check whether the "waits on nothing" table is honest.
5. **The listing's chunks:** the deadline, cancellation part-way, a hostile chunk, ordering, and
   threads or processes left behind.
6. **Round 5's invariants, briefly re-checked:** isolation, the caller's deadline, and NFR-39.
7. **Regressions:** the full suite, and example 27 live if you can afford it.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M32,** round 1's clarifications, DECISION-7c9da7ae, and the
  README's M33 rows.
- **The bounds working:** a server slower than `connect_timeout`, a request not taken within
  `call_timeout + 5`, or a check slower than 10 s is treated as unavailable, wedged or invalid.
  A listing chunk that cannot be checked in 10 s has all of its tools skipped.
- **Sizes:** a server's data is bounded by deadlines and memory, not capped by size.
- **The cost of isolation:** each check of a server schema starts a process, about 0.25 s; a
  listing starts up to twenty at once.

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
