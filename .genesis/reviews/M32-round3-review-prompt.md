You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M32 was implemented by Opus 5.5; Opus 5 reviewed round 1 (approved, with findings C1 to C8)
and Fable 5.1 round 2 (rejected, F1). Bring no memory of an earlier review into this one: if your
session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md: "#### M32" (FR-120, FR-121), AC-88, NFR-30, NFR-31,
NFR-22, NFR-33. Read P4-D3 (the exit signal certifies Phase 4 increment 1), KNOWLEDGE-e504b980
(round 1), KNOWLEDGE-f9084878 (round 2's rejection) and DECISION-e75ad00a (the owner's scope for
this round).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass `--help` to a genesis command:** it is read as an argument
and the command runs.

- **Configuration:** DATABASE_URL, BASE_URL and MODEL_API_KEY from .env. **Never print credentials
  or a connection string;** pass every DSN through `agentsdk.config.normalise_database_url`.
- **Host:** Windows with Developer Mode on (D7), Python 3.11.

**Running tests**
- **One test session at a time.** `tests/conftest.py` holds an advisory lock for the session; a
  second session from another shell exits at once with code 3. If you see that, wait.
- Check that no other python or node process is running first. Run probes with
  `-p no:cacheprovider`. **Do not kill a gate mid-run.**
- **Prefer runtime mutation** (a monkeypatch in a subprocess or a pytest plugin), as round 2's
  reviewer did: it leaves no repository file changed. If you edit a file, restore it byte for byte
  and verify its SHA-256 in a `finally`; never `git stash`; never touch an applied migration.
- **The full suite takes about 10 minutes; the unit gate about 90 seconds.**

**Gates (DECISION-066e317a).** `genesis gate . M32-approval-contention --timeout 1500000` in the
background; then check for a stray python process. If a gate reads `stale` straight after, run the
gate file once with the cache on, then gate again.

**Cleanup order:** `interruption_decisions`, then `interruptions` and `standing_decisions`, then the
run's other rows, then the runs, children first, by run id; never by label.

## Task under review

**M32-approval-contention**, **round 3**, risk **high**. HEAD is `c2437ee` (M31); M32 is
uncommitted. Round 2's repairs C1 to C8 were confirmed by its reviewer; it rejected on F1: the
rejection bullet of the exit signal was `all()` over a possibly empty list, and the approval screen
swallowed the `InterruptionExpired` of a late reject, so a screen late for b.txt reported a clean
pass on both stores.

### What round 3 changed

**`scripts/26_approval_contention.py`, every bullet now has a witness:**
- **F1:** the rejection bullet requires a principal rejection **with a reason** (the c.txt race's
  losing reject has none, so `bool(rejections)` alone was satisfied without b.txt). Offline, the
  sources bullet also requires b.txt decided by a principal (`sources.get("B\n") == "principal"`).
- **The screen no longer swallows.** It records what it meant to decide (`intended`) and each
  decision of its that raised (`failed`). A new bullet requires every intended decision recorded as
  made: by a principal, or by a standing decision when the screen's own call was refused
  `InterruptionAlreadyDecided`.
- **Issue order**, found by the author in the same sweep: it also passed with no execution, or with
  no writer running more than one call; it now requires some writer with more than one.
- **Two module settings the tests change:** `UNDECIDED` (paths the screen never decides) and
  `DELAYED` (paths it decides after a delay, in their own task).

**`tests/test_approval_contention.py`** (24 tests, from 20): the meta-test is a table of four
faults, each asserting the exact list of failing labels: no race; b.txt never decided (the round 2
reviewer's probe); b.txt decided after it expired; no writer running twice. Also a test that the
sweep matches its target ignoring case, and that such a name is reserved in any case; and the guard
test no longer raises `TypeError` without DATABASE_URL.

**`agentsdk/builtin_tools.py`:** `_sweep`'s pattern and `_TEMPORARY` match ignoring case, as the
file system does; so a name the sweep would match is refused as a target in any case.

**`tests/conftest.py`:** a comment only: the variable is unchecked by design (declared).

### Edits to approved tests

- **`tests/test_approval_contention.py`** and **`tests/conftest.py`**, as above.
- Rounds 1 and 2's listed edits stand. No other approved test is changed.

## What the author ran

1. **Tests first:** the case test failed before the fix; the "b never decided" case failed on the
   first witness (`bool(rejections)`), which is how the reason-carrying witness was found.
2. **Mutation run: 7 of 7 killed by assertions,** files restored by SHA-256: each witness removed
   (rejection, offline b.txt, issue order), the screen accepting a decision recorded otherwise, the
   screen skipping delayed paths, and each case rule. Round 2's 9 mutants were killed before.
3. **Example 26 offline:** all 11 checks pass.
4. **Example 26 live, twice.** The first run failed one check, "the plan completed": a live writer
   kept repeating a.txt until `max_turns`, the planner replanned four times (writing f.txt and
   g.txt of its own invention) and the run failed at `max_replans 3`; the six approval checks all
   passed. The second run passed all 7. Rows of both removed by run id (12 runs and 29
   interruptions; 9 runs and 19 interruptions); the store is at 10351 runs, 19 in example-tenant,
   interruption tables 0.
5. **Gates,** both green on source hash `76f708c4`: `unit` 24 passed in 87.97s; `regression` 2172
   passed in 566.87s (started 11:22:40 UTC), from 2168.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Probes in `m32-r3-probes` in your own scratchpad; tenant ids starting `SYN-m32r3`.

1. **Any bullet still without a witness?** Go through every check of example 26, offline and live,
   and for each build the absence of the thing it checks. Each must report `[FAIL]` on its label.
2. **The screen bullet:** can a decision that never landed, landed differently, or landed by the
   wrong source still pass? Is the standing allowance too wide?
3. **The witnesses themselves:** could one be satisfied by something other than what it witnesses,
   as the race's reasonless reject satisfied `bool(rejections)`?
4. **The case rules:** can the sweep still miss its own orphan, reach another target's, or refuse a
   name it should allow?
5. **Live:** run example 26 live if you can afford it. A live model leaving its script (round 3's
   first live run) is not an approval defect; an approval check failing is.
6. **Regressions:** the full suite.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M31 and in rounds 1 and 2** (the earlier prompts in
  `.genesis/reviews/`), including: `counted` defines "undecided" as membership in the pending
  listing; duplicate decisions pinned only through the race label; `AGENTSDK_TEST_SESSION` admits any
  session that sets it.
- **Example 23's live lost-race and judged-once bullets** pass when no race occurs; recorded for the
  next phase's pre-flight (DECISION-e75ad00a).
- **A live run can fail "the plan completed"** when the model leaves its script and exhausts the
  replans.

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
  control approve . M32-approval-contention --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M32-approval-contention --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the six attack items, and your
verdict.
