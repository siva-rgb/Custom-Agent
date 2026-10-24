You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails to
start. M37 was implemented by Opus 5.5 and its pre-review run by an Opus 5.5 subagent; M36 was
reviewed by Sonnet 5 and M35 by Fable 5.1. Bring no memory of an earlier review into this one: if
your session holds any, say so and ask for a fresh one.

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
- KNOWLEDGE-7d24c4c5: the final mutation run, and the change check naming the catalog's refusal.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`; HEAD is `3f7753f` (M36). M37 is uncommitted. No migration is added.

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
a run: remove only those of tenancies your probes created (tenant ids starting `SYN-m37r1`). Example
31 offline makes a project of its own each run (`contention-<hex>`).

## Task under review

**M37-mcp-contention**, round 1, risk **high**.

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

### How the author checked it

- **Tests first** for the two pre-flight features: `mcp_blocked` and the cap's tests were written
  and run against the new code as it was built; the example and its gate test together.
- **Mutation:** 11 of 11 killed by named assertions in the final run, each file restored by
  SHA-256. These are the cap (boundary, absent, wrong reason, URL or schema unmeasured), D1,
  `blocked` (error, previous hash, stale read), `mcp_blocked`'s tenancy check, and the example's
  approver accepting the change at once. The first run killed three cap mutants only by hanging,
  which a short `approval_timeout` turned into assertion kills. The last mutant survived the run
  after the pre-review rework, which led to the offline check naming the catalog's own refusal
  (KNOWLEDGE-7d24c4c5).
- **A fresh pre-review agent** (runtime probes only, about 110 offline runs, rows removed by
  recorded id) found D1, D2 and W1 to W6; each was reproduced, fixed or declared. Its own runtime
  controls were run again after the fixes: `args_tamper`, `twice`, `wrong_route`, `error_internal`
  each fail their check; `usable_all`, `sdk_no_approval`, `auto_answer` each print `[FAIL]` live,
  not `[SKIP]`. `always_usable` passed at first, because the run's own guard (changed since
  offered) still refused the call. Since the final mutation run it fails too, because the offline
  check names the catalog's refusal. Its probes are in the author's scratchpad, `m37/prereview`.
- **Gates,** on source hash `8c511046`:
  - `unit`: 34 passed in 182.25s;
  - `regression`: 2544 passed in 1144.28s (started 14:21:24 UTC on 2026-10-22).
  The gates passed first time on each of three hashes: `9c02e515` (before the pre-review fixes),
  `281549a2` (after them and the live rework) and `8c511046` (after the final mutation run). The
  last change tightened only an offline branch of example 31.
- **Live,** against the gateway model on PostgreSQL:
  - **Example 31, first run:** five of eight checks failed. The model followed the injection,
    called `catalog__revise` with other children's tickets, and ran out of turns, so the plan was
    replanned into 16 child runs. The store showed no SDK violation: the only lookup that started
    before the new version's acceptance began before any revise (KNOWLEDGE-c59cba3a). The checks
    were then reworked to hold whatever the models decide.
  - **Example 31, second run:** on the reworked checks, before the final offline-only change, the
    seven safety checks passed and plan completion was SKIP. Again the model followed the
    injection, giving 13 child runs.
  - **Examples 27 to 30:** passed 3 of 3, 6 of 6, 6 of 6 and 5 of 5.
  - **Cleanup:** every run's rows were removed by recorded run id, children first. Catalog rows
    were removed by each run's own project, counted first, and example 29's one resource artifact
    by its project. The store holds 10351 runs (19 under example-tenant), 0 SYN, empty catalog
    tables, 6 artifacts and 0 interruptions, as before. No example-31 temp folder is left.

### Edits to approved tests

Each named with its reason (NFR-38, amended after M35); **check each by content.**

- **`tests/test_mcp_client.py`** (M33's): the invariant tables gain `blocked`'s call of `_ensure`
  and its loop over the events, `_request_size`'s literal, and `_too_many`'s import row renamed
  `_refused`.
- **`tests/test_distribution.py`:** example 31 in `EXPECTED_EXAMPLES`.
- **`tests/fixtures/mcp_http_servers.py`** (M35's): the legacy `ask` takes `schema` as a list, one
  schema a request, so one call can ask big then small (D1).
- `tests/conftest.py`: unedited -- no table is added.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m37-r1-probes` in your own scratchpad; tenant ids starting `SYN-m37r1`.

0. **The table above:** check every row is true, and every approved-test edit by content.
1. **The exit signal's honesty:** for each of the eight checks, try to make it pass while its
   property is violated (runtime monkeypatching only), live semantics included (`report(...,
   live=True)`). A check that passes over a violation, or reports SKIP over one, is a finding.
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
