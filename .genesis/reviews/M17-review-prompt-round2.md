You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Sonnet 5.** M17 was implemented by Opus 5 and its round 1
was reviewed by Fable 5.1, which rejected it. If you have reviewed M17 before, say so and ask for
a different session. **This milestone is about money**, and round 1's defect was a column that
quietly recorded the wrong number into real rows: check the arithmetic and the stored records
yourself rather than reading the code's word for them. Re-derive the requirements from SPEC.md
rather than inheriting the framing below.

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

**M17-budget-governor**, **round 2**: runs carry an ADR-06 budget: reservations, reclaim and soft
enforcement in USD and tokens, the P2-D19 defaults, and a dated price table that makes an unpriced
USD budget a configuration error.

Round 1 (Fable 5.1, 2026-09-20T12:09:31Z) **rejected** it. F1: `execution_manifests.budget_spend`
held the lease's running total rather than what the run spent, so a second run on one lease
inherited the earlier run's spend and a run refused before its first call recorded a spend it
never made. The shipped demo had written such a row into the development database. The owner
agreed, and the fixes are recorded as **DECISION-f831a12b**, which you should read first. Your job
is to judge the fixed code, and M17 as a whole; round 1 is a different model and you should not
inherit its coverage.

The requirements are FR-67, FR-68, FR-69, AC-53, AC-54, AC-55 and NFR-23, quoted below. Other
decisions that bear on it: P2-D19 and P2-D20 (DECISION-4aa01ef9), ADR-06 itself
(DECISION-e1bf0327), and the two pre-flight clarifications DECISION-c274eb02 (a governor over a
PlanVersion plus an internal lease the loop checks; budgets for plain single-agent runs stay out
of scope per backlog I-04) and DECISION-727f3a42 (the shipped table holds `openai.gpt-4o-mini`
alone). The pre-flight is KNOWLEDGE-b40dd9de. D13 applies (DECISION-2bad84bb).

**M17 is not committed.** HEAD is `bc1ca3e`. No snapshot of the round 1 tree was kept, so there is
no round 2 patch; the changes are listed below. Review the whole milestone with:

```bash
git diff bc1ca3e -- agentsdk tests scripts pyproject.toml README.md
git status --short
```

### What changed since round 1

- **F1, the rejection.** `Runner._spend_record(lease, usage, cost_usd)` now records **this run's**
  spend from its own meter, with the node's running total beside it under `node_total`. Every
  terminal path passes the meter: completed, failed and cancelled. Checked on new rows in the
  development database: a refused run records `0` against `cost_usd` `0`, where before it recorded
  `0.000055`.
- **F2.** A call cancelled in flight may already be billed and reports no usage (P2-D7), so the
  loop now charges the lease with an unknown cost: the governor's USD spend becomes unknown rather
  than staying exact, and further calls are refused.
- **F3, F4.** `top_up` refuses a released lease, and validates both units before moving either, so
  a top-up refused on tokens no longer leaves the USD half spent.
- **F5.** `_tokens_of` adds prompt and completion when a provider reports no total.
- **F6.** An equal share is rounded **down** to 18 decimal places and the remainder goes to the
  unallocated reserve, so a pool that does not divide exactly still sums to the ceiling in any
  order a consumer adds it. Round 1 saw drift at the 28th digit.
- **F9.** The four boundaries round 1 found unpinned now have tests: a lease exactly at its
  reservation, a run exactly at its ceiling, a reclaim floor, and a plan where every node proposes.
- **F7, declared not fixed.** A `before_model` hook can send a model that the call-site check never
  saw. The call is not stopped; its cost is unknown, so the spend becomes unknowable and the next
  call is refused rather than spent blind. A test pins that.
- **F8, recorded.** The cited page lists no cache-write price, so `cache_write` is absent rather
  than 0 in `prices.json`, with a note saying so; a call reporting cache-write tokens therefore
  cannot be priced, and its run's cost stays unknown (NFR-11).
- **Tests:** 17 added (40 to 57), each seen failing first where it covers a fix.

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

