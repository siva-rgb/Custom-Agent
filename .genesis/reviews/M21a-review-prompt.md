You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Opus 5, falling back to Sonnet 5** if Opus 5 fails to
start. M21a was implemented by Opus 5.5; Fable 5.1 approved M21 in round 2 and raised the caveats
this settles, and Sonnet 5 rejected M21's round 1. Bring no memory of an earlier review into this
one: if your session holds any, say so and ask for a fresh one. Re-derive the requirements from
SPEC.md ("#### M21a") rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M21a adds no library.
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, AC-63) and the development database is shared.
Check that no other python or node process is running first. Run your own probes with
`-p no:cacheprovider`: Genesis hashes .pytest_cache. The full suite takes about 6 minutes; keep
every command in the foreground and under 590 s. **`genesis gate` kills a gate after 120 s unless
you pass `--timeout 590000`**, and on this host that kills only the venv launcher
(KNOWLEDGE-64fa6e18): run `genesis gate . M21a-carried-caveats --timeout 590000`, then check for a
stray python process. **Restore a mutated file byte for byte** and verify its SHA-256 in a
`finally`. Beware `git stash`: on this host it rewrites line endings (LF to CRLF), so a stash and
pop changes a file's hash without changing its text (KNOWLEDGE-ae83f412). `agentsdk/session.py`
has CRLF line endings.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`.

**Concurrency probes:** at the default 5 ms thread switch interval a race in this store can hide;
set `sys.setswitchinterval(1e-6)` while a probe contends (KNOWLEDGE-93fa7f44; M21's round 2 showed
a detector catching a restored race 8 of 8 at 1 microsecond but 1 of 8 at 5 ms).

## Task under review

**M21a-carried-caveats**, **round 1**. M21 was approved in round 2 with four caveats carried
(KNOWLEDGE-8e29cfe3). Before M22's pre-flight, the owner settled them (DECISION-23ae5809) and had
them specified as FR-97 and AC-75 and run as their own task.

### Owner decisions that shape it (DECISION-23ae5809)

- **Caveat 1, repaired:** `InMemorySessionStore.append_with_event` derives the run's key once, and
  its write and its take-back act on that one run. Under M21 an unbound combined write racing the
  run's first bound write could leave the run id filed under two keys, silently.
- **Caveat 2, repaired:** the first tenant and project to bind a run id adopt the history written
  to that id unbound, as the store merged them before M21. A second tenant binding the same id
  still gets a history of its own, and an unbound read of an id two tenants hold is still refused.
- **Caveat 3, declared:** inside a bound combined write, a sink that writes a different run is
  refused (FR-87 requires it). The Runner never builds such a sink, which a test asserts for a
  top-level run, a subagent and an orchestrator's child.
- **Caveat 4, kept declared (M20a):** on Postgres, a scope-less custom sink whose `emit` fails
  leaves the summary stored without its event.
- **Governance:** Genesis cannot reopen the plan while a task is active and has no command to
  clear one, so the owner approved one manual edit of `.genesis/project.json`: M22's active pointer
  cleared and M22 returned to queued, and M21a moved ahead of M22 in the task list after
  `task add`. Nothing else was edited by hand; the plan was then reopened, checked and approved
  through the CLI.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-97:** M21's four carried caveats (KNOWLEDGE-8e29cfe3) are settled before M22, as the owner decided (DECISION-23ae5809). The in-memory combined write derives a run's key once, and its write and its take-back act on that one key, so a combined write racing the run's first bound write never leaves the run id filed under two keys. The first tenant and project to bind a run id on the in-memory store adopt the history written to that id unbound before it, as the store merged them before M21; a second tenant binding the same id still gets a history of its own, and an unbound read of an id two tenants hold is still refused. Two are declared, each stated in the README's limits and asserted by a test of the behaviour as declared: inside a bound combined write a sink that writes a different run is refused, which the Runner never reaches because every sink it builds -- for a top-level run, a subagent or an orchestrator's child -- names its own run; and on Postgres a scope-less custom sink whose emit fails leaves the summary stored without its event (M20a's declared limit).
- **AC-75:** FR-97: with a wrapper on `append` that makes the run's first bound write between the combined write's key and its append, the run id is filed under one key and the take-back on a failing event leaves that key's history as it was; an unbound append before a Runner's first write is read back unbound in order with the Runner's messages, and a second tenant's run with the same id keeps a separate history while an unbound read of that id is refused; every sink the Runner builds for a top-level run, a subagent and an orchestrator's child names that run's tenant, project and run id; a bound combined write with a sink for another run is refused with nothing written; and on Postgres a scope-less sink whose emit fails leaves the summary stored and no event, as declared. Each repair has a test that fails without it.

## What changed

**M21a is not committed.** HEAD is `de0de37` (M21). Review with:

```bash
git diff de0de37 -- agentsdk tests README.md SPEC.md
git diff de0de37 --stat
```

- `agentsdk/session.py`, the only source change:
  - `_held(key, create=)`: one resolution, made with the lock held, of the key a run's history
    lives under. An unbound key resolves to the one run of its id, or is refused if two tenants
    hold it. A bound key not yet stored adopts the id's unbound history by moving the list object
    itself, so a write already holding that list lands in the adopted history.
  - `_unbound` is gone: `_key` resolves through `_held` without creating.
  - `append` resolves and appends under one acquisition of the lock.
  - `history` resolves (and may adopt) under the lock.
  - `append_with_event` takes the run's list once, before the write, and binds the write to the
    key it derived, still through `append`, so wrappers see it.
- `README.md`: two rows in "What it does not do yet", for caveats 3 and 4.
- `SPEC.md`: an M21a section with FR-97, and AC-75.
- `tests/test_carried_caveats.py` (new, the gate file, 10 tests).
- `.genesis/`: DECISION-23ae5809, the task, the plan and spec approvals, and the manual edit
  described above.
- **Edits to approved tests:** none.

## What the author ran

1. **The gate file first, against M21's code:** 5 failed (both caveat 1 variants with "run 'r'
   exists in more than one tenant or project", all three caveat 2 tests) and 3 passed (the three
   declared-behaviour tests, as they should). Two caveat 1 interleaving tests were added after
   the repair, to pin the binding and the take-once, which the first tests could not tell apart
   from adoption alone.
2. **Mutation run:** **9 of 9 killed**, each file restored by SHA-256. The mutants:
   - the combined write's append not bound to its key;
   - the take-back looking the run up again after the write;
   - no adoption;
   - adoption leaving the run index stale;
   - adoption only on a write;
   - an ambiguous unbound id not refused;
   - `_key` never resolving an unbound id;
   - the Runner building a sink for another run;
   - Postgres emitting before it appends for a scope-less sink.
3. **Gates.** Both green on source hash `8ade0977`, from one `genesis gate --timeout 590000` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_carried_caveats.py -q`, exit 0, 10
     passed in 1.04s.
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1675 passed in 372.14s
     (12:00:03 to 12:06:17 UTC). The baseline before M21a was 1665.
