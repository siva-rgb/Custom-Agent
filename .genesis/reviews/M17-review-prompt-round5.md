You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Fable 5.1**, which last saw this code at round 3. M17 was
implemented by Opus 5; rounds 1 and 3 were reviewed by Fable 5.1, rounds 2 and 4 by Sonnet 5, and
**all four rejected it**. If you have reviewed this round before, say so and ask for a different
session. This milestone is about money and about what a run leaves behind in the record, and four
rounds have each found one defect the entire suite missed. Check what is stored, what escapes, and
what is written after a terminal event -- not what the code says it does. Re-derive the
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

**M17-budget-governor**, **round 5**: runs carry an ADR-06 budget: reservations, reclaim and soft
enforcement in USD and tokens, the P2-D19 defaults, and a dated price table that makes an unpriced
USD budget a configuration error.

**Four rounds have rejected this milestone.** Every one found a single defect the whole suite
missed, and every one was in the same place: what M17 records, or the path that records it.

| round | reviewer | finding | decision |
|---|---|---|---|
| 1 | Fable 5.1 | F1: `budget_spend` held the lease's running total, so a run that made no call recorded one | DECISION-f831a12b |
| 2 | Sonnet 5 | G1: 0 tokens recorded for an 11-token call; round 1's own fallback was applied to the lease and not the record | DECISION-4efea5e8 |
| 3 | Fable 5.1 | H1: a negative token report made `Runner.run()` raise from inside its own error handler, leaving a run `running` with both terminal events written. H2: the record summed the wrong thing | DECISION-307f0c8d |
| 4 | Sonnet 5 | J1: the completion path wrote its terminal row unwrapped, so a failing budget write produced two terminal events, a row stuck `running`, and FAILED for a run that completed | DECISION-2df048ae |

Read all four decisions. Round 4's fix is worth your attention because the obvious repair was
wrong: wrapping the completion path the way the failure path is wrapped broke an **approved M4
contract** (`test_a_persistence_failure_on_the_success_path_is_not_swallowed`), whose point is that
a run recorded as still running is a lie the caller should hear about. The fix was rebuilt to keep
that contract and stop only what was actually wrong. Check that reasoning yourself; if you think
the M4 contract and FR-31 now disagree, say so rather than working around it.

Assume a fifth defect exists in something none of the four rounds looked at. You are Fable 5.1 and
reviewed rounds 1 and 3; do not inherit your own or anyone's coverage.

The requirements are FR-67, FR-68, FR-69, AC-53, AC-54, AC-55 and NFR-23, quoted below. Other
decisions: P2-D19 and P2-D20 (DECISION-4aa01ef9), ADR-06 (DECISION-e1bf0327), DECISION-c274eb02
and DECISION-727f3a42. Pre-flight: KNOWLEDGE-b40dd9de. D13 applies (DECISION-2bad84bb).

**M17 is not committed.** HEAD is `bc1ca3e`. Review it with:

```bash
git diff bc1ca3e -- agentsdk tests scripts pyproject.toml README.md
git status --short
```

### What changed since round 4

- **`RunControl.terminal_written`**, set only once a terminal event has actually been written, as
  distinct from `terminal`, which is set *before* the write so that a cancellation arriving during
  it has no effect. The failure handler now skips both the second terminal event and the second
  terminal row write when it is set, and still returns FAILED carrying the store's error.
- **Why the distinction matters:** keying the skip on `terminal` instead broke M9's R2 sweep
  (`test_a_failure_at_any_point_after_the_model_answers_loses_no_billed_call`), where a fault
  injected at the emit of `RunCompleted` must still finish the row. That test is what caught it.
- **`PostgresRunStore.finish_run` is split:** the status row is written and committed first, then
  the budget and price columns are written in their own statements inside a guard. A value the
  database cannot hold now costs the manifest its budget and never the run its status.
- **Tests:** 2 added (87 to 89), both seen failing first: a failing terminal write is reported with
  exactly one terminal event and exactly one write attempt; and a budget column that cannot be
  written still leaves a terminal row.

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