- `agentsdk/budget.py`: the policy, the split, the leases, the reclaim, `top_up`, `_tokens_of`.
- `agentsdk/prices.py`, `agentsdk/prices.json`: the dated table and its note.
- `agentsdk/migrations/0008_budget.sql`: four columns on `execution_manifests`.
- `agentsdk/api.py`: `RunConfig.budget_lease`; `_effective_pricing`; `_budget_record`, which raises
  `UnpricedModel` at the call site; `_spend_record`, the F1 fix; the terminal paths.
- `agentsdk/loop.py`: the lease asked before each call, charged after each, and on cancellation.
- `agentsdk/manifest.py`, `agentsdk/postgres.py`, `agentsdk/persistence.py`, `agentsdk/errors.py`,
  `agentsdk/__init__.py`, `pyproject.toml`, `README.md`.
- `scripts/15_budget.py`, the demo command; `tests/test_distribution.py` lists it.
- `tests/test_budget.py` (57 tests).

## What the author ran for round 2

1. **Reproduced F1 first**, in the development database: run `f6de1db9`, written by the shipped
   demo, had `cost_usd` `0` with `budget_spend.usd` `0.000055` and no model call.
2. **Tests first.** With the new tests and the round 1 code: 6 failed, one per fix.
3. **After the fixes:** 57 passed; the full suite 1432 passed in 262.52 s.
4. **Demo command:** 8 of 8 offline and live; the live run's rows were then read back from the
   database and agree with `runs.cost_usd` on every row.
5. **Mutation run**, every file restored and SHA-256 verified: **36 of 36 killed**, including ten
   new mutants, one for each round 1 finding, so a regression on any of them fails the gate.
6. **Gates.** Both green on source hash `42e808d0`, the hash KICKOFF.md records, from one
   `genesis gate` run; `independent-review` is pending, as round 1 rejected.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_budget.py -q`, exit 0, 57 passed in 5.44s (13:22:35 to 13:22:41 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1432 passed in 238.83s (0:03:58)
     (13:22:41 to 13:26:42 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save every probe script under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m17r2-probes\`

with shared redaction and cleanup in one `common.py`, and use tenant ids starting `SYN-m17r2`.
Round 1's probes are in `m17r1-probes\` beside it.

1. **F1 end to end.** Does `budget_spend` equal `runs.cost_usd` and the sum of that run's
   `ModelCalled` costs, on every ending: completed, failed, refused before its first call,
   cancelled, max-turns, and a second run on the same lease? Is `node_total` right at each point?
2. **The arithmetic, again.** With rounding now in the split: does it still sum to the ceiling for
   any ceiling and any number of nodes, including tiny ceilings where an 18-decimal step could
   round a share to zero? Can a node be allocated more than the pool, or the reserve go negative?
3. **Enforcement and concurrency.** The overshoot bound under real concurrency and threads;
   `charge`, `release`, `top_up` and `may_call` interleaved; a released or exhausted lease.
4. **The loop.** Does every call go through the lease, on every path, including the cancellation
   charge F2 added? Anything charged twice, or charged when no call was made?
5. **Pricing.** Registry versus table precedence, the refusal from `run()` and `start()`, a
   token-only ceiling, the date reaching the manifest, and F7's hook path. Are the two prices in
   `prices.json` what the cited page says? Round 1 said it checked; the owner did not re-fetch.
6. **The manifest and 0008**, and **NFR-23 and AC-44** on both stores.
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **F7 and F8** above, both pinned or recorded rather than fixed.
- **Three manifest rows in the development database predate the F1 fix** and keep the old meaning;
  they have no `node_total` key. They are demo rows under `example-tenant`, left as they are.
- **Nothing drives plan nodes or child runs yet** (DECISION-c274eb02); nested children reserving
  from a parent's reservation is FR-68's text but M18's work.
- **The governor lives in one process**; only what reached the manifest survives a restart.
- **`release()` is called by the orchestrator**, not automatically when a run ends.
- **The price table holds one model** (DECISION-727f3a42), and Haiku's Bedrock rate is still
  unverified (KNOWLEDGE-b40dd9de), which also leaves M15's dollar figures possibly 10% low.
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

