You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M34 was implemented by Opus 5.5; round 1 was rejected by Opus 5, and round 2 approved by
Fable 5.1 with caveats the owner chose to fix before closing. Bring no memory of an earlier review
into this one: if your session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md:
- "#### M34" (FR-127 to FR-129);
- AC-91, and AC-90's clause on the MCP methods with no server;
- NFR-35, NFR-36, NFR-38;
- the P4-D21, P4-D29 and P4-D30 rows.

Read the records:
- KNOWLEDGE-d5a17d72: M33 approved, and the items it carried to M34;
- KNOWLEDGE-9f774ba6: M34's pre-flight;
- KNOWLEDGE-3b4227e6: M34's pre-review, four defects found and fixed;
- KNOWLEDGE-a5db5edc: round 1 rejected: NFR-38's third named test edit was not made;
- DECISION-383e6df2: M34's design, one owner choice and one clarification of FR-127;
- DECISION-5d598bcd: M33 round 6's choices, which M34 keeps.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`; HEAD is `d4b29de` (M33). M34 is uncommitted.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost (KNOWLEDGE-cf82c576).

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string.** Strip the quotes if you export .env by hand.
- **Host:** Windows, Python 3.11, `mcp==2.3.0` in the venv, 16 logical processors. **This laptop
  enters Modern Standby on battery when idle,** which suspends a gate mid-run; run long commands
  under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256, and never touch an applied migration (`0014` to `0017`).
- **MCP tests start real server processes, some of which never answer on purpose.** After every
  run, check for a leftover `mcp_servers.py`, `time.sleep(600)` or `addsitedir` process. Remove
  only those your own run started.
- **Expensive probes go in a child process,** started with `sys._base_executable`.
- **Timings:** the full suite takes about 12 to 17 minutes; `tests/test_mcp_catalog.py` about
  40 seconds.