4. **Store:** example-tenant at 19 runs, 6 artifacts, 0 running, no `SYN-m21a` rows left.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m21a-r1-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m21ar1`; delete only rows your probes wrote, by the run ids they recorded.

1. **Adoption.**
   - Is moving the unbound list into the first bound key ever wrong?
   - Two tenants binding the same id at once, a bound read racing an unbound write, an adoption
     racing a take-back.
   - Can a key ever be in `_runs` and missing from `_by_run`, or the other way round? Write a
     probe that checks this invariant after each contended attempt, at a 1 microsecond switch
     interval.
   - Does adoption on a bound *read* (`history`) have any effect a caller would not expect?
2. **Caveat 1 under real concurrency**, not only the wrapper interleavings in the gate file:
   unbound combined writes, some with failing sinks, racing first bound writes of the same run ids.
   Is any run id filed under two keys, or any summary left without its event in memory?
3. **Lock discipline:** is the lock ever held across `emit`, nested, or taken re-entrantly? A sink
   whose `emit` calls the store on the same thread, or on another thread and waits for it, must
   still complete.
4. **The declarations:**
   - Is caveat 3's test a faithful proof that the Runner never builds another run's sink? It
     subclasses `AgentLoop` and reads `self._events.scope`. Is there a path that builds a loop
     another way?
   - Is caveat 4's README row and test faithful?
5. **Regressions:**
   - M21's gate file, subagents and the orchestrator on the in-memory store, and the 47 unbound
     call sites M21's round 2 counted (42 in the suite, 5 in scripts 02, 03, 04, 06 and 10, run
     offline).
   - Does anything in the suite or the examples rely on the M21 behaviour that caveat 2 changes
     (an unbound write before a bound one making unbound reads refuse)?
6. **Governance:** is the manual edit of `.genesis/project.json` limited to what DECISION-23ae5809
   says? Check with `git diff de0de37 -- .genesis/project.json`.

## Declared limitations: known, recorded, NOT findings

- **Caveats 3 and 4** as declared in DECISION-23ae5809 and the README.
- **A caller's own session store** cannot be bound by the Runner and keeps its behaviour.
- **An unbound read** of a run id that two tenants hold is refused rather than guessed. So is an
  unbound write whose run id becomes ambiguous before it lands: it raises with nothing written.
- **Adoption gives unbound history to the first tenant to bind the id.** An unbound write carries
  no tenancy; the owner chose to restore the pre-M21 merge for it.
- Everything declared for M20, M20a and M21 still stands (KNOWLEDGE-cc4d686c, KNOWLEDGE-afac039f,
  KNOWLEDGE-8e29cfe3), except caveats 1 and 2, which this repairs.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`.
- **Clean up** every run, row, temp folder and process you create, and say what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M21a-carried-caveats --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M21a-carried-caveats --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the six attack items, and your
verdict.
