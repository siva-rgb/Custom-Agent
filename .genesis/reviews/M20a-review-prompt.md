You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Sonnet 5** if Fable 5.1 fails
to start. M20a was implemented by Opus 5.5; Opus 5 reviewed M20, the code it amends. Bring no
memory of an earlier review into this one: if your session holds any, say so and ask for a fresh
one. Re-derive the requirements from SPEC.md ("#### M20a") rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M20a adds no library.
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, AC-63) and the development database is shared.
Check that no other python or node process is running first. Run your own probes with
`-p no:cacheprovider`: Genesis hashes .pytest_cache. The full suite takes about 6 minutes; keep every
command in the foreground and under 590 s. **`genesis gate` kills a gate after 120 s unless you pass
`--timeout 590000`**, and on this host that kills only the venv launcher (KNOWLEDGE-64fa6e18): run
`genesis gate . M20a-compaction-record --timeout 590000`, then check for a stray python process.
**Restore a mutated file byte for byte**, line endings included, and verify its SHA-256 in a
`finally`; some files here are CRLF in the working copy (KNOWLEDGE-ae83f412).

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`.

## Task under review

**M20a-compaction-record**, **round 1**: two caveats from M20's round 1 review (KNOWLEDGE-cc4d686c),
specified before Phase 3. C1: `ContextCompacted`'s `tokens_before` was the measure that decided the
compaction while `tokens_after` was a whole-view estimate, so the pair could read backwards (108 of
330 fuzzed compactions). C3: the summary was appended and the event emitted afterwards, so a failed
emit left a summary in `messages` with no event explaining it.

### Owner decisions and readings that shape it

- **DECISION-f56a9415** (owner): C1 by a same-basis pair plus the decider -- `tokens_before` and
  `tokens_after` both whole-request estimates of the view before and after, the deciding number kept
  as `threshold_measure` with `threshold_basis`; C3 by one transaction on Postgres, as M19's F4 did
  for transitions, with the same rule in memory, the artifact written first.
- **KNOWLEDGE-43a86c0a** (pre-flight readings). Judge each:
  (a) FR-86 is met with **one database transaction**, stronger than F4's shape (F4 emits through
  the sink's own connection inside the transition's transaction scope): the message insert and the
  event insert are factored out (`postgres._insert_message`, `PostgresEventStore.write`) and
  `PostgresSessionStore.append_with_event` takes the messages lock, then the events lock, and
  inserts both on one connection;
  (b) after the commit the event reaches the run's buffer and its `RunHandle` through a new
  `stored()` on the sink, and `PublishingSink` offers `write`/`stored` only when the sink it wraps
  has them;
  (c) in memory, `InMemorySessionStore.append_with_event` appends, emits, and takes the message
  back out if the emit fails;
  (d) the combined write is an optional extension of the SessionStore protocol (NFR-7): a custom
  store without it falls back to the two writes in turn, as before M20a;
  (e) `tokens_before` keeps its name and changes meaning; (f) so do the M20 tests that read it.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-85:** `ContextCompacted` records its token counts on one basis. `tokens_before` and `tokens_after` are both the estimate of the whole request built from the history's view, before and after the compaction, so the pair compares like with like; the number that decided the compaction is recorded beside them as `threshold_measure`, with `threshold_basis` naming how it was measured -- the provider's `prompt_tokens` for the last request plus an estimate of what was added since, or the whole-request estimate when no report applies (DECISION-468e2bfa). Round 1 caveat C1: today `tokens_before` is the deciding measure and `tokens_after` a whole-view estimate, and 108 of 330 fuzzed compactions recorded the pair reading backwards although the prompt shrank on every one.
- **FR-86:** A compaction's summary message and its `ContextCompacted` event are written together or not at all. On Postgres they are written in one transaction, as M19 writes a plan node's transition and its event (F4), so no stored summary lacks the event that explains it and no event names a summary that was not stored; the in-memory stores keep the same rule. The artifact holding what was replaced is written first, so an artifact with no compaction is the only thing a failed write can leave, and nothing reads it as one. Round 1 caveat C3: today the summary is appended and the event emitted afterwards, so a failed emit leaves a summary in `messages` with no event, and FR-78's "every compaction is recorded" holds only for runs that survive.
- **AC-68:** Over compactions whose script, result sizes, window, `keep_recent_turns` and `compact_at` vary, every `ContextCompacted` event's `tokens_before` and `tokens_after` equal the whole-request estimates recomputed from the stored history's view before and after it, and its `threshold_measure` is at least `compact_at` times the window and names its basis; a compaction decided by the provider's count and one decided by the estimate are each shown.
- **AC-69:** A failure injected at the summary write and at the event write each leave neither behind, on both stores: no stored summary without its `ContextCompacted` event and no event without its summary, the run ends `failed` with the store's error as any store failure does, and `sequence_no` stays contiguous. A compaction that completes writes both.

### Standing invariants that constrain every task

- Every persisted row carries non-null `tenant_id` and `project_id`, taken from the run row.
- `sequence_no` is unique and contiguous per run on both stores (NFR-17); the messages and events
  locks are what keep it so under concurrent writers.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- A boundary's error path must not itself be able to raise.
- A cancellation is recorded, then re-raised (INVARIANT-73299c7c).

### What changed

**M20a is not committed.** HEAD is `a576adc` (M20). The specification of M20a (SPEC.md and the
Genesis plan records) is also uncommitted. Review it with:

```bash
git diff a576adc -- agentsdk tests scripts SPEC.md
git status --short
```

- `agentsdk/compaction.py`: `whole()` (the one basis) and `basis()`.
- `agentsdk/loop.py`: `_compact` records `tokens_before`/`tokens_after` with `whole()`, adds
  `threshold_measure`/`threshold_basis`, builds the payload before writing, and writes the summary
  and the event through `append_with_event` when the session store has it.
- `agentsdk/postgres.py`: `_insert_message` factored out of `PostgresSessionStore.append`;
  `append_with_event`; `PostgresEventStore.emit` split into `write` and `stored`, emit unchanged in
  behaviour.
- `agentsdk/session.py`: `InMemorySessionStore.append_with_event`.
- `agentsdk/handle.py`: `PublishingSink.__getattr__` for `write` and `stored`.
- `scripts/18_context.py`: prints the deciding measure and its basis.
- `tests/test_compaction_record.py` (new, the gate file, 15 tests).
- **Edits to approved tests:** `tests/test_context_policy.py` (M20, approved), two assertions that
  read the deciding number through `tokens_before` now read `threshold_measure`, and assert its basis.
  FR-85 changes what `tokens_before` means; the property each test checks is unchanged.

## What the author ran

1. **Gate file:** 15 tests. AC-68: a seeded fuzz of 40 runs (script shapes with parallel calls,
   result sizes, windows 600 to 2500, `keep_recent_turns` 1 to 3, `compact_at` 0.5/0.75/0.9, and
   providers that report the real size or nothing), recomputing each pair from the stored history
   and the artifacts with an estimator written out in the test, both bases required to occur; the
   same on Postgres; the provider's count deciding; the estimate deciding. AC-69: a failure at the
   event write and at the summary write, each on both stores, and a completing compaction on both;
   the event reaching `RunResult.events` and the `RunHandle` stream in stored order; a custom
   session store's fallback; a sink for another run refused, on both stores.
2. **A finding while writing it:** when the provider's count decides on a small history, the
   summary can be longer than what it replaced. The same-basis pair now shows that compaction made
   the history larger (67 before, 93 after); under M20 the pair could not show it. A test pins it.
3. **Mutation run:** **12 of 12 killed** on the first run, each restored by SHA-256 and checked
   byte-identical, run against both gate files: each FR-85 field, the loop writing apart, Postgres
   writing in two transactions or the event outside the summary's, the in-memory take-back, the
   stored event not reaching the buffer or the handle, and the other-run sink check on both stores.
4. **Demo command:** `scripts/18_context.py --offline` 6 of 6 and **live 6 of 6**. Read back from
   Postgres: completed, two summaries and two `ContextCompacted` events, contiguous events; live the
   provider's count ran above the estimate (decided at 1881 and 2024, the pair 1577/748 and
   1712/796). Removed by its run id; example-tenant is at 19 runs and 6 artifacts, 0 running.
5. **Gates.** Both green on source hash `f19dc611`, from one `genesis gate --timeout 590000` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_compaction_record.py -q`, exit 0, 15
     passed in 8.04s (04:32:48 to 04:33:01 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1647 passed in 355.32s
     (04:33:03 to 04:39:00 UTC). The baseline before M20a was 1632.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts in a folder `m20a-r1-probes` in your own session's scratchpad, with
tenant ids starting `SYN-m20ar1`; delete only rows your probes wrote, by the run ids they recorded.

1. **Both or neither.** Fail every write in `append_with_event` -- each insert, each lock, the
   commit, `stored()` after it -- on both stores, and check what is left. Is "the artifact with no
   compaction" really the only leftover, and does anything read it as one?
2. **Concurrency.** The combined write takes the messages lock then the events lock. Can any other
   writer take them in the other order and deadlock with it? Run compaction alongside concurrent
   tool events and messages for the same run; is `sequence_no` contiguous on both tables?
3. **The stream.** Does every event still reach `RunResult.events` and `RunHandle.events()` in
   stored order -- the ordinary `emit` path included, which now goes through `write` and `stored`?
4. **FR-85's pair.** Recompute it yourself, with a probe that does not choose the schedule, on both
   stores. Can the pair or the basis name anything other than what happened?
5. **Fallbacks and protocol.** A custom session store, a custom sink, a sink for another run or
   tenant, a `PublishingSink` over a sink without `write`.
6. **The demo command**, offline and live, checked against the store.

## Declared limitations: known, recorded, NOT findings

- **A custom session store** without `append_with_event` writes the two in turn, as before M20a,
  so C3's gap remains for it.
- **The artifact is written before the transaction**, so a failed transaction leaves it: tagged
  with the run and an untrusted provenance, and named by nothing.
- **A compaction can make the history larger** when the provider's count decides on a small one;
  the record now says so rather than hiding it.
- **`tokens_before` changed meaning under the same key.** Nothing in the SDK reads it; an external
  consumer of M20's payload would.
- Everything declared for M20 still stands (KNOWLEDGE-cc4d686c), C4 to C6 among it.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T`).
- **Clean up** every run, row, temp folder and process you create, and say what you removed.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M20a-compaction-record --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M20a-compaction-record --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the six attack items, and your
verdict.