**Gates:** `genesis gate . M34-mcp-catalog --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id. Catalog rows carry a tenancy, not a
run: remove only those of tenancies your probes created (tenant ids starting `SYN-m34r3`).

## Task under review

**M34-mcp-catalog**, round 3, risk **high**.

### Why there is a round 3

**Round 2 approved,** and the owner verified it. Of its caveats, the owner chose to fix two
before closing, so the approval no longer covers the code:

- **A step-1 refusal carried the executor's labels.** `ToolBlocked` at executor step 1 was
  returned before `state.tool` was set, so it was labelled `internal_tool` / `trusted_source`,
  while the same refusal at the sink carried the tool's `mcp_resource` / `untrusted`. Now
  `state.tool` is set before the check (`agentsdk/executor.py`, step 1).
- **A server chose how long its own listing was trusted.** A listing was kept for a passed
  `ttlMs`, or 60 s with none, so a server declaring `ttlMs` 600000 kept a quietly changed tool
  running 61 s later. SPEC contradicted itself: FR-127's read list said "older than 60 seconds or
  `ttlMs` was absent", the risk register "once they are older than 60 seconds". **Owner's choice:
  capped.** A listing is read again once older than 60 s, whatever its `ttlMs`; a shorter `ttlMs`
  still reads sooner (`_Connection._is_stale`). SPEC FR-127 now reads "older than 60 seconds,
  whatever its `ttlMs`", matching the risk register and the README, which already said so.

**SPEC.md's approval is stale for that one line.** The owner chose to re-approve the spec and
plan after M34 completes, so `specification: SPEC.md / stale` in KICKOFF.md is expected and is
not a finding. Whether the new wording says what the code does is.

### Why round 1 failed, and what round 2 changed

**NFR-38 names three permitted edits to approved tests.** The third, `tests/conftest.py`'s tracked
tables gaining the new tables (NFR-16), was not made, and round 1's brief never mentioned
`conftest.py`. So AC-44 (`the_store_is_left_as_it_was_found`) could not see `mcp_tool_versions` or
`mcp_tool_acceptances`, the one row class no run-id cleanup reaches.

**Round 2:**
- **`_TRACKED` gains both tables:** a version by its whole key, an acceptance by
  `acceptance_id`. The suite now fails on a leaked catalog row.
- **A failed read again** (round 1's settled lead) ended the session but kept its tools offered.
  So step 1 passed them, and a call was validated against the old schema and could be asked
  about, before the sink refused it. Now:
  - an ended session's tools are offered to no turn;
  - executor step 1 goes through `_ensure`, which starts a dead session again (the restart-once
    rule) or reads a stale listing first;
  - a server still down is refused at step 1, `ToolBlocked: <name>: its server is not running`,
    before validation and approval;
  - the README row is corrected.

  Two tests pin it, and both mutants are killed.
- **Fields outside the hash** (title, `_meta`, icons, `execution.taskSupport`): read by no SDK
  code, recorded for M35 (KNOWLEDGE-a5db5edc).

### What M34 builds

**1. The catalog (FR-127, migration `0017`).**
- **The tables:**
  - `mcp_tool_versions`: one row per tool version a tenancy's connection has seen, keyed by
    tenant, project, alias and `tool_hash`;
  - `mcp_tool_acceptances`: one row per accepted hash, referencing its version by that composite
    key.

  Both are insert-only (`agentsdk/catalog.py`, and `PostgresCatalogStore` in
  `agentsdk/postgres.py`).
- **The hash** is the SHA-256 of the canonical JSON of the tool's name, description, schemas and
  annotations. The name is a clarification of FR-127 (DECISION-383e6df2): without it, two tools
  with equal content would share a hash.
- **Accepting:** `Runner.accept_tools` refuses in FR-127's order. A server's `accept` holds one
  `McpAcceptance` per tenancy, for a service principal.

**2. The gate (NFR-36), asserted in three places:**
- **The view:** a run is offered only tools whose current hash is accepted. `McpToolBlocked`
  names the hash, the previous accepted one, and the parts that changed. It is reported once per
  version per run, and at every read.
- **The property:** a call runs only the connection's current tool object for its name, while
  its hash is accepted, under a listing read first if stale. It is checked by identity in two
  places:
  - **executor step 1** (`RunTools.blocked`);
  - **the sink** (`_Connection._call`), again where the request leaves for the server, whatever
    the call waited on since step 1: an approval, or a slot.
- **Tool objects** are kept while their spec is unchanged, so identity means "the same tool, the
  same policy". A refusal says either `not accepted in its current version` or `changed since it
  was offered`.

  The store fails closed: a catalog read that raises, or takes over 30 s, leaves every
  not-yet-usable tool blocked. Each statement runs under a 20 s `statement_timeout`.

**3. When the catalog is read:**
- **The SDK owns freshness:** the `mcp` Client is built with `cache=None`.
- **Notifications:** a list-changed notification reads the listing at once in the background. On
  the legacy era it arrives through the message handler; on the 2026-07-28 era through
  `subscriptions/listen`, which is reopened at the next read if it closed.
- **Turn start and step 1:** both read the listing again when it is stale. That means a
  notification since the last read, a passed `ttlMs`, or 60 s, whichever comes first (round 3).
  The catalog's clock is replaceable.
- **Failure:** a re-read that fails ends that session.
- **Schema checks:** verdicts are cached per connection by canonical JSON, so an unchanged
  listing read again starts no checking process.

**4. Approval (FR-128):**
- `by_annotations` is for a trusted server only.
- A trusted server's annotations set risk, `read_only`, `idempotent` and `concurrency_safe`; an
  untrusted server's change nothing.
- An MCP approval request carries `alias`, `tool_hash` and the accepted description.
- A standing decision records its request's `tool_hash` and applies only to a request with the
  same one (`loop.py`, keyed by agent, tool and hash).
- **Owner's choice:** while `approval_overrides` names a tool the server does not list, every
  tool of that server asks, and `McpServerConnected` names the unmatched keys.

**5. Example 28** (`scripts/28_mcp_catalog.py`, FR-129) shows the five steps of FR-129, offline on
both stores and live. Example 27 now accepts its tools from a first run.

### Every carried finding, answered

This table is the contract. **If any item that M33's approval or M34's pre-flight carried is
missing from it, or its status is false, that alone is a finding.**

| From | Finding | Status |
|---|---|---|
| M33 approval | A qualified name in `deny` was silently ignored | Fixed: `allow`, `deny` and `approval_overrides` refuse `<alias>__...` (`test_an_invalid_server_config_is_refused_when_built`) |
| M33 approval | The bounds test exempted calls by callee name, so `open(server_path)` would pass | Fixed: `open`, `re.compile`, `json.loads`, `base64.b64decode` and `pathlib.Path` are exempt only as exactly written; the test of the test carries the variable forms |
| M33 approval | `approval_overrides` keys not checked against the listing | Fixed: an unmatched key makes every tool ask (owner's choice); `test_an_override_naming_no_listed_tool_makes_every_tool_ask` |
| M33 approval | Example 27 not run live in round 6 | Run live 6 times at pre-flight, and again after M34 |
| M33 approval | The task read rejected beside three passing gates (no `control resume` after round 5) | Process: resumed before `task complete`; noted in KNOWLEDGE-d5a17d72 |
| M34 pre-flight | Example 27 started its server with the venv launcher, against the README's advice | Fixed: the real interpreter, the packages path passed as an argument |
| M34 pre-flight | Two of six live runs of example 27 failed before any tool ran, undiagnosed | Open: their rows were removed before their events were read. Every later live run passed |
| M33 (AC-90) | `UnknownMcpServer` absent until the MCP methods exist | Fixed: `accept_tools` raises it, also with `mcp` uninstalled (`test_without_the_extra...`) |
| M34 round 1 | **NFR-38's third edit: `tests/conftest.py`'s `_TRACKED` lacked the catalog tables** | **Fixed in round 2:** both tables tracked |
| M34 round 1 | A failed read again kept its tools offered; validation and an approval used the old listing | Fixed in round 2: offered to no turn; step 1 starts the session again or refuses (`test_after_a_failed_read_again_...`, `test_a_turn_whose_read_again_failed_...`) |
| M34 round 1 | The README row claiming a failed read's tools were absent overstated it | Fixed in round 2: the row matches what is now done |
| M34 round 1 | `tests/fixtures/mcp_servers.py` edits were not declared | Declared in round 2, below |
| M34 round 1 | title, `_meta`, icons and `execution.taskSupport` are outside the hash | Deferred to M35, which brings the fields that read them; no SDK code reads them today |
| M34 round 1 | Examples 27 and 28 not run live in round 1 | Run live in round 2 by the author: 27 passed 3 of 3, 28 passed 6 of 6 |
| M34 round 2 | **A step-1 `ToolBlocked` carried executor provenance, the sink's the tool's** | **Fixed in round 3:** `state.tool` set before the check; `test_after_a_failed_read_again_...` asserts `mcp_resource` / `untrusted` |
| M34 round 2 | **A passed `ttlMs` over 60 s kept a changed tool running past 60 s; SPEC lines 770 and 1255 disagreed** | **Fixed in round 3 (owner capped it):** `test_a_ttl_longer_than_60_seconds_does_not_keep_a_listing_past_60_seconds`; SPEC 770 reworded |
| M34 round 2 | Unknown annotation keys are dropped by the library's `ToolAnnotations` before hashing | Deferred to M35 with the other fields outside the hash |
| M34 round 2 | Round 2's brief said `_call` takes `digest`, and a malformed hash gives `UnknownToolVersion` | Brief wording: `_call` takes `tool`; a malformed hash gives `ValueError`. Corrected below |
| M34 round 2 | The task read `rejected` beside three passing gates (no `control resume` after round 1) | Process: resumed before round 3's gates |

### Found while building M34, and fixed

- **The finalizer held the Runner.** Handing the engine the Runner's own `_catalog_store` meant
  the finalizer's callback reached back to the Runner, which kept a collected Runner alive and
  its servers with it (NFR-39). M33's finalizer test caught it. The engine now holds only the
  store, or `Persistence`.
- **Re-reads re-checked unchanged schemas.** A modern server sends `ttlMs` 0, so each turn and
  call re-read its listing and re-checked every schema in a new process. The fix is the verdict
  cache.

### How the author checked it

- **Tests first:** `tests/test_mcp_catalog.py`, written before the code. Now 63 tests, on both
  stores and both eras.
- **Mutation, first run:** 22 of 22 killed by named assertions. Two survived at first, both
  test gaps, and the strengthened tests killed them:
  - an acceptance kept by name across a change;
  - step 1 not reading a stale listing.
- **A fresh pre-review agent** ran runtime probes only (KNOWLEDGE-3b4227e6). It found four
  reachable defects, each reproduced first-hand by the author and now fixed and pinned:
  - **D1:** a call that waited for approval reached the server on the last read, so the 60 s or
    `ttlMs` window became the length of the wait;
  - **D2:** a notification arriving during a read was lost;
  - **D3:** a read that changed every tool's approval policy, under the owner's rule, left the
    turn's tool running under its old policy;
  - **D4:** a server changing its tools without end grew the catalog and memory without limit.

  A failing store was also reported every turn. Its caveats about a hung in-memory store and the
  cost of `ttlMs` 0 are declared.

  The pre-review's probe scripts are in the author's scratchpad, `m34/prereview`, and you may
  rerun them.
- **Mutation, second run:** 7 of 7 killed. Step 1 and the sink are each tested on their own,
  since two guards for one property hide each other.
- **Round 2's fixes:** 2 of 2 mutants killed, 31 of 31 across M34.
- **Round 3's fixes:** 2 of 2 mutants killed, 33 of 33 across M34, each by its own test alone:
  - `state.tool` cleared before the step-1 refusal: `assert (<Origin.INTERNAL_TOOL, ...trusted_source>) ==
    (<Origin.MCP_RESOURCE, ...untrusted>)`;
  - the cap removed: `AssertionError: within ttlMs, but past 60 s`.
- **Round 3's first gate run** failed two M33 static tests: the bounds scan read `min(...)` as an
  iteration naming nothing. The cap is now a conditional expression; the approved test is
  unedited.
- **AC-44 sees the catalog now:** a control session, with a copy of the conftest outside the
  repository, left one catalog row behind and failed:
  `the test session changed the store's runs: {'mcp_tool_versions': {'added': 1, 'removed': 0}}`.
