You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M26 was implemented by Opus 5.5. Earlier reviews: Sonnet 5 approved M25a, Opus 5
approved M25, and Fable 5.1 approved M24a. Bring no memory of an earlier review into this one: if
your session holds any, say so and ask for a fresh one. Re-derive the requirements from SPEC.md
("#### M26": FR-100 to FR-103, and AC-78; and NFR-25 to NFR-29, which the task also lists)
rather than inheriting the framing below. Read the increment 2 decision table at the top of
"### Phase 3, increment 2".

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Host:** Windows Developer Mode must be on (D7). Python 3.11.

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite now takes about 7m15s.** The gate's limit is 9m50s, so keep the machine
  otherwise idle while it runs. Split your own full-suite runs into halves under 590 s each.

**Gates:** `genesis gate` kills a gate after 120 s unless you pass `--timeout 590000`, and on this
host that kills only the venv launcher (KNOWLEDGE-64fa6e18). Run
`genesis gate . M26-verifications-and-signals --timeout 590000`, then check for a stray python
process.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412).

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M26-verifications-and-signals**, **round 1**, risk medium. It is the second milestone of
Phase 3's increment 2, after M25 and M25a (committed at `c35a319`).

**Pre-flight decision (DECISION-4f57d8c5).** The critic arrives with M27. Until then a gated node
whose signals fire:
- records its `critic_required` verification;
- emits `GateEvaluated`;
- fails `criterion_not_available: critic; the critic arrives with M27 (signals: ...)`.

**Interpretation stated for you to judge.** A node subject belongs to a ledger when two things
hold:
- its plan node is stored for the subject run in the row's tenant and project;
- that run is the ledger's run, or descends from it by `parent_run_id`.

Postgres reads `runs`. The in-memory store asks the Runner, which now records each run's parent.

### What it builds

- **`agentsdk/gate.py` (new):**
  - `EvidenceVerification` and `NodeSubject`, validated by field name;
  - `GatePolicy`, whose critic must have no tools, and which refuses gating every node without a
    critic;
  - `signals_of`, the five rules, with the critic's contributions (node id ending `#critic`)
    excluded from `low_confidence`.
- **`agentsdk/ledger.py`:**
  - the `LedgerStore` protocol and `InMemoryLedgerStore` gain `append_verifications`,
    `verifications` and `verifications_since`;
  - the in-memory store takes `nodes=`, the Runner's check;
  - `EvidenceLedger.signals(claim, policy)`.
- **`agentsdk/postgres.py`:** the same three methods on `PostgresLedgerStore`, with the subject
  checked in the transaction. The manifest insert writes `gate_policy`.
- **`agentsdk/migrations/0012_verifications.sql` (new):** the `verifications` table, with the
  subject either a claim or a node, kind/outcome pairs checked, and foreign keys to `claims`,
  `runs` and `plan_node_states`. Also `execution_manifests.gate_policy`.
- **`agentsdk/manifest.py`:** `agent_spec_hash()` is factored out, so the policy can name its
  critic by spec hash. `build_manifest` takes `gate_policy`.
- **`agentsdk/api.py`:** `RunConfig.gate_policy`, the Runner's `_parents` and `_node_of_ledger`,
  `Runner.record_validation` (async), and the manifest wiring.
- **`agentsdk/orchestrator.py`:**
  - `Orchestrator(gate=)`, and `config()` sets `gate_policy`;
  - the critic is checked at submission, as roles are;
  - `_criteria` runs every deterministic criterion first, in plan order, then refuses a critic
    criterion that is not gated, as before;
  - `_gated` is the gate;
  - a gated node's reservation is held until the gate ends, through `_attempt` and
    `_attempted`; an ungated node's is released when its child ends, as before.
- **`agentsdk/events.py`:** `GATE_EVALUATED`.
- **`agentsdk/__init__.py`:** exports the two types.
- **`README.md`:** a feature bullet; the orchestration limit row; a declared row for the claim set
  and for a node gated only by `gate_all_nodes` that recorded no claim.

**Edits to approved tests:** one. `tests/conftest.py`'s `_TRACKED` gains
`("verifications", "verification_id")`, which NFR-29 lists.

