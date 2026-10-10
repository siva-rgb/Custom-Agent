You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M21 was implemented by Opus 5.5 and round 1 was reviewed by Sonnet 5; Fable 5.1 reviewed
M20a, whose findings M21 repairs, and Opus 5 reviewed M20. Bring no memory of an earlier review into this one: if your session holds any,
say so and ask for a fresh one. Re-derive the requirements from SPEC.md ("#### M21") rather than
inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M21 adds no library.
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, AC-63) and the development database is shared.
Check that no other python or node process is running first. Run your own probes with
`-p no:cacheprovider`: Genesis hashes .pytest_cache. The full suite takes about 5 minutes; keep
every command in the foreground and under 590 s. **`genesis gate` kills a gate after 120 s unless
you pass `--timeout 590000`**, and on this host that kills only the venv launcher
(KNOWLEDGE-64fa6e18): run `genesis gate . M21-carried-items --timeout 590000`, then check for a
stray python process. **Restore a mutated file byte for byte** and verify its SHA-256 in a
`finally`. Beware `git stash`: on this host it rewrites line endings (LF to CRLF), so a stash and
pop changes a file's hash without changing its text (KNOWLEDGE-ae83f412; the author hit it).

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`.

## Task under review

**M21-carried-items**, **round 2**, the first milestone of Phase 3. Phase 3's first increment was
specified and approved on 2026-10-10 (FR-87 to FR-96; P3-D1 to P3-D12 in DECISION-39f28e48 and
DECISION-423f3e23; the reviewed draft's choices in DECISION-10494890). M21 carries M20a's round 1
findings F1 and F2 (KNOWLEDGE-afac039f) and decides M20's caveats C4 to C6 (KNOWLEDGE-cc4d686c).

### Owner decisions that shape it

- **DECISION-153046de** (owner): F1 and F2 repaired first, as FR-87. **C4 declared** (`tools_sent`
  is configuration, written at run start). **C5 declared** (under a ContextPolicy an agent is sent
  its `tool_profile`'s tools even when its own `permission_policy` permits others). **C6 repaired**
  at plan submission: every role a node to be run names is checked against the Runner before
  anything is adopted, stored or spawned, and a role that cannot run ends the plan failed and
  unreplannable, naming the role and why. Read the decision for the alternatives rejected.

### Round 1, and what changed

Round 1 (Sonnet 5) rejected M21 on one finding (DECISION-f12abafb): `InMemorySessionStore._unbound`
iterated the store's dict with a bare comprehension while other threads added runs, so an unbound
append or read raised "dictionary changed size during iteration" -- 8 of 8 attempts in the
reviewer's probe, 0 of 8 on HEAD `c244f23` under the same load. A regression M21 introduced, mostly
on the write path, contradicting FR-87's promise that a caller the Runner cannot bind keeps its
behaviour; each unbound call was also an O(runs) scan. Everything else held, each negative with a
mutant behind it.

**The repair:** one lock over the store's dict and a new run id to keys index (`_by_run`), so an
unbound call finds its run without a scan; `append`, the take-back and the `history` copy run under
the same lock, and no lock is held across a sink's `emit`. A `list()` copy alone was rejected
because it keeps the scan. **The test**, written first and failing on round 1's code (1599 errors on
its first attempt): six threads bind new runs while six make unbound appends and reads, eight
attempts, nothing patched, with the thread switch interval at a microsecond. The first mutation run
showed why that matters: at the default 5 ms a restored scan finished between switches and survived.

Round 1 left two gaps of its own, which this round should cover: **Postgres-backed orchestrations
beyond C6**, and **the 44 unbound call sites** (tests and examples that read
`runner._sessions.history(run_id)`) beyond what the suite exercises. The finding was in the unbound
path; look hardest there.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-87:** The combined write M20a added never refuses or crashes on a sink that names no scope: a sink with no `scope`, or whose `scope` is None -- a `PublishingSink` over a scope-less custom sink -- gets the two writes it got before M20a, with the in-memory take-back kept. A sink that does name a scope is refused, before anything is written, unless it names the run being written to by tenant and project as well as run id. The Postgres store already compares all three. The in-memory session store is bound per run as the Postgres one is, keying a run's history by tenant, project and run id, so two tenants' runs never share a history and a sink for another tenant or project is refused; a caller's own session store, which the Runner cannot bind, keeps its behaviour. M20a round 1 findings F1 and F2 (KNOWLEDGE-afac039f): F1 is reachable only on the in-memory store, where `PublishingSink.scope` returns None over a scope-less sink.
- **FR-88:** M20's caveats C4 (`tools_sent` written at run start, before anything was sent), C5 (visibility follows `tool_profile`, not a custom permission policy) and C6 (under the Orchestrator's default policy, a child on a model outside the ModelRegistry is refused at spawn and surfaces as `node_error` rather than at configuration) are each repaired or recorded as a declared limitation at M21's pre-flight, with the owner's decision and its reason (KNOWLEDGE-cc4d686c). A repair has a test that fails without it; a declared limitation is stated in the README's limits and asserted by a test of the behaviour as declared, so neither enters the ledger milestones unexamined.
- **AC-70:** FR-87: on the in-memory store, a run whose sink is a `PublishingSink` over a scope-less sink completes and records the summary and its event as it did under M20, where today it fails with a TypeError; on both stores a sink naming another run, another tenant with the same run id, or another project is refused before anything is written; two tenants' in-memory runs with one run id keep separate histories; and the in-memory take-back still removes the summary when the event fails. M21's pre-flight decision on each of C4 to C6 is recorded, each repair has a test that fails without it, and each declared limitation has a test of the behaviour as declared.

### Where to attack first

1. **The in-memory binding.** `InMemorySessionStore` now keys history by (tenant, project, run id);
   the Runner gets a bound view per run that sets a context variable and calls the store's own
   public methods, so a test double or wrapper on `InMemorySessionStore.append` still sees every
   write (a first version wrote past `append` and broke three approved tests). Does the context
   variable follow every write onto its worker thread, and can it leak to a call it should not
   reach -- a concurrent run on the same store, a nested run (subagents), a cancelled write? Do
   unbound reads by run id, which 44 test and example call sites use, still find a Runner's run?
2. **F1 and F2 on both stores.** Every combination of scoped and scope-less sink, `PublishingSink`
   wrapping, with and without `write`/`stored`, for this run, another run, another tenant with the
   same run id, another project. Is anything written before a refusal?
3. **C6.** `_refuse_unrunnable_roles` calls `Runner._open` for each role with the orchestrator's own
   lease. Does `_open` have any side effect (a sink, a started-run record, a row)? Does the check
   cover every configuration error a child would meet at spawn, and only those? Carried nodes are
   skipped; is that right? Can a replan reach a role the first plan did not check?
4. **C4 and C5's declarations**: are the README rows and the tests faithful to the behaviour?
5. **Regressions**: subagents, orchestrator, compaction and handles on the in-memory store.

## What changed

**M21 is not committed.** HEAD is `c244f23` (the Phase 3 increment 1 specification). Review with:

```bash
git diff c244f23 -- agentsdk tests README.md
git status --short
```

- `agentsdk/session.py`: `InMemorySessionStore` keyed by (tenant, project, run id), `bind()`,
  `_BoundInMemorySessions`, the context variable `_BINDING`, `_refuse_foreign`.
- `agentsdk/api.py`: `Runner._sessions_for` binds the in-memory store per run (used by `_run` and
  `_history_for`); the `tool_profile` comment updated for M20 and C5.
- `agentsdk/postgres.py`: `append_with_event` refuses a scoped foreign sink before anything else and
  sends a scope-less sink down the two-write path.
- `agentsdk/orchestrator.py`: `_refuse_unrunnable_roles`, called in `_submit` before `adopt`.
- `agentsdk/manifest.py`: the `tools_sent` comment declares C4.
- `README.md`: two rows in "What it does not do yet" for C4 and C5.
- `tests/test_carried_items.py` (new, the gate file, 18 tests; the 18th is round 1's).
- **Round 2:** `agentsdk/session.py` only -- the lock, the `_by_run` index, and the four calls made
  under the lock.
- **Edits to approved tests:** none.

## What the author ran

0. **Round 1's finding first**: the concurrency test failed on round 1's code (1599 errors on its
   first attempt), then the repair. Mutation run on the final code: **13 of 13 killed** (round 1's
   11, one re-pointed to the take-back's new place under the lock, and two for round 2: the scan
   restored outside the lock, and the run index not kept).
1. **The gate file first, against M20a's code:** with the three source edits stashed, 8 of the
   first 12 tests failed, F1's TypeError among them; restored, all passed. (The stash rewrote line
   endings; the restored files were checked equal with carriage returns stripped.)
2. **Mutation run:** **11 of 11 killed**, each restored by SHA-256. The first runs left two
   survivors, each closed by a test: the Runner not binding its store (no test checked where a real
   run's history was filed), and C6 checked without the run's lease (the USD case also lacked a
   window, so the window check caught it anyway; the USD case now names a window, so only the
   missing price can refuse it).
3. **Gates.** Both green on source hash `1c386e0f`, from one `genesis gate --timeout 590000` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_carried_items.py -q`, exit 0, 18 passed
     in 27.24s (09:48:52 to 09:49:20 UTC); the concurrency test is most of that.
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1665 passed in 368.43s
     (09:49:21 to 09:55:31 UTC). The baseline before M21 was 1647.
