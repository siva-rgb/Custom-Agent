You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Fable 5.1** if Sonnet 5 fails
to start. M27 was implemented by Opus 5.5. Earlier reviews: Opus 5 approved M26a, Fable 5.1
approved M26, and Sonnet 5 approved M25a. Bring no memory of an earlier review into this one: if
your session holds any, say so and ask for a fresh one. Re-derive the requirements from SPEC.md
("#### M27": FR-104 to FR-106, and AC-79; and NFR-25, NFR-26, NFR-28 and NFR-29, which the task
lists) rather than inheriting the framing below. Read the increment 2 decision table and
DECISION-2cec3b61, the spec-level choices the owner accepted.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.** Pass every DSN
  through `agentsdk.config.normalise_database_url`. A raw `postgresql+psycopg://` DSN makes
  psycopg echo it in an error.
- **Host:** Windows Developer Mode must be on (D7). Python 3.11.

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite takes about 7 minutes.**

**Gates (changed at M27, DECISION-066e317a).** Run `genesis gate . M27-critic --timeout 1500000`
in the background, or detached, since it outlasts a 10-minute foreground call. Then check for a
stray python process. If a gate reads `stale` straight after, the first run churned
.pytest_cache: run the gate file once with the cache on, then gate again.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). Do not mutate an applied
migration file, because its checksum is recorded in the database.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

## Task under review

**M27-critic**, **round 1**, risk **high**. HEAD is `1c21fc5` (M26a). The gate built in M26 now
calls the critic when its signals fire. Pre-flight (KNOWLEDGE-462abb1d, DECISION-066e317a) also
settled M26a's six follow-ups and moved gates to the background.

### What it builds (all in `agentsdk/orchestrator.py`, apart from the event)

- **`_gated`** returns the node's answer, its label, and the artifact already holding it. A fired
  gate calls **`_criticised`**, which:
  1. **Checks the budget.** The node's lease must be able to pay for a call
     (`lease.may_call()`), or the node fails `budget_exceeded`, unreplanned, with no critic run.
  2. **Stores the briefing.** The answer goes in an artifact with the child's label. A JSON
     rendering of the node's claims and the fired signals goes in one labelled `from_model` over
     the answer and every claim. Every cited source version is read through the evidence store.
  3. **Bounds the briefing.** Over `max_briefing_chars`, the node fails
     `critic_failed: briefing_too_large`, with no critic run.
  4. **Spawns the critic** through the run's `SubagentPool`:
     - at depth 1, on the node's own lease, with node id `<node>#critic`;
     - briefed with the answer, the rendering and the pages as data inputs;
     - answering a verdict schema built per node (`_verdict_schema`), with enums of the node's
       claim uris and the briefed source uris, and `if`/`then` so that only `corrected` needs a
       non-empty answer.

     A refused spawn fails the node `critic_failed: spawn_refused: ...`.
  5. **Labels the result.** The critic's result label joins the node's labels whatever happens
     next. A failed critic fails the node `critic_failed: <error>`, or `budget_exceeded`
     unprefixed.
  6. **Applies the verdict.**
     - `approved`: the node is done, reusing the stored answer artifact.
     - `escalate`: the node fails `critic_escalated: <reason>`.
     - `corrected`: the corrected answer is checked against the node's `expected_output_schema`
       first. Then each correction is applied through the ledger store's `supersede`, as a
       contribution of the critic run, node `<node>#critic` and the critic's agent, with no
       confidence and the critic's label, stopping at the first refusal. Each applied one emits
       `ClaimSuperseded`. The node takes the corrected answer, labelled
       `answer.taking_in(critic)`.
  7. **Records the verdict last:** `critic` verifications on the node and each of its claims,
     naming what was applied and what was refused, then `CriticVerdict`.
- **`agentsdk/events.py`:** `CRITIC_VERDICT`.
- **`scripts/22_critic.py`** (FR-106), offline and live.

### M26a's follow-ups, settled here

1. The `Runner.record_validation` docstring no longer claims no tool reaches the Runner.
2. Renamed: `test_record_validation_is_reachable_from_no_tool` →
   `test_record_validation_is_a_runner_method_no_tool_or_ledger_reader_offers`.
3. AC-83 and FR-110 now name the tools the test runs, and the static check that covers every
   shipped tool.
4. M26's signal oracle now encodes the amended `low_confidence` rule. Its generator now reaches a
   claim only a critic vouched for.
5. The case where `0013` cannot apply over a pre-existing cross-ledger row is stated in the README
   and FR-110, not in `0013`, whose checksum is fixed.
6. The scope walk's docstring is bounded to one level.

### Edits to approved tests (each to judge)