**A behaviour change to check against FR-74 and NFR-29.** Before M26, a `critic` criterion
standing before a failing deterministic criterion ended the node `criterion_not_available`. Now
the deterministic failure wins, as FR-102 requires. A plan with no critic criterion is
unchanged.

## What changed

**Nothing since M25a is committed.** HEAD is `c35a319`. Review with:

```bash
git diff c35a319 -- agentsdk tests README.md
git status --short
```

The uncommitted `.genesis/` changes include the CLI resume/requeue of M26–M28 and the plan
re-sign after M25a (DECISION-4cf55a17), and DECISION-4f57d8c5.

## What the author ran

1. **Tests first:** `tests/test_verifications.py`, 58 tests, written before the implementation,
   so all fail against M25a's code by import.
2. **Mutation run: 27 of 27 killed**, each file restored by SHA-256. The mutants cover:
   - every signal rule, its edges, the critic exclusion and the policy filter;
   - the policy's critic rules;
   - the record's kind, outcome and subject rules;
   - each store's subject check, kinds filter and tenancy;
   - the Runner's ancestry check, and the manifest on both layers;
   - the claim set, `no_claims`, a fired signal passing the node, and the reservation released
     early;
   - criteria order, and the submission check.
3. **Neighbouring gate files:** orchestrator, ledger, claim-lock, M25, persistence, carried
   caveats, subagents, budget and plan: 521 passed.
4. **Gates.** Both green on source hash `f17a7f2e`, from one `genesis gate --timeout 590000` run.
   - `unit`: 58 passed in 8.33s.
   - `regression`: 1981 passed in 435.32s (06:50:41 to 06:57:58 UTC). The baseline before M26 was
     1923.
5. **Store:** at baseline. 0 advisory locks.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m26-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m26r1`; delete only rows your probes wrote, by the run ids they recorded.

1. **Tenancy and subjects (NFR-25).**
   - Can any verification be recorded, or read, across a tenant, project or ledger, on either
     store?
   - Is the node-subject rule right, and the same on both stores? Consider a child orchestrator,
     a replanned version, a node of a sibling top-level run, and a run with an explicit
     `ledger_run_id`.
   - Do the foreign keys and checks in `0012` hold when the store's checks are bypassed?
2. **Append-only and never cleaner (NFR-26, NFR-28).**
   - Is any verification updatable or deletable through the SDK?
   - Does any verification, of any kind, change a claim's derived provenance or a node's output?
   - Is the gate row's provenance at least as tainted as what it judged?
3. **The signals.**
   - Each rule against FR-102 exactly: the edges, `None` confidence, critic exclusion, and
     `unsupported` versus `thin_support`.
   - Is "this execution's child run" the right claim set under replanning and concurrency? Could
     a sibling's claim, or a claim a node contributed to after another recorded it, be judged
     wrongly?
4. **The gate in a plan.**
   - Ordering against deterministic criteria.
   - The reservation held exactly for gated nodes.
   - What happens on cancellation or a store failure during the gate.
   - Does `GateEvaluated` and the verification always agree?
   - Does a clean node really make no model call?
5. **`record_validation`.** Is it unreachable from any tool or `ToolScope`, as FR-103 says? Is its
   validation complete?
6. **NFR-29.** Does an ungated plan send the same requests and record the same events and rows as
   before M26, apart from the manifest column's NULL? Check the criteria-order change above.
7. **Migration `0012`.** Is it idempotent? Do all migration checksums match?
8. **Regressions:** the full suite, in halves.

## Declared limitations: known, recorded, NOT findings

- **The critic is not built** (M27). A gated node whose signals fire fails
  `criterion_not_available` (DECISION-4f57d8c5).
- **The claim set excludes runs a node's child spawns itself, and a node gated only by
  `gate_all_nodes` that recorded no claim is cleared** (FR-102, README).
- **Everything declared for M20 to M25a still stands:** W1, C6, B2, a waited fetch that raises
  recording no wait, `KeyFailed`'s forced `__suppress_context__` and its pickling, and M25a's
  untested M4 branch.
- **M27 and M28 are not built.**

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`.
- **Clean up** every run, row, temp folder, process and advisory lock you create, and say what you
  removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M26-verifications-and-signals --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M26-verifications-and-signals --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the eight attack items, and
your verdict.
