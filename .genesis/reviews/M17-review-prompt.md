You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Fable 5.1.** M17 was implemented by Opus 5. M16 was
reviewed by Fable 5.1 (round 1) and Sonnet 5 (round 2); M15 by Sonnet 5. If you have reviewed M17
before, say so and ask for a different session. Across M5 to M16 nearly every defect was found in
a region the previous reviewer had not examined. **This milestone is about money**, so an error
here is quiet and expensive: check the arithmetic yourself rather than reading it. Re-derive the
requirements from SPEC.md rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M16 adds no library.
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, now including `plan_versions` and
`plan_node_states`), the development database is shared, and M14's timing tests assert lower
bounds. Before running tests or gates, check that no other python or node process is running. Run
your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache, so a cache write can make
a gate read stale with no code change. The full suite takes about 4 minutes; keep every command in
the foreground and under 590 s rather than backgrounding it and waiting.

**If the suite hangs, it is not memory.** Measured on 2026-09-19 (KNOWLEDGE-9a3abf61): the suite
peaks at 32 MB and passed with 924 MB free. Suspect the two live-gateway tests in
`test_golden_eval.py`, whose calls have no end-to-end deadline; rerun with
`-o faulthandler_timeout=120` to see where it stopped.

## Task under review

**M17-budget-governor**: runs carry an ADR-06 budget: reservations, reclaim and soft
enforcement in USD and tokens, the P2-D19 defaults, and a dated price table that makes an
unpriced USD budget a configuration error.

First review round. The requirements are FR-67, FR-68, FR-69, AC-53, AC-54, AC-55 and NFR-23,
quoted below. Decisions that bear on it: P2-D19 and P2-D20 (DECISION-4aa01ef9), and the two
pre-flight clarifications **DECISION-c274eb02** (a governor over a PlanVersion plus an internal
lease the agent loop checks, because nothing runs plan nodes or child runs until M18 and M19, and
budgets for plain single-agent runs stay out of scope per backlog I-04) and **DECISION-727f3a42**
(the shipped price table holds `openai.gpt-4o-mini` alone). The pre-flight is KNOWLEDGE-b40dd9de.
ADR-06 itself is DECISION-e1bf0327. D13 applies (DECISION-2bad84bb): in-process caller code is
trusted.

**M17 is not committed.** HEAD is `bc1ca3e`. Review it with:

```bash
git diff bc1ca3e -- agentsdk tests scripts pyproject.toml README.md
git status --short   # new: agentsdk/budget.py, agentsdk/prices.py, agentsdk/prices.json,
                     #      agentsdk/migrations/0008_budget.sql, scripts/15_budget.py,
                     #      tests/test_budget.py
```

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-67:** `BudgetPolicy` is a public frozen type with `run_ceiling_usd`, `run_ceiling_tokens` (at least one set; both may be), `orchestrator_reserve_fraction` (default 0.20), `unallocated_reserve_fraction` (default 0.20), `reservation_cap_fraction` (default 0.40) and `max_replans` (FR-75), each validated at construction by field name (P2-D19). A run's ceiling is split at plan time: the orchestrator reserve, the unallocated reserve, and a reservation for each node. The planner proposes each node's `budget_reservation`; a proposal above `reservation_cap_fraction` of the currently unallocated pool is capped to it, and nodes that propose none split what is left equally. Additional allocation comes only from the unallocated reserve, by deterministic orchestrator policy, never at a model's request (ADR-06, DECISION-e1bf0327).
- **FR-68:** Enforcement is soft and checked before each model call. An agent at or over its reservation makes no further call, so it can exceed that reservation by at most one call, and a run's committed plus spent budget can exceed its ceiling by at most one call per concurrently running agent, the orchestrator included; `max_concurrent_subagents` (FR-72) bounds that overshoot to 4 by default. Overshoot is charged to the run. A child that reaches its reservation, and a run that reaches its ceiling, end `failed` with reason `budget_exceeded`; once a run's spend reaches its ceiling no new model call and no new child run starts. Unspent reservation returns to the run pool when a node ends, in whichever units have a ceiling. A nested child reserves from its parent's reservation, never from the run pool; retries of a node draw on that node's reservation; replanning stays inside the run ceiling. Migration `0008` records the run's effective `BudgetPolicy`, its reservations and its final spend in the `execution_manifests` row, NULL for rows written before it, idempotent per FR-17. Budgets for plain single-agent runs stay out of scope (backlog I-04).
- **FR-69:** A dated price table ships inside the package and is the default source of `ModelPricing` (P2-D20). It names its own date, which is reported wherever a price is used and recorded in the `ExecutionManifest`, so a stale price is never read as current fact. A caller-supplied `ModelPricing` overrides it for that model. A USD ceiling on a model the effective pricing does not cover is a configuration error raised at the call site, naming the model, not a silently unpriced run; a token ceiling always applies, priced or not, and an unknown cost stays None and is never reported as 0 (NFR-11). `scripts/15_budget.py` runs a plan whose second node exceeds its reservation and prints the reservations, the reclaim and the `budget_exceeded` ending.
- **NFR-23:** Budgets and prices are honest. A run's recorded spend equals the sum of its `ModelCalled` costs plus its children's, an unknown cost stays None rather than 0 (NFR-11), the shipped price table reports its own date wherever it is used, and the overshoot soft enforcement permits is bounded by `max_concurrent_subagents` and charged to the run rather than hidden.
- **AC-53:** With a scripted client whose calls cost known amounts, a four-node plan under a USD ceiling shows the arithmetic of FR-67 and FR-68: the orchestrator and unallocated reserves are 20% each, a node proposing more than 40% of the unallocated pool is capped to it, nodes proposing nothing split the remainder equally, and a node that ends under its reservation returns the difference to the run pool.
- **AC-54:** Under `max_concurrent_subagents` of 4 and a run ceiling reached mid-flight, the run's total spend exceeds its ceiling by at most one model call per concurrently running agent, every agent at or over its reservation makes no further call, and the run ends `failed` with reason `budget_exceeded` while no new child run has started.
- **AC-55:** A USD ceiling on a model the effective pricing does not cover raises a configuration error at the call site naming that model; the same run under a token ceiling completes; a caller-supplied `ModelPricing` overrides the shipped table; and the table's date appears in the `ExecutionManifest`.

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve, validate, permission, execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.
- A cancellation is recorded, then re-raised (INVARIANT-73299c7c).

