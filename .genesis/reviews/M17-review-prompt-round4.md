You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Sonnet 5.** M17 was implemented by Opus 5; rounds 1 and 3
were reviewed by Fable 5.1 and round 2 by Sonnet 5, and all three rejected it. Sonnet 5 has not
seen the code as it now stands. If you have reviewed this round before, say so and ask for a
different session. **This milestone is about money, and three rounds have each found one wrong
number that the entire suite missed.** Check what is stored and what escapes, not what the code
says it does. Re-derive the requirements from SPEC.md rather than inheriting the framing below.

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

**M17-budget-governor**, **round 4**: runs carry an ADR-06 budget: reservations, reclaim and soft
enforcement in USD and tokens, the P2-D19 defaults, and a dated price table that makes an unpriced
USD budget a configuration error.

**Three rounds have rejected this milestone**, each finding one wrong number that the whole suite
missed:

| round | reviewer | finding | fixed in |
|---|---|---|---|
| 1 | Fable 5.1 | F1: `budget_spend` held the lease's running total, so a run that made no call recorded one | DECISION-f831a12b |
| 2 | Sonnet 5 | G1: the same column recorded 0 tokens for an 11-token call, because round 1's own fallback was applied to the lease and not to the record | DECISION-4efea5e8 |
| 3 | Fable 5.1 | H1: a negative token report made `Runner.run()` raise from inside its own error handler, leaving a run `running` for ever with both terminal events written. H2: the record took the run's summed usage instead of adding what each call counted | DECISION-307f0c8d |

Read those three decisions first. The author was asked for a structural fix this time rather than
a third point repair, and made one; judge whether it is actually structural, and assume a fourth
defect is there in something none of the three rounds looked at. You are a different model from
rounds 1 and 3; do not inherit any round's coverage.

The requirements are FR-67, FR-68, FR-69, AC-53, AC-54, AC-55 and NFR-23, quoted below. Other
decisions: P2-D19 and P2-D20 (DECISION-4aa01ef9), ADR-06 (DECISION-e1bf0327), DECISION-c274eb02
(the governor plus an internal lease; budgets for plain single-agent runs are out of scope, backlog
I-04) and DECISION-727f3a42 (the table holds `openai.gpt-4o-mini` alone). Pre-flight:
KNOWLEDGE-b40dd9de. D13 applies (DECISION-2bad84bb).

**M17 is not committed.** HEAD is `bc1ca3e`. Review it with:

```bash
git diff bc1ca3e -- agentsdk tests scripts pyproject.toml README.md
git status --short
```

### What changed since round 3

- **An amount records; it does not police.** `BudgetAmount` now checks only that a value is the
  right type and finite. Sign and range belong to configuration, which is checked where a ceiling,
  a reservation or a top-up is set. Round 3's run died because a *record* refused what a provider
  reported.
- **`tokens_of` clamps at zero.** A provider reporting fewer than zero tokens spends nothing and
  cannot buy budget back by saying so.
- **The spend record is total.** `_spend_record` cannot fail a run: it writes nulls rather than
  something wrong, and a lease that breaks mid-run still leaves a terminal run and an honest row. A
  lease already unreadable when the run is configured is refused at the call site, before any row
  exists.
- **The record sums what each call counted** (H2), through a per-run counter on `RunMeter` that
  uses the same `tokens_of` the lease charges with, so the two can no longer disagree.
- **H3:** releasing the orchestrator lease now shrinks its reserve, so the allocation keeps summing
  to the ceiling.
- **H4:** the USD split runs in a wide Decimal context; a ceiling of 1e11 splits exactly instead of
  raising a raw `decimal.InvalidOperation`.
- **H5:** the price table's date is recorded when that table actually prices a call, including one
  a `before_model` hook moved to another model. The redundant open-time computation was removed,
  which is what made its mutant survive.
- **Tests:** 28 added (59 to 87), including a property-style test that drives a run for nine shapes
  of `Usage` the type can hold, against both kinds of ceiling, asserting a terminal status, exactly
  one terminal event, and a governor still readable afterwards.

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

