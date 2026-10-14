You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Sonnet 5** if Fable 5.1 fails
to start. M32 was implemented by Opus 5.5; Opus 5 reviewed round 1. Bring no memory of an earlier
review into this one: if your session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md rather than inheriting the framing below: "#### M32"
(FR-120, FR-121), AC-88, NFR-30, NFR-31, NFR-22 and NFR-33, FR-104, FR-118. Read P4-D3 (the exit
signal certifies increment 1), KNOWLEDGE-e504b980 (the round 1 review, findings C1 to C8) and
DECISION-deac9a26 (the owner's decision to repair them all before the increment closes, and to add
a session guard).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: without the venv first on PATH, graphizer hits the Microsoft Store
python alias and crashes Node. **Never pass `--help` to a genesis command:** it is read as an
argument and the command runs (it requeued this task once).

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.** Pass every DSN
  through `agentsdk.config.normalise_database_url`.
- **Host:** Windows with Developer Mode on (D7), Python 3.11.

**Running tests**
- **Only one test session at a time.** New this round: `tests/conftest.py` holds an advisory lock
  for the whole session, and a second session started from another shell stops at once with exit
  code 3, "another test session is running". If you see that, wait; do not work around it. A
  session started by a test of the running one inherits `AGENTSDK_TEST_SESSION` and is let in.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite takes about 8 to 10 minutes; the unit gate about 75 seconds.**
- **Do not kill a gate to test staleness:** round 1's reviewer did, which killed a session
  mid-suite and left 49 runs behind.

**Gates (DECISION-066e317a).** Run `genesis gate . M32-approval-contention --timeout 1500000` in
the background. Then check for a stray python process. If a gate reads `stale` straight after, run
the gate file once with the cache on, then gate again. Writing any file under the repository while
a gate runs can stale it.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Never
`git stash` (KNOWLEDGE-ae83f412). Do not mutate an applied migration (0014, 0015). Run each mutant
with a timeout and kill the process tree.

**The FR-81 fixture is active in every test.** **Cleanup order:** `interruption_decisions`, then
`interruptions` and `standing_decisions`, then the run's other rows, then the runs, children first,
by run id; never by label.

## Task under review

**M32-approval-contention**, **round 2**, risk **high**. HEAD is still `c2437ee` (M31); round 1's
work and this round's are both uncommitted. Round 1 found no behavioural defect; it found the exit
signal could pass without testing what it claims. This round repairs that.

### What round 2 changed

**`scripts/26_approval_contention.py`, the exit signal:**
- **C1:** the contention check requires the race: outcomes `["recorded", "refused"]`, on the c.txt
  interruption, whose decision is by one of the two approvers. `not race or` is gone.
- **C2:** base.txt's second edit replaces `"1, two"`, text that exists only after the first edit.
  A new writer, e.txt, makes two `write_file` calls in one turn: run in issue order, the first
  lands and the second is refused "changed since approval", so the file shows which ran first. The
  approval screen now decides pending calls in a random order (seeded).
- **C3:** each write tool is wrapped (`counted`) to log every execution as it starts: its
  interruption id, and whether that interruption was still pending in the store at that moment.
  The checks count executions per interruption (exactly one), require an approve decision for each
  and none undecided, and require each writer's executions in its issue order.
- **C6:** every decision lookup is guarded, so a view with no decision is a `[FAIL]`, not an
  exception.

**`tests/test_approval_contention.py`** (the unit gate, 20 tests, from 17):
- **C4:** `until(handle, wanted, timeout=15)` replaces each unbounded `async for handle.events()`,
  so a missing event fails rather than hangs.
- **C5:** the child's own stream (held from `runner._live` while the child waits) must carry both
  approval events with `task_id` the node id.
- **C7:** the critic test now runs a gated node whose critic model asks for `write_file` before its
  verdict, with `approval_timeout=1`; no interruption is recorded and nothing is written.
- **C8:** a new test: writing f.txt leaves the stale temporary file of a target named
  `f.txt.agentsdk-<uuid>.tmp`.
- **New:** the exit signal fails, on exactly the contention label, when the race is replaced by one
  decision; a second pytest session started outside this one is refused with exit code 3.
- A dead line in the in-flight test is removed.

**`agentsdk/builtin_tools.py`:** **C8:** `_sweep` matches its own target's exact shape,
`.<name>.agentsdk-<uuid>.tmp`, with `fullmatch`; `_UUID` is factored out of `_TEMPORARY`.