- **`tests/test_verifications.py`:**
  - the rename (2) and the oracle (4);
  - `test_a_signal_that_fires_records_critic_required_and_fails_the_node_until_m27`, renamed
    `..._and_calls_the_critic_once`;
  - `test_no_claims_fires_only_for_a_node_carrying_a_critic_criterion`.

  The last two pinned M26's interim behaviour (DECISION-4f57d8c5), which M27 replaces. Their
  file's scripted critic approves, so the nodes are now done, and the gate-row assertions are
  kept.
- **`tests/test_gate_caveats.py`:** the walk's docstring (6) only.
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `22_critic.py`.

## What changed

**Nothing since M26a is committed.** Review with:

```bash
git diff 1c21fc5 -- agentsdk tests scripts README.md SPEC.md
git status --short
```

The uncommitted `.genesis/` changes include the resume/requeue and plan re-sign after M26a,
DECISION-066e317a, and this task's gate evidence.

## What the author ran

1. **Tests first.** `tests/test_critic.py` was drafted before the critic existed. It now has 21
   tests, including example 22 offline on both stores.
   - Two test bugs were found on the way. The scripted critic matched verdict words anywhere in
     its message, and the critic's own instructions contain "approved", "corrected" and
     "escalate". The manifest key was `briefed_input`, not `briefed`.
2. **Mutation run: 20 of 20 killed**, each file restored by SHA-256. Three first survived, all
   test gaps:
   - "the critic runs on a spent reservation": the test counted model calls, not critic runs;
   - "a critic out of budget replans": no test drove a critic out of budget mid-run;
   - "a correction may cite any source": no test cited an unbriefed source.

   Each got a test, and each was then killed.
3. **Example 22 live**, against the gateway on PostgreSQL, twice. The first run failed, for two
   reasons:
   - The live readers batched the fetch with a claim citing it, so the citation was refused and
     they never retried. The live briefing now tells them to repeat a failed call.
   - The critic sent `"corrected_answer": ""` beside `approved`, and the schema refused it twice.
     **A real schema bug**, now fixed and tested on both sides.

   The example's own check was also wrong: it keyed on `CriticVerdict`, which a failed critic
   never emits. It is now keyed per execution (version, node).

   The second run passed all four live checks. The real critic approved one node, corrected
   another, escalated the third, and the replan was judged afresh. Both runs' rows were removed
   by run id.
4. **Gates.** Both green on source hash `a23d4e3b`, run in the background with `--timeout
   1500000`. The first run went stale between unit and regression from cache churn, so it was
   rerun.
   - `unit`: 21 passed in 4.78s.
   - `regression`: 2010 passed in 392.75s (13:17:14 to 13:23:49 UTC). The baseline before M27
     was 1989.
5. **Store:** at baseline. 0 advisory locks.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m27-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m27r1`; delete only rows your probes wrote, by the run ids they recorded.

1. **Never cleaner (NFR-28).**
   - Can any path give a node output, a superseding claim, a verification or the orchestrator's
     report a cleaner label than the answer, the claims, the pages or the critic it rests on?
     Include `approved` reusing the answer artifact, and a critic that fails.
   - Is the rendering's label right?
2. **At most once per execution.** Can the critic run twice for one node execution, or run for a
   cleared one? Consider replans, re-asks, concurrent nodes sharing a claim, cancellation during
   the critic, and a store failure after the critic.
3. **The budget.**
   - Is the reservation held exactly as FR-102 and FR-104 state?
   - Is `may_call()` the right check?
   - Can the critic or the node overspend?
   - Does `budget_exceeded` never replan, and does `critic_failed` always replan as a child
     failure does?
4. **The verdict path.**
   - The schema against what the ledger can record: statement and reason lengths, 50 sources,
     enums.
   - A correction to a claim another node's critic superseded first. Does the rerun see the
     ledger as it then stands?
   - What remains recorded after a partial correction: is the verification's
     `applied`/`refused` honest?
   - Can a critic's `paraphrases` or `corrections` name anything outside the node's claims?
5. **Records and events.** Do the `critic` verifications, `CriticVerdict`, `ClaimSuperseded` and
   the node's status always agree? Check both stores, and the order (verifications last).
6. **The approved-test edits listed above.** Is each necessary, and does none weaken what it
   checked?
7. **NFR-29.** An ungated plan, and a gated plan whose signals are clean, unchanged since M26.
8. **Example 22 live,** if you can afford the gateway calls. Clean up by run id.
9. **Regressions:** the full suite, and the gate files of M22 to M26a.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M26a:** W1, C6, B2, a waited fetch that raises recording no
  wait, `KeyFailed`'s context and pickling, M25a's untested M4 branch, the claim set and
  `gate_all_nodes` rows, the custom-tool reach, and `0013` over a pre-existing cross-ledger row.
- **Live verdicts are the model's own.** Example 22 live checks only what must hold whatever the
  critic decides.
- **M28 is not built.**

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
  control approve . M27-critic --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M27-critic --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the nine attack items, and
your verdict.
