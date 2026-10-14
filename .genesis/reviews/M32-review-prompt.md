You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Fable 5.1** if Opus 5 fails to
start. M32 was implemented by Opus 5.5. Earlier reviews: Sonnet 5 approved M31, Fable 5.1 approved
M30, and Opus 5 approved M29. Bring no memory of an earlier review into this one: if your session
holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md rather than inheriting the framing below:
- "#### M32": FR-120 and FR-121;
- AC-88;
- NFR-30, NFR-31, NFR-22 and NFR-33, which the task lists;
- FR-90 (the ledger run id a pool sets on every child) and FR-104 (critic runs have no tools);
- FR-118, which the pre-flight repairs.

Read the decision table P4-D1 to P4-D15, especially P4-D3 (the exit signal), P4-D8 (waiting) and
P4-D11 (orchestration), and DECISION-d9ddaf0f (the spec-level choices accepted). Read
KNOWLEDGE-86b899ad (M31's review), whose four follow-ups this task's pre-flight settles, and
KNOWLEDGE-14f3ec8b (repetitions with shuffled starts).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-4`.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.** Pass every DSN
  through `agentsdk.config.normalise_database_url`. A raw `postgresql+psycopg://` DSN makes
  psycopg echo it in an error.
- **Host:** Windows with Developer Mode on (D7), Python 3.11.

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
  M31's reviewer overlapped two mutation runs and left a mutant line in the source: do not.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite takes about 10 minutes; the unit gate about 70 seconds** (the exit signal runs
  ten times on both stores).

**Gates (DECISION-066e317a).** Run `genesis gate . M32-approval-contention --timeout 1500000` in
the background, or detached. Then check for a stray python process. If a gate reads `stale`
straight after, run the gate file once with the cache on, then gate again. Writing any file under
the repository while a gate runs can stale it.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). Do not mutate an applied
migration file (0014 and 0015 are applied). A mutant that makes the run wait forever hangs the
test: run each with a timeout and kill the process tree.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

**Cleanup order:** delete `interruption_decisions`, then `interruptions` and
`standing_decisions`, then the run's other rows, then the runs, children first, by run id. Never
delete by label or agent spec id. Probe folders on disk are yours to remove; never touch a path
outside your own temporary folders.

## Task under review

**M32-approval-contention**, **round 1**, risk **high**. HEAD is `c2437ee` (M31). This is the last
milestone of Phase 4 increment 1, and it carries the exit signal (P4-D3).

### Pre-flight: M31's follow-ups (KNOWLEDGE-86b899ad)

1. **The quadratic edit.** `_edited` now finds the first occurrence with `str.find` and asks
   whether another starts after it (overlapping included), in linear time. `old` is capped at
   `_MAX_OLD` = 10000 characters, in the code and in the schema (`maxLength`). The refusal now
   reads "occurs more than once" rather than a count.
2. **Hidden names and orphans.**
   - Only a temporary file of a write **in flight in this process** (`_IN_FLIGHT`, under a lock) is
     withheld from list, glob and grep. A file merely named like one is shown.
   - A target whose final name has the temporary shape is refused (`_RESERVED_NAME`).
   - Before each write, `_Target._sweep` deletes that target's orphans older than
     `_ORPHAN_SECONDS` = 60, read from the directory listing's last-write time, so another
     process's write in flight is not removed.
3. **The README** now says any open reader can block a replace, with M31's 43 of 60.
4. **The sticky early wake** is pinned by a timing bound: `Runner.decide` must wake a waiting run
   well inside the five-second re-read, sticky or not. The non-bool predicate is now killed by an
   assertion, not a hang.

### M32 (FR-120, FR-121)

- **`agentsdk/api.py`:**
  - `Runner._live`: run id to (event sink, control, config) for every run under way in this
    process, set in `_drive` around the new `_driven`, removed in a `finally`.
  - `_top_emitter(run_id, top_run_id, control)`: for a run beneath another, emits on the
    top-level run's stream; `None` for a top-level run.
  - `_approval_timeout_of(run_id)`.
- **`agentsdk/loop.py`:** `emit_top`, and `_emit_approval`, which emits `ApprovalRequested` and
  `ApprovalDecided` with the node id as `task_id` on the run's own stream and, for a child, on the
  top-level stream.
- **`agentsdk/subagents.py`:** a child inherits its parent's `approval_timeout` when the parent is
  live in this process.
- **Already in place from M29 and M30, now exercised:** `Runner.interruptions(run_id=<top>)` lists
  recursively beneath a run on both stores; the handle's `_decide` refuses an interruption whose
  top-level run is not its own; cancelling decides every pending interruption `cancel`.
- **`scripts/26_approval_contention.py`** (FR-121): offline on both stores and live.
- **`tests/test_approval_contention.py`** (the unit gate): 17 tests.

### Edits to approved tests

