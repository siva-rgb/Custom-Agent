You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Sonnet 5**, in a fresh session. M18a was implemented by
Opus 5.5. The code it changes was reviewed in M18 by Sonnet 5 (rounds 1 and 3) and Fable 5.1
(rounds 2 and 4), so Sonnet 5 is the reviewer who has seen it least recently. If you have
reviewed M18a before, say so and ask for a different session. Re-derive the requirements from
SPEC.md ("#### M18a") rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M18a adds no library.
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44), the development database is shared, and
M14's timing tests assert lower bounds. Before running tests or gates, check that no other python
or node process is running. Note that one gate test, the AC-65 test, starts a second pytest
session as a subprocess and waits for it; that is sequential, not parallel. Run your own probes
with `-p no:cacheprovider`: Genesis hashes .pytest_cache, so a cache write can make a gate read
stale with no code change. The full suite takes about 5 minutes; keep every command in the
foreground and under 590 s. **`genesis gate` kills a gate after 120 s unless you pass
`--timeout 590000`**, and on this host that kills only the venv launcher, leaving the real
interpreter running against the database (KNOWLEDGE-64fa6e18): run
`genesis gate . M18a-validation-and-guard --timeout 590000`, and check for a stray python
process afterwards.

**If the suite hangs, it is not memory** (KNOWLEDGE-9a3abf61). Suspect the two live-gateway tests
in `test_golden_eval.py`; rerun with `-o faulthandler_timeout=120` to see where it stopped.

## Task under review

**M18a-validation-and-guard**, **round 1**: `RunConfig` and `Briefing` refuse an invalid depth,
`output_schema` or `expected_output_schema` at construction; the loop-thread rule is asserted of
every store call by an autouse fixture and refused at the store checkout itself; and a briefed
input carries a provenance-manifest entry and a data-only marker.

It exists because M18's round 4 approved with caveats C1 to C4 (DECISION-938f4485,
DECISION-01c140d4). C4 was the third recurrence of KNOWLEDGE-c0f23fea: a thread-identity guard
that enumerated run paths and so missed call sites nobody listed.

### Where to attack first

1. **FR-82, the checkout helper.** It changes the path every Postgres store call takes. Every one of
   the 18 checkouts in `agentsdk/postgres.py` now goes through `_checkout(dsn)`, which builds the
   pool's connection context manager, then refuses (or logs once and proceeds) when
   `asyncio.get_running_loop()` succeeds in the calling thread. Distrust: whether any checkout
   escapes it; whether building the context manager before the check can take a connection that a
   refusal then leaks; what `sys._getframe(1).f_code.co_qualname` names when a helper sits between
   a store method and `_checkout`; whether a refusal inside a run ever reaches application code
   instead of a failed `RunResult` (INVARIANT-af776957 and INVARIANT-73299c7c, including a
   cancelled run); whether "logs once" holds under threads; and whether the switch,
   `postgres.REFUSE_ON_EVENT_LOOP`, can be on in production by any route other than code setting it.
2. **FR-81, the autouse fixture in `tests/conftest.py`.** This is the requirement most likely to have
   been narrowed to make the suite pass. Check it records every checkout from a thread running a
   loop, from every test, with no exemption, marker, path list or name filter, and that it fails at
   teardown independently of FR-82's refusal (a run's boundary swallows the refusal). Read
   DECISION-e4fe2bc5 for what it found and how that was resolved, then decide for yourself whether
   the resolution narrowed anything.

### Owner decisions made during this task

- **DECISION-53ee27f6** (pre-flight): FR-82's on/off is a module switch, off in code, turned on for
  every test by the conftest fixture. Its depth half was revised by DECISION-8161bcb1.
- **DECISION-8161bcb1**: `RunConfig.depth` is `int | None`, default None. A depth that is set must
  be an int from 1 to 2**31-1 and never a bool, with or without a parent. The first reading (a run
  with a `parent_run_id` must be at least 1) refused FR-21's public usage, `RunConfig(parent_run_id=...)`
  without depth, in scripts/07 and seven approved tests.
- **DECISION-e4fe2bc5**: with the fixture on and nothing else changed, 37 existing tests failed, every
  one a test reading the store synchronously from an async test after a run; none came from
  `agentsdk`. The reads were offloaded, and the fixture was not narrowed. A 38th failure was a
  defect in the new fixture itself (a fresh wrapper per `_pool()` call broke the one-pool-per-DSN
  identity test), fixed by caching one wrapper per pool. After the depth revision, one more
  on-loop read surfaced in a test whose earlier on-loop call had masked it.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-79:** `RunConfig` refuses an invalid `depth` or `output_schema` at construction, by field name, as `max_turns` does (FR-43): `depth` must be an `int` of 1 or more and at most 2**31-1, and a `bool` is refused; `output_schema`, when not None, must be a mapping. Round 4 caveat C1: today both are accepted, and a non-mapping schema surfaces only later as "the schema could not be applied", a re-ask and a failed run.
