You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Sonnet 5, falling back to Opus 5** if Sonnet 5 fails to
start. M31 was implemented by Opus 5.5. Earlier reviews: Fable 5.1 approved M30, Opus 5 approved
M29, and Fable 5.1 approved M28. Bring no memory of an earlier review into this one: if your
session holds any, say so and ask for a fresh one.

Re-derive the requirements from SPEC.md rather than inheriting the framing below:
- "#### M31": FR-118 and FR-119;
- AC-87;
- NFR-30, NFR-33 and NFR-14, which the task lists;
- FR-36 and FR-45 (M10's confinement and pool), which the write tools build on;
- FR-113 to FR-115, which the pre-flight repairs.

Read the decision table P4-D1 to P4-D15, DECISION-2402caab (P4-D10, the write tools) and
DECISION-d9ddaf0f (the spec-level choices accepted, item 8 on the write tools). Read
KNOWLEDGE-ebc4b9fd (M30's review), whose four caveats this task's pre-flight settles.

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
- **Host:** Windows with Developer Mode on (D7), Python 3.11. The tests create symlinks, a
  junction and a hard link in temporary folders.

**Running tests**
- **Only one test session at a time, and nothing else in parallel with one.** Every pytest session
  compares the store's ids at its start and end (AC-44), and the development database is shared.
- Check that no other python or node process is running first.
- Run your own probes with `-p no:cacheprovider`: Genesis hashes .pytest_cache.
- **The full suite takes about 10 minutes.**

**Gates (DECISION-066e317a).** Run `genesis gate . M31-write-tools --timeout 1500000` in the
background, or detached. Then check for a stray python process. If a gate reads `stale` straight
after, run the gate file once with the cache on, then gate again. Writing any file under the
repository while a gate runs can stale it.

**Mutations:** restore a mutated file byte for byte and verify its SHA-256 in a `finally`. Beware
`git stash`: on this host it rewrites line endings (KNOWLEDGE-ae83f412). Do not mutate an applied
migration file.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown.

**Cleanup order:** delete `interruption_decisions`, then `interruptions` and
`standing_decisions`, then the run's other rows, by run id. Probe folders on disk are yours to
remove; never touch a path outside your own temporary folders.

## Task under review

**M31-write-tools**, **round 1**, risk **high**. HEAD is `a669934` (M30). These are the first
tools that change anything on disk.

### Pre-flight: M30's caveats (KNOWLEDGE-ebc4b9fd)

1. **What runs is what was approved.**
   - `ToolExecutor._approval` deep-copies the arguments once, replaces `prepared.tool_call` with
     that copy, and records another deep copy in the request.
   - Found on the way: the in-memory store kept and handed out its own `RunInterruption`, so a
     caller could rewrite a recorded request. It now copies in and out (`_copied`).
2. **Fail closed:** `needs_approval` must answer a `bool`; anything else fails the call
   `ToolApprovalCheckFailed`.
3. **`ApprovalRequested`** is emitted right after each interruption is recorded, not when the run
   first waits.
4. **`Runner.decide(sticky=True)`** wakes the run as soon as the decision is recorded. If the
   standing write then fails, it raises, saying the decision was recorded and the standing
   decision was not.

### M31

- **`agentsdk/tools.py`:** `ApprovedCall(interruption_id, request)`.
  - **The executor** keeps it on `PreparedCall.approved`.
  - **`_with_approved`** hands it to a tool parameter annotated `ApprovedCall`, overriding
    anything sent under that name, as `_with_scope` does.
- **`agentsdk/builtin_tools.py`:**
  - **`_FileSystem` binds** `WriteFile`, `FlushFileBuffers`, `MoveFileExW` and `DeleteFileW`. It
    adds `open_target` (no following: `FILE_FLAG_OPEN_REPARSE_POINT`), `attributes`
    (FileBasicInfo), `create_new`, `write_all`, `replace` (`MOVEFILE_REPLACE_EXISTING |
    MOVEFILE_WRITE_THROUGH`) and `delete`.
  - **`list_handle` withholds `_TEMPORARY` names.**
  - **`_Target`** holds the parent directory open, without delete sharing, from the check to the
    replace. It refuses an 8.3 alias as the final name.
  - **`current()`** refuses a link target, a directory, a hard-linked file, or one over
    `max_bytes`.
  - **`write()`** writes `.<name>.agentsdk-<uuid>.tmp`, flushes it, and replaces the target with
    it. On any failure it deletes the temporary file; error 5 or 32 is refused as "in use".
  - **`_edited`** counts overlapping occurrences and keeps the byte-order mark and every byte
    outside the span.
  - **`_describe_change`** computes the path, the before and after SHA-256s, the byte counts and
    the line.
  - **`_apply_change`** refuses "changed since approval" unless the `before` the approver saw
    still holds.
  - **`write_file_tool` and `edit_file_tool`:** WRITE, not read-only, not idempotent, not
    concurrency-safe, `REQUIRE_APPROVAL` by default.
- **`scripts/25_write_tools.py`** (FR-119): offline on both stores and live.
- **`tests/test_write_tools.py`** (the unit gate): 39 tests.

### Edits to approved tests

- **`tests/test_distribution.py`:** `EXPECTED_EXAMPLES` gains `25_write_tools.py`.
- **No other approved test is changed.**

## What changed

```bash
git diff a669934 -- agentsdk tests scripts README.md
git status --short
```

## What the author ran

1. **Tests first.** `tests/test_write_tools.py` was written before the tools existed.
   - **The atomicity test first failed on real Windows behaviour:** the polling reader holds the
     file open without delete sharing, so the replace is refused `target in use` by design.
   - **The fix:** the test retries such writes, requires all 30 to land, and asserts that the
     reader saw only the old or the new content.
2. **Mutation run: 19 of 19 killed,** each file restored by SHA-256. Two first survived, both test
   gaps:
   - **"The store hands out its own record"** in the single lookup: the aliasing test read only
     through the listing.
   - **"A directory target accepted":** reading a directory handle fails anyway, so the call was
     still refused, only for the wrong reason. The test now asserts the reason.
3. **Full suite before the gates:** 2146 passed, with no failure.
4. **Example 25 live,** on the gateway and PostgreSQL: all 4 live checks passed. The real model
   retried the two refused calls once each, and both were refused again. Its rows were removed by
   run id.
5. **Gates,** both green on source hash `9c531471`, run in the background with `--timeout
   1500000`.
   - `unit`: 39 passed in 8.67s.
   - `regression`: 2148 passed in 625.37s (18:30:26 to 18:40:53 UTC). The baseline was 2109.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m31-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m31r1`. Delete only rows your probes wrote, by run id, interruptions first, and only
folders you created.

1. **Confinement (FR-36, NFR-14).** Escape the root for a write or edit by every route you can
   build:
   - `..` and absolute paths, device and stream names, 8.3 aliases, trailing dots or spaces;
   - symlinks, junctions and hard links, at the target and anywhere in the path;
   - a link swapped between `describe` and execution, and during the write.

   Does any refusal name a path outside the root?
2. **What is written is what was approved.**
   - Can a call write anything other than the `after` the approver saw: through a changed file,
     the arguments, a hook, or the record?
   - Is "changed since approval" checked against the right bytes?
   - Can the model, or anything else, supply `approved`?
3. **Atomicity and cleanup.**
   - Can a reader ever see a partial file?
   - Can a temporary file be left behind: a crash, cancellation mid-write, a failing flush, a
     failing replace?
   - Is every temporary name withheld from list, glob and grep?
4. **Edits.** Overlapping occurrences, byte-order marks, CRLF and line endings, non-UTF-8 files,
   `max_bytes` on both the file and the result, an empty `new`, an `old` at the very start or end.
5. **The pre-flight.**
   - Mutate the arguments, the request and the store's records every way you can, and look for
     divergence.
   - Do a non-bool `needs_approval`, an `ApprovalRequested` race and a failing sticky write each
     behave as stated?
6. **NFR-33.** No new runtime dependency, and every Win32 call is inside `_FileSystem`: AC-29's
   source check.
7. **The FR-45 pool.** Do `describe` and the write both run on the file-tool pool and never on the
   event loop? Look at blocking under contention with the read tools.
8. **The approved-test edit** listed above.
9. **Example 25 live,** if you can afford it, and **regressions:** the full suite, and the gate
   files of M10 and M29 to M30.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M30.**
- **The re-hash window:** a program outside the SDK can still change the file between the re-hash
  and the replace; the window cannot be closed from user mode.
- **A target held open** without delete sharing is refused `target in use`, not retried.
- **Windows only,** like M10's tools (D11).
- **Orchestration is M32.**

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
  control approve . M31-write-tools --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M31-write-tools --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the nine attack items, and
your verdict.