- **Gates,** both green on source hash `a1a7fea9`:
  - `unit`: 64 passed in 52.35s;
  - `regression`: 2333 passed in 795.19s (started 06:07:36 UTC on 2026-10-21).
- **Live:** examples 27 and 28 were run live in round 2 (27 passed 3 of 3, 28 passed 6 of 6) and
  not again by the author for round 3. The store holds 10351 runs, 0 SYN, 0 running, and empty
  catalog tables.

### Edits to approved tests

**NFR-38 names the edits it permits:** those FR-122 and FR-123 name; `tests/test_distribution.py`'s
exports and `EXPECTED_EXAMPLES`; and `tests/conftest.py`'s tracked tables. **All three are made,
each listed below.**

- **`tests/conftest.py`:** `_TRACKED` gains `mcp_tool_versions` and `mcp_tool_acceptances`
  (round 2).

- **`tests/test_mcp_client.py`** (M33's):
  - an autouse fixture making every listed version read as accepted, through the one function
    every catalog read goes through, `_read_catalog`;
  - the same patch in its two child-process scripts;
  - the invariant tables for M34's waits and loops;
  - `_call`'s new `tool` argument in one test;
  - the thread test checking its own Runner's thread;
  - the no-extra test also asserting `UnknownMcpServer`;
  - the pre-flight's config refusals and argument-keyed exemptions.
- **`tests/test_distribution.py`:** example 28 added to `EXPECTED_EXAMPLES`.
- **`tests/test_mcp_catalog.py`** is M34's own and not yet approved; round 3 adds one test and
  one assertion, both named in the table above.
- **`tests/fixtures/mcp_servers.py`** (M33's fixture):
  - a catalog mode for both eras, whose tools come from a state file the tests rewrite, with
    every listing read logged and a `notify` tool;
  - for the 2026-07-28 era, a `ttlMs` taken from the state file.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m34-r3-probes` in your own scratchpad; tenant ids starting `SYN-m34r3`.

0. **The table above:** check every row is true, round 2's rows first. Does every refusal of an
   MCP tool, at step 1, at the sink, or anywhere else, carry the tool's labels? Can any listing be
   kept past 60 s, by any `ttlMs` value or page combination (`ttlMs` 0, absent, negative, huge, a
   different value per page), or by any path that skips `_is_stale`? Does SPEC FR-127 now say
   what the code does?
1. **NFR-36:** can any call reach a server under a hash not accepted for its tenancy?
   - by any path: in flight across a change, after a restart, through a tool object held from
     before, across tenancies, in concurrent runs;
   - after a change the server made without saying so;
   - after the listen stream was lost.
2. **Siblings of the gate:** is anything of a blocked tool used before acceptance? Its schema,
   description or name in a model request, the manifest's `tools_sent`, an approval request, a
   validation.
3. **Find a guard that still enumerates.** For each property above, look for a check written as a
   list of cases rather than as the property, and for the sibling case it misses.
4. **`accept_tools` and `McpAcceptance`:** the order, atomicity, tenancy, rows read back, never
   updated.
5. **Standing decisions bound to the hash,** in both directions, and non-MCP tools unchanged.
6. **The catalog's reads:** each era's notifications, `ttlMs`, the 60-second default, step 1's
   read, a flood of notifications, a slow store, a slow server; and `aclose()` and NFR-39 with
   the new background tasks.
7. **Regressions:** the full suite, and examples 27 and 28 live (round 1 ran no live model call).

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M33,** and the README's M33 and M34 rows.
- **The bounds working:** a catalog read slower than 30 s leaves its tools blocked; a re-read
  slower than `connect_timeout` ends the session.
- **Acceptances are never withdrawn** (README).
- **A server that changes a tool without notifying is caught at the next read:**
  - once its `ttlMs` passes;
  - once it is 60 s old, whatever its `ttlMs`;
  - before a call, when either has passed (FR-127, declared).
- **SPEC.md's approval is stale** for FR-127's one reworded line, by the owner's choice (above).

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
  control approve . M34-mcp-catalog --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M34-mcp-catalog --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.
