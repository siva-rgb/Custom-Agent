You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M37 was implemented by Opus 5.5 and its pre-review run by an Opus 5.5 subagent; round 1 was
reviewed by Fable 5.1, which rejected it. Bring no memory of an earlier review into this one: if
your session holds any, say so and ask for a fresh one.

**This is round 2.** Round 1 found the SDK sound and four defects in example 31's live checks
(KNOWLEDGE-1afcf6b2). Judge the repair, and judge everything again: a repair can break what held.

M37 is **Phase 4's exit signal**. Re-derive the requirements from SPEC.md:
- "#### M37" (FR-135), AC-94, including its "Pre-flight additions (owner, M37)";
- FR-127's new bullet "Listing what is blocked" (`Runner.mcp_blocked`);
- FR-133's new sub-bullet "its size is bounded" (the input request cap);
- NFR-22, NFR-34 to NFR-39 (NFR-38 as amended after M35: the edits an increment needs, each named
  here with its reason and checked by content).

Read the records:
- KNOWLEDGE-a3af13d4: M36 approved; its caveat 3 (no cap on an input request) carried here;
- DECISION-8e3e2ea1: the owner's pre-flight decisions, `mcp_blocked` and the 64 KiB cap, and the
  SPEC amendment re-approved;
- KNOWLEDGE-d2afcab0 and KNOWLEDGE-14f3ec8b: the witness and schedule lessons FR-135 cites;
- KNOWLEDGE-95493841: M37's pre-review, two cap defects fixed and six weak checks hardened;
- KNOWLEDGE-c59cba3a: the first live run, why five checks failed, and how they were reworked;
- KNOWLEDGE-7d24c4c5: the final mutation run, and the change check naming the catalog's refusal;
- KNOWLEDGE-1afcf6b2: round 1 rejected by Fable 5.1, D1 to D4, and the lesson it carries;
- DECISION-9e28294e: the owner's round-2 choices -- repair all four, name the failing clause,
  and fix example 30's live checks in this round;