### Files in scope

- `agentsdk/budget.py` (new): `BudgetPolicy`, `BudgetAmount`, `BudgetAllocation`,
  `BudgetGovernor`, `BudgetLease`, and the plan-time split.
- `agentsdk/prices.py` and `agentsdk/prices.json` (new): the dated table and its lookup.
- `agentsdk/migrations/0008_budget.sql` (new): four columns on `execution_manifests`.
- `agentsdk/api.py`: `RunConfig.budget_lease`; `_effective_pricing`, which puts the shipped table
  behind the caller's registry; `_budget_record`, which raises `UnpricedModel` at the call site
  and builds the manifest's budget; `_spend_record`; the lease reaching `AgentLoop`; the spend
  written at every terminal path, including cancellation.
- `agentsdk/loop.py`: the lease asked before each model call and charged after each one.
- `agentsdk/manifest.py`, `agentsdk/postgres.py`, `agentsdk/persistence.py`: the manifest keys,
  the columns, `finish_run(budget_spend=...)` and `records_budget`.
- `agentsdk/errors.py`: `UnpricedModel`; `BudgetExceeded` is no longer a placeholder.
- `agentsdk/__init__.py`, `pyproject.toml` (package data), `README.md`.
- `scripts/15_budget.py` (new), the demo command; `tests/test_distribution.py` lists it.
- `tests/test_budget.py` (new, 40 tests).

## What the author ran

1. **Pre-flight**, KNOWLEDGE-b40dd9de: baseline 1375 passed in 233.16 s at `7d9e485`; the code
   facts M17 builds on; and the prices, read from the providers' own pages on 2026-09-19.
2. **Tests first.** Against no implementation: 37 failed, 1 passed. The failures were 33
   `ModuleNotFoundError` for `agentsdk.budget` and four missing pieces; the pass was a pricing
   test that guards behaviour M17 must not change.
3. **After the implementation:** 40 passed, and the full suite 1415 passed in 236.19 s.
4. **Demo command, both modes:** `python scripts/15_budget.py --offline` and live against the
   gateway and Postgres, 8 of 8 checks each; the live run cost 0.000115 USD.
5. **Mutation run**, every file restored and SHA-256 verified, every `.py`, `.sql` and `.json`
   under `agentsdk/` hashed before and after: **26 of 26 killed**, each by the test written for
   it. A2 to A4 below say how it got there. Mutants of `0008` run the migration test alone with
   `--noconftest`, because the live database's checksum guard would otherwise kill them for the
   wrong reason.