- **FR-80:** `Briefing` validates `expected_output_schema` at construction -- None or a mapping -- and refuses anything else by field name. A refused briefing never reaches `SubagentPool.spawn`, so it cannot consume a task count in `_admit`, write a row, or change what `spawned()` reports. Round 4 caveat C2: `"abc"` raises a bare `ValueError` from `spawn` after admission.
- **FR-81:** The property DECISION-6b62d1f5 states -- every store call made during a run goes to a worker thread -- is asserted of every store call, not of a list of paths. An autouse fixture wraps `postgres._pool` for every test in the suite and fails any connection checkout made from a thread that has a running event loop, naming the store method and the test. `test_no_store_call_runs_on_the_event_loop_thread_on_any_run_path` remains as a second check, and its docstring records that the fixture is the guarantee. Round 4 caveat C4: forcing the artifact read in `_brief` onto the loop passes that test, because its sixth path spawns with no artifact store, and the cancelled-child path is in no guard at all.
- **FR-82:** Every Postgres store reaches the database through one checkout helper, which refuses when `asyncio.get_running_loop()` succeeds in the calling thread, so a new call site cannot violate FR-20 even where no test covers it. The refusal is on by default under tests, where a violation must fail loudly, and off by default in production, where it logs once and proceeds, so a latent violation costs latency rather than a run.
- **FR-83:** `ContextAssembler`'s provenance manifest gains an entry for every briefed input a subagent receives: the input's uri or hash, origin, instruction authority, trust zone and taint flags, carried as request metadata beside the tool-result entries it already lists (ADR-26), never as text the model reads. A child briefed with no inputs adds no entry. Round 4 caveat C3, owner decision 2026-10-07.
- **FR-84:** The briefing marks a briefed input as data: each input block is delimited and labelled data rather than instructions, naming its uri or hash. The marker is signalling only -- ADR-17 stands, and the enforceable half is FR-83's manifest entry and the policy that reads it -- and `scripts/16_subagents.py`'s check that a child ignored an injected instruction asserts the result's provenance instead of a substring of the child's text, which passes whenever the model paraphrases.
- **AC-64:** FR-79 and FR-80's refusals are proven as classes, not examples: `depth` as a `bool`, a non-int, 0, a negative and 2**31; `output_schema` and `Briefing.expected_output_schema` as a string, an int and a list, with a mapping accepted. For a refused briefing, `spawned()` is unchanged and no row is written in any table.
- **AC-65:** FR-81's fixture is proven both ways: a deliberately un-offloaded store call in one test fails that test with the store method named; the full suite passes with the fixture active, so no existing call site violates the property; and the fixture permits a synchronous store call made from a sync test and an offloaded call made from a worker thread.
- **AC-66:** FR-82's helper is proven on and off: with the refusal on, a checkout attempted from a thread with a running event loop raises and names the method, and the run's status is recorded as on any other failure path; with it off, the same checkout logs once, proceeds, and changes no run's status, events or stored rows.
- **AC-67:** A child briefed with a tainted artifact produces a request whose provenance manifest lists that input with its origin, trust zone and taint flags; the stored history shows FR-84's marker; the child's result still carries the input's taint; and a child briefed with no inputs adds no manifest entry. The example's injected-instruction check passes on provenance, and is shown to fail if the result's provenance is clean.

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve, validate, permission, execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.
- A cancellation is recorded, then re-raised (INVARIANT-73299c7c).

### What changed

**M18a is not committed.** HEAD is `252c51a` (the M18a specification). Review it with:

```bash
git diff 252c51a -- agentsdk tests scripts
git status --short
```

- `agentsdk/postgres.py`: `REFUSE_ON_EVENT_LOOP`, `_checkout`, and the 18 checkouts routed through it.
- `agentsdk/api.py`: FR-79's checks; `depth` becomes `int | None`; a new `briefed_inputs` field,
  validated, set by the pool, passed to the loop.
- `agentsdk/loop.py`, `agentsdk/context.py`: `briefed_inputs` reaches `ContextAssembler.build`,
  which lists each one before the tool-result entries, by uri and labels only.
- `agentsdk/subagents.py`: FR-80's check; `_brief` collects `(uri, provenance)` pairs and wraps each
  input in the FR-84 marker.
- `scripts/16_subagents.py`: the injected-instruction check is `labelled_like_the_note`, over every
  child including the structured one.