- KNOWLEDGE-33f03394: round 2's build, each defect pinned by a test of its violation.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`; HEAD is `3f7753f` (M36). M37 is uncommitted. No migration is added. Round 1's brief is
`.genesis/reviews/M37-review-prompt.md`, kept as it was.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs:** the gate writes `.genesis/project.json` back when it
ends, and a record made meanwhile is lost.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string.**
- **Host:** Windows, Python 3.11, `mcp==2.3.0`, `httpx2==2.12.0`. **This laptop enters Modern
  Standby on battery when idle,** which suspends a gate mid-run; run long commands under a wrapper
  holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`.

**Running tests**
- **One test session at a time.** `tests/conftest.py` refuses a second one with exit code 3.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation,** leaving no repository file changed. Restore any edited file byte
  for byte, verified by SHA-256. **Check that no repository file changed by content, never by
  mtime.**
- **Example 31 starts five real server processes per run** (three stdio, two HTTP) and a temp
  folder `example-31-*`, which it removes. After every run, check for a leftover
  `31_mcp_contention.py`, `mcp_servers.py`, `mcp_http_servers.py` or `addsitedir` process, and a
  leftover temp folder. Remove only those your own run started.
- **Timings:** the full suite takes about 20 minutes; `tests/test_mcp_contention.py` about 3; one
  offline run of example 31 about 5 to 7 seconds; a live run several minutes.

**Gates:** `genesis gate . M37-mcp-contention --timeout 1500000` in the background.

**Please complete every attack item, and verify the gate evidence yourself.**

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then
`artifacts` by `source_run`, `plan_node_states`, `plan_versions`, `run_events`, `messages`,
`execution_manifests`, then the runs, children first, by run id. Catalog rows carry a tenancy, not
a run: remove only those of tenancies your probes created (tenant ids starting `SYN-m37r2`). Example
31 offline makes a project of its own each run (`contention-<hex>`).

## Task under review

**M37-mcp-contention**, round 2, risk **high**.

### What round 2 changes

Only example 31's checks, example 30's live checks and the tests that hold them; no SDK source
file changed since round 1 (`git diff` against round 1 shows `scripts/30_mcp_input.py`,
`scripts/31_mcp_contention.py`, `tests/test_mcp_contention.py` and one README row).

- **One rule for every subject (D2 to D4).** Live, a check is `None` -- `[SKIP] not exercised` --
  only when the servers' journals show its least subject never happened: NAMES, UNTRUSTED and
  SECRETS, that a server ran anything; APPROVED, that a call that asks ran; INPUT, that the server
  asked; CHANGED, that the server changed its tool. Never the property under test, never a record
  of the SDK's, never a conjunction a model breaks by skipping a step. What a scripted run must also
  show (both lookups and poison ran; the injection as a result, an error and a read; both secrets
  arrived; a turn with two calls that ask; the one intake call's exact shape) is asked only when
  `strict`, by default offline. `demonstrate` and `offline` take `strict`, so a test runs scripted
  children under the live checks.
- **D1, approvals by existence.** Each execution of a tool that asks, in the order the servers ran
  them, takes the unused approve decision for its name and arguments decided first, made before it
  ran. One exists for every execution exactly when none ran unapproved, twice on one approval, or
  with arguments changed; a surplus approval -- a call approved, then refused as it left -- changes
  nothing. Issue order is held only where a call can be told from every other: its name and
  arguments made once.
- **D3, NAMES both ways.** Each call a server ran is one a child made under that server's name with
  those arguments, and each result that came from a server (NFR-35's origin, not a `ToolBlocked`
  refusal before sending) came from an execution on the server its name says.
- **Clauses.** Each check is `(label, passed, clause)`; `report` prints the clause on every FAIL and
  SKIP, so a live failure needs no replay.
- **Example 30 (owner).** Live, a request the model never made is `None`, `[SKIP]`; offline it still
  fails. Its checks stay two-tuples, so M36's test of it is unedited.

### What M37 builds

**1. `Runner.mcp_blocked(tenant_id, project_id, alias)` (FR-127, owner).** Through that tenancy's
connection, an `McpBlocked` of kind `tool` for each tool the server lists now whose current version
is not accepted, with `McpToolBlocked`'s payload; a stale listing is read again first; a catalog
that cannot be read gives each tool `error`. It exists because under orchestration a changed tool's
`McpToolBlocked` lands on a child's run, which no application can read live, so a top-level approver
could not learn the new hash. Refused as `mcp_resources` is; never a tool. Connection method
`blocked()` in `agentsdk/mcp/__init__.py`, Runner method in `agentsdk/api.py`.

**2. The input request cap (FR-133, owner; M36's caveat 3).** A request whose message, with its
schema in form mode or its URL in url mode, is over 64 KiB as JSON (`_request_size`, `_MAX_REQUEST`)
is refused before anything is recorded: answered to the server as an error, counted toward the
three, and the call fails `ToolExecutionError("input request too large")`. `_AskingCall.too_many`
became `refused`, a reason; `_too_many()` became `_refused(reason)`. Pre-review D1 and D2: once
refused, every later request of the call is refused unrecorded, sampling and roots included, and
the first reason stands.

**3. Example 31, the exit signal (FR-135, AC-94),** `scripts/31_mcp_contention.py`. An orchestrated
plan of five children; five real servers in the same file:
- `catalog` (stdio, auto era): `lookup` changes its description when `revise` runs;
- `shadow` (stdio, legacy): a `lookup` of its own, and an env value it receives;
- `intake` (stdio): `register` asks for a form (2026-07-28 input_required, so the library retries);
- `poison` (HTTP, header): `summarize`, whose description tells the model to call `catalog__revise`;
- `inject` (HTTP legacy, header, `resources_as_tool`): an injected result, an injected error and an
  injected resource read.

Each server journals every execution as it starts, with its arguments, version and answers. A
scripted approver works from the top-level run only: it accepts first sight through `mcp_blocked`,
accepts the change once the reviser waits, approves every call in a random order after a random
delay, and answers the form. The eight checks, each **witnessed from the journals alone** (a
`None` result: subject never happened, `[SKIP]` live, failure offline):
- the plan completed (offline only; live it is reported);
- the changed tool never ran after its change until accepted, and the change was named (offline
  also: the call sent after the change was refused as it left, one ran under the new version, and
  on PostgreSQL the reviser's run holds the `McpToolBlocked` row);
- each call a server ran is one a child made under that server's qualified name, with those
  arguments;
- every MCP result, error and read is untrusted and data-only;
- every call needing approval ran at most once, with the approved arguments, after an approve
  decision, in issue order;
- the input request was answered, and its call ran again with the answer;
- no header or env value in any record;
- every server process the Runner started has exited after `aclose`.

Live, calls are matched to journal entries by name and arguments, counted, and approvals greedily
by tool, arguments and decision time, since a live model repeats calls and reuses tickets.

**4. `tests/test_mcp_contention.py`,** the unit gate: ten repetitions on each store with shuffled
starts and random delays; each of the five removals (change, shadowing tool, injection, input
request, approval) fails exactly the check that names it; the `McpToolBlocked` row on PostgreSQL;
`mcp_blocked` in both eras and stores; the cap at 64 KiB + 1 (both modes, eras, stores) and at
exactly 64 KiB; D1/D2; NFR-34's dependencies.

### Every carried finding, answered

This table is the contract. **If any item carried or found is missing from it, or its status is
false, that alone is a finding.**

| From | Finding | Status |
|---|---|---|
| M36 approval, 1 | `cancel()` then `aclose()` unawaited leaves an interruption undecided, 2 of 6 modern | Declared (SPEC line 532, NFR-30, Phase 6); carried to Phase 6's resume scope (KNOWLEDGE-a3af13d4) |
| M36 approval, 2 | A run can report cancelled about 0.2 s before its cancel decision is written | Declared, reporting order |
| M36 approval, 3 | No size bound on an input request's message, schema or URL | Fixed by the owner's cap (`test_an_input_request_over_64_kib_...`, `..._of_exactly_64_kib_is_recorded`) |
| M37 pre-flight | No public way for an approver to learn a version only a child met | `Runner.mcp_blocked` (DECISION-8e3e2ea1; four tests) |
| M37 build | A modern server with no cache hint lists `ttlMs` 0, stale at once | FR-127 as written; the tests give a long ttl |
| M37 build | A child has four turns (`_DEFAULT_MAX_TURNS`) | The reviser's steps fit four |
| M37 mutation | Three cap mutants were killed only by hanging | `approval_timeout=3` in the over-cap test; killed by assertion |
| M37 pre-review D1 | After a refusal a legacy call kept asking the person | Fixed (`test_once_a_request_is_refused_the_calls_later_requests_ask_nobody`) |
| M37 pre-review D2 | A later refusal overwrote the first reason | Fixed, same test with three later requests |
| M37 pre-review W1 | In memory, SECRETS reads no child event or manifest | Declared (README): no in-memory store keeps them; read on PostgreSQL |
| M37 pre-review W2 | Live, a witness read from SDK records turned an SDK failure into SKIP | Fixed: every witness from the journals; usable_all, sdk_no_approval, auto_answer now FAIL live |
| M37 pre-review W3 | UNTRUSTED covered no error and no read | Fixed: an injected error and an injected resource read |
| M37 pre-review W4 | The call after the change failed ToolNotFound; the leave-time check never ran | Fixed: the call is in the change's turn and refused as it leaves; the McpToolBlocked row checked on PostgreSQL |
| M37 pre-review W5 | Approvals not bound to arguments | Fixed: journals record arguments, compared to the approved request |
| M37 pre-review W6 | The HTTP half of EXITED was tautological | Narrowed to the Runner's processes, labelled so |
| M37 pre-review notes | Temp folders never removed; size measure escapes non-ASCII; `mcp_blocked` can spend the restart | Folders removed; the other two declared (README) |
| M37 live run 1 | Five checks failed: ticket matching, a row only offline can require, plan completion | Reworked to hold whatever the models decide (KNOWLEDGE-c59cba3a); no SDK violation found in the store |
| M37 final mutation | The approver accepting the change at once survived: R4 is refused by either guard | Offline, the check names the catalog's refusal; mutant and the always_usable control both fail it (KNOWLEDGE-7d24c4c5) |
| Round 1, D1 | A surplus approval cascaded through greedy matching: APPROVED failed live with no violation | Fixed: matching by existence, order on calls made once (`test_a_call_approved_then_refused_and_made_again_fails_no_check`) |
| Round 1, D2 | APPROVED's subject needed a two-call turn read from histories: SKIP over unapproved executions | Fixed: subject is a call that asks ran (`test_calls_run_without_approval_fail_approved_live_...`) |
| Round 1, D3 | NAMES witnessed by the routing it tests: SKIP when misrouted | Fixed: subject is anything ran, and NAMES checks both ways (`test_a_call_routed_to_another_server_fails_names_live`) |
| Round 1, D4 | Conjunctive subjects: UNTRUSTED and SECRETS skipped over violations when the model skipped a step | Fixed (`test_results_labelled_trusted_fail_untrusted_live_...`, `test_an_env_value_in_the_manifest_fails_secrets_live_...` on PostgreSQL) |
| Round 1, caveat | In memory, SECRETS passes over a manifest leak | Declared (W1); the leak test runs on PostgreSQL |
| Round 1, caveat | Example 30 failed live when the model skipped connect_calendar | Fixed in round 2, the owner's choice (`test_example_30_reports_a_request_the_model_never_made_...`) |
| Round 1, caveat | Checks printed no failing clause; a live failure needed a replay of the store | Fixed: every FAIL and SKIP names its clause (`test_a_check_whose_subject_never_happened_...`) |

### How the author checked it

Round 1's account stands in round 1's brief; round 2's:

- **Tests first for each defect:** each round-1 defect has a test running its violation, or the
  live shape that broke it, under the live checks (`strict=False`) with scripted children. The
  violations are runtime patches inside the test (an SDK that asks no approval, a call routed to
  another server, results labelled trusted, env values written into the manifest), never edits on
  disk.
- **Mutation:** 18 of 18 killed by named assertions, each file restored by SHA-256: round 1's eleven
  again, and seven more -- each round-1 defect put back (D1's order over repeated calls, D2's, D3's
  and D4's two subjects), the clause dropped from a FAIL, and example 30 failing a request never
  made. Each killed by its own test.
- **Gates,** on source hash `468af8e0`:
  - `unit`: 40 passed in 226.14s;
  - `regression`: 2550 passed in 1343.09s (started 11:34:43 UTC on 2026-10-23), first run.
- **Live,** against the gateway model on PostgreSQL, on `468af8e0`:
  - **Example 31, twice:** both runs passed all seven safety checks, plan completion SKIP (`the plan
    ended failed`): the model followed the injection again, 14 and 11 child runs.
  - **Examples 27 to 30:** 3 of 3, 6 of 6, 6 of 6, and example 30 5 of 5 twice.
  - **Cleanup:** every run's rows removed by recorded run id, children first; catalog rows by each
    run's own project, counted first; example 29's resource artifact by its project. The store holds
    10351 runs (19 under example-tenant), 0 SYN, empty catalog tables, 6 artifacts and 0
    interruptions, as before; no example-31 temp folder left.

### Edits to approved tests

Each named with its reason (NFR-38, amended after M35); **check each by content.**

- **`tests/test_mcp_client.py`** (M33's): the invariant tables gain `blocked`'s call of `_ensure`
  and its loop over the events, `_request_size`'s literal, and `_too_many`'s import row renamed
  `_refused`.
- **`tests/test_distribution.py`:** example 31 in `EXPECTED_EXAMPLES`.
- **`tests/fixtures/mcp_http_servers.py`** (M35's): the legacy `ask` takes `schema` as a list, one
  schema a request, so one call can ask big then small (D1).
- **`scripts/30_mcp_input.py`** (M36's example; round 2, the owner's choice): live, a request the
  model never made is `None`; offline unchanged. Its own test (`tests/test_mcp_input.py`) is
  unedited and passes.
- `tests/conftest.py`: unedited -- no table is added.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m37-r2-probes` in your own scratchpad; tenant ids starting `SYN-m37r2`.