- `agentsdk/budget.py`, `agentsdk/prices.py`, `agentsdk/prices.json`,
  `agentsdk/migrations/0008_budget.sql`.
- `agentsdk/api.py`: `RunConfig.budget_lease`, `_effective_pricing`, `_budget_record`,
  `_spend_record`/`_spend_fields`, `cost_of`, and every terminal path.
- `agentsdk/handle.py`: `RunControl.terminal_written`.
- `agentsdk/loop.py`: `RunMeter.budget_tokens` and `price_table_date`; the lease asked before each
  call, charged after each, and on cancellation.
- `agentsdk/manifest.py`, `agentsdk/postgres.py`, `agentsdk/persistence.py`, `agentsdk/errors.py`,
  `agentsdk/__init__.py`, `pyproject.toml`, `README.md`.
- `scripts/15_budget.py`; `tests/test_distribution.py`; `tests/test_budget.py` (89 tests).

## What the author ran for round 5

1. **Reproduced J1 first** with the reviewer's probe, then wrote the two tests.
2. **The first fix was wrong and the suite said so:** wrapping the completion path failed M4's
   contract test; the second attempt, keyed on `terminal`, failed M9's R2 sweep. Both are recorded
   in DECISION-2df048ae.
3. **Gate file:** 89 passed. **Full suite:** 1464 passed.
4. **Demo command:** 8 of 8 offline and live.
5. **Mutation run:** **48 of 50 killed, 2 declared equivalent**, with a mutant for every finding of
   all four rounds, including four for J1 alone.
6. **Gates.** Both green on source hash `d286b198`, the hash KICKOFF.md records, from one
   `genesis gate` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_budget.py -q`, exit 0, 89 passed in 13.35s (12:12:04 to 12:12:18 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1464 passed in 234.34s (0:03:54)
     (12:12:19 to 12:16:14 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts under

`C:\Users\<user>\AppData\Local\Temp\claude\<session-folder>\7dae5546-45c4-45ef-a67e-cb3e638507df\scratchpad\m17r5-probes\`

with shared cleanup in one `common.py`, and tenant ids starting `SYN-m17r5`. Earlier rounds'
probes are in `m17r1-probes\` through `m17r4-probes\`.

1. **The terminal path, from every direction.** Fail the store at each seam, on each ending:
   completed, failed, max-turns, cancelled, budget-refused. For each, how many terminal events
   exist, what `runs.status` says, what the result says, and how many write attempts were made. Is
   there any combination where the row and the result disagree without the caller being told?
2. **`terminal` versus `terminal_written`.** Is the new flag set exactly when a terminal event was
   written, on every path, including cancellation and the handle API?
3. **Every number that reaches a row**, in both units, on every ending. Four rounds found one each.
4. **The arithmetic**, the shrink on release, extreme ceilings, and whether any allocation can
   stop summing to the ceiling.
5. **Enforcement and concurrency**, including clamped negative reports over many calls.
6. **Pricing**: precedence, the call-site refusal, the hook path, `prices.json` against its page.
7. **The manifest and 0008**, **NFR-23 and AC-44** on both stores, and **the demo command**
   checked against the stores.

## Declared limitations: known, recorded, NOT findings

- **Two equivalent mutants**, both redundant guards kept as defence: B13 (`released` in
  `may_call`, unreachable since the shrink on release) and N16 (`BudgetAmount` refusing negative
  tokens, unconstructible since `tokens_of` clamps).
- **A store that fails the status write leaves the row as it was**, and the caller is told through
  a FAILED result carrying the error. That is M4's contract, not an M17 choice.
- **F7:** a `before_model` hook can send a model the call-site check never saw; the call proceeds,
  its cost is unknown, the next call is refused. Pinned by a test.
- **F8:** the cited page lists no cache-write price, so that class is unpriced rather than 0.
- **Three manifest rows in the development database predate round 1's fix.**
- **Nothing drives plan nodes or child runs yet** (DECISION-c274eb02).
- **The governor lives in one process**; **`release()` is called by the orchestrator**.
- **The table holds one model**; Haiku's Bedrock rate is unverified (KNOWLEDGE-b40dd9de).
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