- **`tests/test_write_tools.py`:** the test that every temporary-shaped name is withheld became
  `test_list_glob_and_grep_show_a_file_merely_named_like_a_temporary_one`, which asserts the
  lookalike is shown. This reverses M31's behaviour on purpose (follow-up 2).
- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `26_approval_contention.py`.
- **`scripts/25_write_tools.py`:** one live check's text changes from "occurs 2 times" to "occurs
  more than once" (follow-up 1).
- **No other approved test is changed.**

## What changed

```bash
git diff c2437ee -- agentsdk tests scripts README.md
git status --short
```

## What the author ran

1. **Tests first.** `tests/test_approval_contention.py` was written before the code. Example 24's
   M30 races (an interruption listed before its wait was registered; a standing decision applied
   after recording) stay fixed.
2. **Mutation run: 11 of 11 killed,** each file restored by SHA-256. Two first survived, both test
   gaps:
   - **"Search: quadratic again":** the near miss sat at the end of `old`, and Python compares
     first and last characters before the rest, so the quadratic scan stayed fast. The near miss
     now sits in the middle of `old`, against a 3 MB file.
   - **"A write in flight not hidden":** the test looked while the temporary file was still open,
     when the listing could not open it anyway. It now looks at the moment of the replace.
   - Two mutants (no top-level emit; runs never registered) were killed by the run hanging past
     240 s, not by an assertion.
3. **Full suite before the gates:** 2165 passed.
4. **Example 26 live,** on the gateway and PostgreSQL: all 5 live checks passed, including two
   threads deciding at the same moment with exactly one decision standing. The live planner went
   through 4 plan versions. Its rows were removed by run id: 10 runs, 21 interruptions, 21
   decisions, 1 standing decision.
5. **Gates,** both green on source hash `fa7fc52d`, run in the background with `--timeout
   1500000`. The first run's unit gate read stale from `.pytest_cache` churn with no code change;
   the rerun landed both on one hash.
   - `unit`: 17 passed in 71.04s.
   - `regression`: 2165 passed in 520.19s (started 05:59:57 UTC). The baseline was 2148.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m32-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m32r1`. Delete only rows your probes wrote, by run id, interruptions first, and only
folders you created.

1. **The exit signal (FR-121, AC-88).** Does the test check every bullet of FR-121 against the
   store, on both stores, ten times? Can any be satisfied vacuously (no rejection, no expiry, no
   contention actually happening)? Break each property in the code and see whether it is caught.
2. **Calls run at most once, only after approve, in issue order.** Look for a call that runs
   twice, runs undecided, runs after reject or expiry, or runs out of its child's order, under
   cancellation, expiry and a decision racing the expiry.
3. **The top-level stream.** Are both events emitted on the child's stream and the top-level
   stream, with the node id, at every depth? Can `_live` leak an entry (an exception in `_drive`,
   cancellation before `_driven`), or emit on a finished run's sink? Does a child run outside a
   plan, or a run whose top-level run lives in another `Runner`, behave sanely?
4. **Deciding from the top.** Can the top-level handle decide an interruption of another plan, or
   another tenant? Can a child's handle decide its parent's? Does the recursive listing stop at the
   tenancy boundary on both stores?
5. **Waiting in a plan (P4-D8).** Do waiting children hold their reservation and slot, and does a
   plan whose waiting children fill every slot admit nothing new until one is decided? Does the
   plan settle only when every node has? Is an expiry a rejection the child sees, never a node
   failure?
6. **Cancellation (NFR-22).** Cancel the top-level run with interruptions pending at several
   depths: is every one decided `cancel`, every child cancelled, and nothing run afterwards?
7. **The pre-flight.**
   - Is the uniqueness check right for overlapping, adjacent and boundary occurrences, and linear
     for any input the schema admits?
   - Can a model still hide a file from the read tools, or get one swept that it should not?
     Can the sweep delete anything but this target's own stale temporary files, or anything
     outside the root? Is the in-flight set safe across threads and failures?
   - Does the timing bound pin the sticky early wake?
8. **Critic runs (FR-104)** never record an interruption; **NFR-33:** no new runtime dependency,
   and every Win32 call stays inside `_FileSystem`.
9. **The approved-test edits** listed above.
10. **Example 26 live,** if you can afford it, and **regressions:** the full suite, and the gate
    files of M29 to M31.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M31.**
- **A paused run does not resume after its process restarts** (Phase 6). `_live` is per process:
  a child whose top-level run is in another process emits on its own stream only.
- **Waiting children can fill every slot** and stall the plan's admission until one is decided;
  stated in FR-120.
- **The sweep** removes only orphans older than 60 seconds, and only when their target is next
  written; an orphan of a file never written again stays.
- **Windows only** for the write tools (D11).

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

Then report: what you checked, what you ran, the result for each of the ten attack items, and
your verdict.
