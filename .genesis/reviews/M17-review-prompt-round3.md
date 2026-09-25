You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Fable 5.1.** M17 was implemented by Opus 5; round 1 was
reviewed by Fable 5.1 and round 2 by Sonnet 5, and both rejected it. Fable 5.1 has not seen the
code as it now stands. If you have reviewed this round before, say so and ask for a different
session. **This milestone is about money**, and both defects so far were a single wrong number
written into a real row while the whole suite passed. Check what is stored, not what the code
says it stores. Re-derive the requirements from SPEC.md rather than inheriting the framing below.

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

**M17-budget-governor**, **round 3**: runs carry an ADR-06 budget: reservations, reclaim and soft
enforcement in USD and tokens, the P2-D19 defaults, and a dated price table that makes an unpriced
USD budget a configuration error.

Two rounds have rejected this milestone, both for the same shape of defect: **a quietly wrong
number written into a real row.**

- **Round 1** (Fable 5.1, 2026-09-20T12:09:31Z), F1: `budget_spend` held the lease's running total
  rather than the run's own spend, so a run that made no call recorded one. Fixed, DECISION-f831a12b.
- **Round 2** (Sonnet 5, 2026-09-20T14:03:39Z), G1: the same column recorded **0 tokens** for a
  call that spent 11, because `_spend_record` read `usage.total_tokens` while the lease counted
  prompt plus completion. Round 1's own F5 fix had been applied in one place and not the other.
  Fixed, DECISION-4efea5e8.

Read both decisions first. Assume the third one is there too, in something neither round looked
at. Your job is to judge the fixed code and M17 as a whole; you are a different model from both
previous rounds and should not inherit their coverage.

The requirements are FR-67, FR-68, FR-69, AC-53, AC-54, AC-55 and NFR-23, quoted below. Other
decisions: P2-D19 and P2-D20 (DECISION-4aa01ef9), ADR-06 (DECISION-e1bf0327), DECISION-c274eb02
(the governor plus an internal lease; budgets for plain single-agent runs are out of scope, backlog
I-04) and DECISION-727f3a42 (the table holds `openai.gpt-4o-mini` alone). Pre-flight:
KNOWLEDGE-b40dd9de. D13 applies (DECISION-2bad84bb).

**M17 is not committed.** HEAD is `bc1ca3e`. No snapshot of either earlier tree was kept, so there
is no round 3 patch. Review the whole milestone with:

```bash
git diff bc1ca3e -- agentsdk tests scripts pyproject.toml README.md
git status --short
```

### What changed since round 2

- **G1.** `budget.tokens_of(usage)` is now public and is the single counter of a call's tokens:
  the lease and the manifest both use it, so a provider that reports the parts and no total is
  charged and recorded identically. A test drives a full `Runner.run()` with such a provider and
  reads the stored row back. Every other reader of `total_tokens` in the package was checked: only
  the `Usage` field and the `runs` columns, which record what a provider reported, by design since
  M9.
- **G2.** Releasing a lease now also shrinks the node's reservation to what it spent, so the
  allocation keeps summing to the run's ceiling. Round 2 saw 107.00 against a 100.00 ceiling.
- **A consequence, declared.** With the reservation shrunk, the explicit `released` guard in
  `may_call` is redundant: the spend then equals the reservation and the arithmetic refuses on its
  own. The guard is kept as a second, independent reason to refuse, and its mutant (B13) is
  **declared equivalent** rather than counted as a kill.
- **Tests:** 2 added (57 to 59), both seen failing first.

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
  `_spend_record`, and every terminal path.
- `agentsdk/loop.py`: the lease asked before each call, charged after each, and on cancellation.
- `agentsdk/manifest.py`, `agentsdk/postgres.py`, `agentsdk/persistence.py`, `agentsdk/errors.py`,
  `agentsdk/__init__.py`, `pyproject.toml`, `README.md`.
- `scripts/15_budget.py`; `tests/test_distribution.py` lists it; `tests/test_budget.py` (59 tests).

## What the author ran for round 3

1. **Reproduced G1 first**, offline: the manifest recorded `tokens: 0` where the lease charged 11.
2. **Tests first:** both new tests failed on the round 2 code, then passed.
3. **Full suite:** 1434 passed in 241.46 s.
4. **Demo command:** 8 of 8 offline and live.
5. **Mutation run**, files restored and SHA-256 verified: **37 of 38 killed, 1 equivalent (B13)**,
   including one mutant per finding from both earlier rounds, so any of them regressing fails the
   gate.
6. **Gates.** Both green on source hash `d91b83d7`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_budget.py -q`, exit 0, 59 passed in 5.35s (14:29:52 to 14:29:58 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1434 passed in 240.80s (0:04:00)
     (14:29:59 to 14:34:01 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m17r3-probes\`

with shared cleanup in one `common.py`, and tenant ids starting `SYN-m17r3`. The earlier rounds'
probes are in `m17r1-probes\` and `m17r2-probes\`.

1. **Every number that reaches a row.** Both rejections were one value, stored wrong, that the
   suite did not notice. Drive real runs through every ending and compare what is persisted, in
   both units, against what actually happened: `budget_spend`, `node_total`, `budget_reservations`,
   `budget_policy`, `price_table_date`, `runs.cost_usd`, the `ModelCalled` payloads. Unusual
   providers are where both defects lived: no total, only a total, zero usage, huge counts,
   negative counts, cached tokens, no usage object at all.
2. **The arithmetic**, with the rounding and the new shrink-on-release: any ceiling, any node
   count, tiny ceilings where a share rounds to zero, repeated release and top-up. Does the
   allocation always sum to the ceiling, and can a node or the reserve go negative?
3. **Enforcement and concurrency**, including the overshoot bound, threads, and a released or
   exhausted lease.
4. **The loop**: every path charges once and only once, cancellation included.
5. **Pricing**: precedence, the call-site refusal from `run()` and `start()`, F7's hook path, and
   whether `prices.json` matches the cited page today.
6. **The manifest and 0008**, and **NFR-23 and AC-44** on both stores.
7. **The demo command**, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **B13 is an equivalent mutant** (above).
- **F7:** a `before_model` hook can send a model the call-site check never saw; the call proceeds,
  its cost is unknown, and the next call is refused. Pinned by a test.
- **F8:** the cited page lists no cache-write price, so that class is unpriced rather than 0.
- **Three manifest rows in the development database predate round 1's fix** and keep the old
  meaning, with no `node_total` key; demo rows under `example-tenant`, left as they are.
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