**`tests/conftest.py`:** `one_test_session_at_a_time`, a session fixture taking
`pg_try_advisory_lock(20261014, 32)` on its own connection (objid 32, apart from the claim locks'
objid 4 that other tests count). `the_store_is_left_as_it_was_found` now depends on it.

### Edits to approved tests

- **`tests/conftest.py`:** the session guard above (owner's decision, DECISION-deac9a26). Check
  that AC-65's nested session in `tests/test_validation_and_guard.py`, which copies this conftest
  unchanged, still runs.
- **`tests/test_approval_contention.py`:** approved in round 1; every change above.
- **No other approved test is changed.** Round 1's listed edits (`test_write_tools.py`,
  `test_distribution.py`, `scripts/25`) stand.

## What changed

```bash
git diff c2437ee -- agentsdk tests scripts README.md
git status --short
```

Round 2's own changes are in `scripts/26_approval_contention.py`, `tests/test_approval_contention.py`,
`tests/conftest.py` and `_sweep` in `agentsdk/builtin_tools.py`.

## What the author ran

1. **Mutation run: 9 of 9 killed, each by a named assertion,** files restored by SHA-256:
   - **C1, no race:** the contention label fails.
   - **C2, a turn's calls run in reverse:** the issue-order, folder and e.txt labels fail.
   - **C3, an approved call runs twice** (the executor calls the tool again): the ran-once label.
   - **NFR-30, the turn stops waiting after its first decision:** the ran-once and decision labels.
   - **C4, no top-level emit:** fails in 17 s on `until`'s bound.
   - **C5, no node id on the child's stream;** **C8, the prefix sweep;** **the guard off:** each
     fails its test.
   - **C7, the critic given the write tool:** first killed by a hang; the test then gained
     `approval_timeout=1`, and it fails on the interruption assertion in 1.7 s.
2. **Example 26 offline:** all 10 checks pass. **Live,** after the gates: all 6 live checks pass.
   The live model put e.txt's two writes in separate turns, so both landed (the folder check is
   offline only), and it repeated a.txt and d.txt under new call ids: each call was approved and
   ran once. Its rows were removed by run id: 8 runs, 11 interruptions, 11 decisions, 1 standing
   decision; the store is back to 10351 runs, 19 in example-tenant.
3. **Gates,** both green on source hash `d5dc57af`: `unit` 20 passed in 74.45s; `regression` 2168
   passed in 438.53s (started 07:48:26 UTC), from 2165.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m32-r2-probes` in your own scratchpad, with tenant ids starting
`SYN-m32r2`. Delete only rows your probes wrote, by run id.

1. **Is any FR-121 bullet still vacuous?** For each check in the signal, build the failure it names
   and confirm it reports `[FAIL]` on that label. In particular: a call run before its decision; a
   call run twice; calls of a turn reordered; no simultaneous decision; a rejection message lost;
   a decision in another tenancy; files differing from what was approved.
2. **Does the signal share anything with what it checks?** Round 1's recurring pattern
   (KNOWLEDGE-e504b980): a check written against the thing it checks. `counted` reads the store's
   pending listing to judge "undecided"; the issue order comes from the child's history; e.txt's
   order rests on the write tool's own "changed since approval". Can any of these agree with a
   wrong execution?
3. **The `counted` wrapper:** does wrapping change what it measures (the `ApprovedCall` injection by
   annotation, the store read inside a tool call, FR-81)?
4. **C4, C5, C7:** do the new bounds and assertions fail fast for the faults named, and does
   `approval_timeout=1` hide anything in the critic test?
5. **C8:** can the sweep now miss its own orphan, or still reach another target's?
6. **The session guard:** does a second session from another shell stop before it touches the
   store? Is the lock always released (a crash, Ctrl+C, `pytest.exit`)? Can the inherited variable
   leak into a session that should be refused? Does AC-65's nested session still run?
7. **Regressions:** the full suite, and example 26 live if you can afford it.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M31, and in round 1's prompt** (`M32-review-prompt.md`).
- **Two decisions made one after the other** give the same `["recorded", "refused"]` as two made at
  once; the barrier makes them simultaneous, and the arbitration is also pinned by a six-way test
  in the regression gate.
- **The guard covers pytest sessions only,** not scripts run by hand against the database.

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
  control approve . M32-approval-contention --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M32-approval-contention --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.