6. **Gates.** Both green on source hash `1ace9471`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_budget.py -q`, exit 0, 40 passed in 3.69s
     (11:18:03 to 11:18:07 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1415 passed in 236.19s (0:03:56)
     (11:18:07 to 11:22:05 UTC).

## Issues the author found during M17

| id | finding | disposition |
|---|---|---|
| A1 | The wiring read `config` inside `_cancelled`, which does not have it. Every cancellation path raised `NameError`: **40 existing tests failed**, in `test_telemetry.py` and `test_run_handles.py` | fixed by passing the lease into `_cancelled`; the full suite is what caught it, the gate file alone did not |
| A2 | The USD-ceiling refusal was first raised inside `_run`, where every exception becomes a FAILED result rather than a configuration error | moved to `_open`, which is documented as where configuration errors raise, so `run()` and `start()` both refuse at the call site. Mutant A3 covers it |
| A3 | Two mutants survived: a released lease could still call, and a token reservation was not enforced on a lease | two tests added; both mutants then killed, and the final run is 26 of 26 |
| A4 | One mutant's snippet did not match the code, so it reported "SNIPPET FOUND 0 TIMES" rather than a result | the snippet was corrected and the mutant killed; it is not counted as a kill until it ran |
| A5 | The live half of the example reached into `governor._reservations` to make a node overshoot | rewritten: the plan's own proposal carries the small reservation, so the example uses only public API |
| A6 | FR-67 says a proposal above the cap fraction of "the currently unallocated pool" is capped. Read here as the pool still unallocated **to nodes**, measured as each proposal is taken, in plan order | stated in the code and in AC-53's test; another reading would give different numbers |
| A7 | `_budget_record` is computed twice: once in `_open` to raise the refusal, once in `_run` for the manifest | left as is; it is a pure function of the lease and the model |
| A8 | An unknown USD cost under a USD ceiling makes the run's spend unknowable, so `_within_ceiling` treats it as reached and stops further calls | deliberate and declared below; a USD ceiling already requires a priced model, so this is the cancelled-in-flight path (P2-D7) |
| A9 | `orchestrator_lease()` adds a pseudo node `<orchestrator>` to the reservations, so it appears in `allocation.reservations` once it has been asked for | declared; M19 is what uses it |
| A10 | `top_up` raises `KeyError` for an unknown node, not a `ValueError` like the field refusals | pinned by a test; a mapping lookup, not a validated field |
| A11 | The live example has to supply its own `ModelPricing`, because the gateway's default model is not in the shipped table | expected under DECISION-727f3a42, and the example says so |
| A12 | M15 costed `bedrock.anthropic.claude-haiku-4-5` at Anthropic's first-party rate, though Bedrock sets its own and charges 10% more on regional endpoints | recorded in KNOWLEDGE-b40dd9de; it may understate both M15 arms by 10% without changing their ratio. Out of scope here |

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save every probe script under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m17r1-probes\`

with shared redaction and cleanup in one `common.py`, and use tenant ids starting `SYN-m17r1`.

1. **The arithmetic.** Can the split lose or invent money, in either unit? Reserves, caps, the
   equal share, rounding and Decimal quantisation, integer division for tokens, many nodes, one
   node, tiny and huge ceilings. Does every allocation still sum to the ceiling, and does a
   reclaim ever return more than it held?
2. **Enforcement.** Is the overshoot really bounded by one call per agent, under real concurrency
   and under threads? Do `charge`, `release`, `top_up` and `may_call` interleave safely? Can a
   released or exhausted lease spend again, directly or through a run?
3. **The loop.** Does *every* model call pass the lease, on every path: retries inside the client,
   a `before_model` hook that changes the model, parallel tool calls, cancellation mid-call, a run
   that fails? Is anything charged twice, or charged when no call was made? A1 is the warning.
4. **Pricing.** Registry versus shipped table precedence, the `UnpricedModel` refusal from both
   `run()` and `start()`, a token-only ceiling on an unpriced model, and the table's date reaching
   the manifest. Are the two prices in `prices.json` what the cited pages say?
5. **The manifest and 0008.** Every value, the NULLs for runs without a budget, idempotency, money
   stored as text rather than a JSON number, and the spend written on completed, failed and
   cancelled runs alike.
6. **NFR-23 and AC-44.** Does a run's recorded spend equal the sum of its `ModelCalled` costs on
   both stores? Any residue after a full run?
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **Nothing drives plan nodes or child runs yet.** M17's leases are handed to runs by whatever
  acts as the orchestrator; M18 and M19 bring the real ones (DECISION-c274eb02). Nested children
  reserving from a parent's reservation is FR-68's text but M18's work.
- **A8, A9, A10, A11, A12** above.
- **The governor lives in one process.** Nothing persists it; a restart loses the allocation, and
  only what reached the manifest survives.
- **`release()` is called by the orchestrator**, not automatically when a run ends.
- **The price table holds one model** (DECISION-727f3a42).
- **Older `independent-review` gates** compute stale because the repository hash moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T`). A mutant that edits `0008` must run the migration test
  alone with `--noconftest`, or the live checksum guard kills it for the wrong reason.
- **Clean up** every run, row, temp folder and process you create, and say what you removed.
- **Report every probe**, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M17-budget-governor --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M17-budget-governor --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.