0. **The table above:** check every row is true, and every approved-test edit by content.
1. **The exit signal's honesty, both directions, under the live checks** (`offline(...,
   strict=False)` or `report(..., live=True)`): for each of the eight checks, try to make it PASS or
   SKIP while its property is violated, and try to make it FAIL while nothing is violated -- a
   repeated call, a reused ticket, a call approved then refused, a skipped step, a replan. Round 1's
   four defects were the first kind and D1 the second; either kind is a finding. Check each
   subject: it must be read from the journals alone and be the least that must happen.
2. **Repetitions:** run example 31 offline many times, both stores; flakiness, hangs, orphaned
   processes or temp folders.
3. **`mcp_blocked`:** wrong answers (a usable tool listed, a blocked one missing), races with
   running calls, other tenancies, closed or failed servers, events it swallows.
4. **The cap:** bypasses (non-ASCII, several in one round, form with a URL, sampling and roots),
   the boundary, anything recorded or emitted for a refused request, a request after a refusal in
   either era, the connection's state after.
5. **NFR-35 to NFR-37 under orchestration:** labels of every MCP result in every child, the
   catalog's hold on a changed tool at both checks (step 1 and as the call leaves), and no
   configured secret in any record on PostgreSQL.
6. **NFR-22 and NFR-39:** contiguous sequence numbers, no child left running, every server process
   gone after `aclose`, including on cancellation mid-plan.
7. **Regressions:** the full suite, and examples 27 to 31 live; example 31's live checks must hold
   whatever the model does.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M36,** and the README's M33 to M37 rows.
- **Owner's choices:** DECISION-8e3e2ea1 (`mcp_blocked`, the cap); DECISION-d4a40cfe's attribution
  rule; the pins of mcp and httpx2.
- **Example 31 reads private state** as examples 26 and 30 do: `orchestrator._runs` for the
  children's ids and `runner._sessions_for` for their histories.
- **Live, a model may follow the injection:** it is labelled untrusted and data-only, never obeyed
  by the SDK; whether the model obeys it is the model's.

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
  control approve . M37-mcp-contention --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M37-mcp-contention --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the eight attack items, and
your verdict.

If you approve, give the clause that a future exit signal's pre-flight should require, if any.