- `tests/conftest.py`: the FR-81 fixture. `tests/test_validation_and_guard.py`: the gate file (new).
- **Edits to approved tests**, all under DECISION-e4fe2bc5 except the docstring: each moves a store
  read made from an async test onto a worker thread, and none changes what is asserted.
  - `test_concurrency.py`: three `tool_results(...)` reads with a Postgres backend (lines near 554,
    1130, 1209). The calls passing `None` persistence are unchanged.
  - `test_run_handles.py`: two reads (near 742, 806) and four `assert_cancelled_call` calls.
  - `test_artifacts.py`: five `stores.run(...)` calls in three async tests.
  - `test_phase2_readiness.py`: `Run` gains `__aenter__`/`__aexit__` that offload its existing
    enter and exit, used by the three async tests that held it with `with`; one `store.append` in
    `test_a_write_for_someone_elses_run_still_fails`; and the docstring of
    `test_no_store_call_runs_on_the_event_loop_thread_on_any_run_path`, as FR-81 requires.
  - `test_golden_eval.py`: two `PostgresTrace.reconstruct` reads. `test_persistence.py`: one
    `reconstruct` and one `get_run`.

## What the author ran

1. **Pre-flight** (KNOWLEDGE-0ca63339): baseline at `252c51a`, clean tree: 1504 passed in 283.97 s.
2. **The fixture against the unchanged suite:** 38 failed, 37 errors in 315.83 s, analysed in
   DECISION-e4fe2bc5.
3. **Gate file:** 35 passed. **Full suite:** 1539 passed (1504 before, plus the 35).
4. **Demo command:** `scripts/16_subagents.py --offline`, 7 of 7 checks pass.
5. **Mutation run: 13 of 13 killed**, each restored by SHA-256: the checkout never checking the
   loop; the switch ignored; logging on every call; one store method bypassing `_checkout`; the
   pool's history read on the loop; the Postgres artifact `metadata` read on the loop (C4's
   case); depth 0 accepted; a bool depth accepted; the pre-flight depth reading restored; any
   briefing schema accepted; no briefed inputs sent; no data marker; the example back on a
   substring.
6. **Gates.** Both green on source hash `d5c75afd`, the hash KICKOFF.md records, from one
   `genesis gate --timeout 590000` run. A first run without `--timeout` was cut off at 120 s and
   recorded regression:fail (SIGTERM); the store was checked afterwards and held nothing from it.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_validation_and_guard.py -q`, exit 0,
     35 passed in 3.48s (07:57:16 to 07:57:20 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1539 passed in 237.90s
     (07:57:20 to 08:01:20 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts in a folder `m18a-r1-probes` in your own session's scratchpad, with
shared cleanup in one `common.py` and tenant ids starting `SYN-m18a-r1`.

1. **FR-82 on every store path**, by thread identity, not by clock: every method that reaches
   `_checkout`, sync and async, from a run, a hook, a tool, a pool spawn, a cancelled run and a
   refused one. Is there any Postgres I/O in `agentsdk` that does not pass through `_checkout`?
2. **FR-82's refusal inside a run** on each path where a store call can happen, including the
   terminal write and a cancellation: does every one end as a recorded failed (or cancelled) run,
   and does any leave a run `running` or a row half-written?
3. **FR-81's fixture**: try to get an on-loop checkout past it -- a thread with its own loop, a
   checkout made during fixture setup or teardown, a test that replaces `postgres._pool` itself,
   a store reached through `close_pools` or a fresh DSN. Then check whether any of the test edits
   changed what a test asserts.
4. **FR-79 and FR-80 as classes**, and the other side: does anything valid now get refused?
5. **FR-83 and FR-84**: can a briefed input's content reach request metadata, can an entry be
   missing on any request of the child (including the FR-71 re-ask), and does the marker's
   delimiter survive an input that contains it?
6. **The example**, checked against what it prints and against a clean label.
7. **What M18a does not cover**: scripts run as subprocesses by `test_distribution.py` are outside
   the fixture; say whether that matters.

## Declared limitations: known, recorded, NOT findings

- **The FR-84 marker can be imitated** by an input that contains the delimiter text. The marker is
  signalling only, as FR-84 says; the manifest entry is what policy reads (ADR-17).
- **The switch is process-wide**, not per `Persistence` (DECISION-53ee27f6).
- **`briefed_inputs` and `depth` are caller-trusted** like `budget_lease`: application code could
  set them, with no ancestry check (D13).
- **Scripts started as subprocesses** by tests run without the fixture and with the switch off.
- Everything declared for M18 (D11, M3 to M8, round 1's caveats) still stands; C5 stays outside
  NFR-16 scope (DECISION-938f4485).

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T`).
- **Clean up** every run, row, temp folder and process you create, and say what you removed.
- **Report every probe**, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M18a-validation-and-guard --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M18a-validation-and-guard --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.