- `agentsdk/budget.py`: the policy, the split, the leases, the reclaim, `top_up`, `tokens_of`.
- `agentsdk/prices.py`, `agentsdk/prices.json`; `agentsdk/migrations/0008_budget.sql`.
- `agentsdk/api.py`: `RunConfig.budget_lease`, `_effective_pricing`, `_budget_record`,
  `_spend_record` and `_spend_fields`, `cost_of`, and every terminal path.
- `agentsdk/loop.py`: `RunMeter.budget_tokens` and `price_table_date`; the lease asked before each
  call, charged after each, and on cancellation.
- `agentsdk/manifest.py`, `agentsdk/postgres.py`, `agentsdk/persistence.py`, `agentsdk/errors.py`,
  `agentsdk/__init__.py`, `pyproject.toml`, `README.md`.
- `scripts/15_budget.py`; `tests/test_distribution.py` lists it; `tests/test_budget.py` (87 tests).

## What the author ran for round 4

1. **Reproduced H1 and H2 first**, then wrote the tests, which failed on the round 3 code.
2. **Gate file:** 87 passed. **Full suite:** 1462 passed.
3. **Demo command:** 8 of 8 offline and live.
4. **Mutation run**, files restored and SHA-256 verified: **44 of 46 killed, 2 declared
   equivalent** (below), with a mutant for every finding of all three rounds.
5. **Gates.** Both green on source hash `608727b3`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_budget.py -q`, exit 0, 87 passed in 10.84s (15:55:05 to 15:55:16 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1462 passed in 240.95s (0:04:00)
     (15:55:16 to 15:59:19 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m17r4-probes\`

with shared cleanup in one `common.py`, and tenant ids starting `SYN-m17r4`. The earlier rounds'
probes are in `m17r1-probes\`, `m17r2-probes\` and `m17r3-probes\`.

1. **Is the totality real?** Round 3's defect was an exception from inside a terminal path. Break
   things the author did not: a lease that lies about its node id, a `governor` whose policy
   changes mid-run, a store that fails the budget UPDATE, a `charge` that raises, a usage object
   that is not a `Usage`. Does a run always reach a terminal status with exactly one terminal
   event, and does anything non-`AgentSDKError` escape `Runner.run()`?
2. **Every number that reaches a row**, in both units and on every ending, against what actually
   happened. Three rounds found one each here.
3. **The arithmetic** with rounding, the shrink on release, and extreme ceilings; does the
   allocation always sum, and can any part go negative?
4. **Enforcement and concurrency**: the overshoot bound, threads, released and exhausted leases,
   and whether a clamped negative report can be used to gain budget over many calls.
5. **Pricing**: precedence, the call-site refusal, the hook path, and whether `prices.json` still
   matches the cited page.
6. **The manifest and 0008**, and **NFR-23 and AC-44** on both stores.
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **Two equivalent mutants, both redundant guards kept as defence:** B13, the explicit `released`
  check in `may_call`, which the shrink-on-release now makes unreachable; and N16, restoring
  `BudgetAmount`'s refusal of negative tokens, which `tokens_of`'s clamp makes unconstructible.
- **F7:** a `before_model` hook can send a model the call-site check never saw; the call proceeds,
  its cost is unknown, and the next call is refused. Pinned by a test.
- **F8:** the cited page lists no cache-write price, so that class is unpriced rather than 0.
- **Three manifest rows in the development database predate round 1's fix** and keep the old
  meaning; demo rows under `example-tenant`, left as they are.
- **Nothing drives plan nodes or child runs yet** (DECISION-c274eb02).
- **The governor lives in one process**; only what reached the manifest survives a restart.
- **`release()` is called by the orchestrator**, not automatically when a run ends.
- **The table holds one model**; Haiku's Bedrock rate is unverified (KNOWLEDGE-b40dd9de), which
  also leaves M15's dollar figures possibly 10% low.
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