4. **Example 17** (the orchestrator), at round 1 (round 2 changes only the in-memory session store,
   which the live run does not use), offline 5 of 5 and live 4 of 4, read back from Postgres and
   removed by its run id (6 runs); example-tenant is at 19 runs and 6 artifacts, no orphans.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probes in a folder `m21-r2-probes` in your own session's scratchpad, with tenant ids
starting `SYN-m21r2`; delete only rows your probes wrote, by the run ids they recorded.

1. The in-memory binding (item 1 above), and **round 1's repair under concurrency**: unbound and
   bound calls, appends, reads and take-backs racing on one store, a probe that does not choose the
   schedule, with a control showing your probe can see the round 1 defect.
2. F1 and F2's matrix (item 2) on both stores.
3. C6 (item 3), including side effects of `_open` and a replan.
4. The declarations (item 4).
5. Regressions on the in-memory store (item 5), with a probe that does not choose the schedule,
   including the unbound call sites round 1 did not exercise, and Postgres-backed orchestrations.

## Declared limitations: known, recorded, NOT findings

- **C4 and C5** as declared in DECISION-153046de and the README.
- **A caller's own session store** cannot be bound by the Runner and keeps its behaviour, including
  run-id-only checks.
- **An unbound read** of a run id that exists in two tenants is refused rather than guessed.
- Everything declared for M20 and M20a still stands (KNOWLEDGE-cc4d686c, KNOWLEDGE-afac039f),
  except F1, F2 and C6, which this repairs.

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
  control approve . M21-carried-items --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M21-carried-items --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the five attack items, and your
verdict.
