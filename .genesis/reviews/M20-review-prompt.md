You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh session: this round is for Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M20 was implemented by Opus 5.5. Sonnet 5 reviewed M19's last round, so it is the most
recently exposed to the code M20 builds on. Bring no memory of an earlier review into this one: if
your session holds any, say so and ask for a fresh one. Re-derive the requirements from SPEC.md
("#### M20") rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M20 adds no library (NFR-21).
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, AC-63), the development database is shared,
and M14's timing tests assert lower bounds. Before running tests or gates, check that no other
python or node process is running. Run your own probes with `-p no:cacheprovider`: Genesis hashes
.pytest_cache, so a cache write can make a gate read stale with no code change. The full suite
takes about 5 minutes; keep every command in the foreground and under 590 s. **`genesis gate` kills
a gate after 120 s unless you pass `--timeout 590000`**, and on this host that kills only the venv
launcher (KNOWLEDGE-64fa6e18): run `genesis gate . M20-context-policy-and-compaction --timeout 590000`,
then check for a stray python process. **A probe that can hang** must run under its own timeout with
`taskkill /T /F` on the whole tree, and be followed by a search for mutation debris and a store
residue check. **Restore a mutated file byte for byte**, line endings included, and verify its
SHA-256: several files here are CRLF in the working copy and some are untracked, so git cannot
catch a bad restore (KNOWLEDGE-ae83f412; M19 round 4 caught one this way).

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`.

**If the suite hangs, it is not memory** (KNOWLEDGE-9a3abf61). Suspect the two live-gateway tests
in `test_golden_eval.py`; rerun with `-o faulthandler_timeout=120`.

## Task under review

**M20-context-policy-and-compaction**, **round 1**: an agent sees only the tools it may execute,
recorded in the manifest, and long histories compact at a threshold with taint and provenance kept
and every compaction recorded. It is the last milestone of Phase 2's second increment.

### Owner decisions and readings that shape it

Read these before the code:

- **DECISION-468e2bfa** (pre-flight, owner):
  1. **Which runs:** `RunConfig.context_policy`, None by default. `Orchestrator.config()` sets
     one and the `SubagentPool` carries it to every child; a plain run sends exactly what it sent
     before M20 (NFR-12, NFR-15) and DECISION-ca1ad3e0 keeps governing it.
  2. **The threshold measure:** the provider's `prompt_tokens` for the last request sent, plus a
     characters/4 estimate of the messages added since; no tokenizer (NFR-21).
  3. **An unknown window:** the policy's `context_window`, else the ModelRegistry's
     `max_context_tokens`; a compacting policy on a model with neither is a configuration error
     where the run is configured, naming the model (P2-D20's USD precedent).
  4. **What is replaced:** the instructions, the task and the latest turns stay whole, a call never
     separated from its results; the middle becomes one summary. Stored messages are never
     rewritten: the summary is appended as a marked message and requests are built from the view.
- **KNOWLEDGE-1545435a** (pre-flight readings, not put to the owner). Judge each:
  (a) a call to a tool the agent cannot see is refused ToolNotFound, because the run's executor
  resolves against the filtered registry; (b) migration `0009` adds `execution_manifests.tools_sent`;
  `tool_spec_hashes` is unchanged; (c) the briefing rules move from `SubagentPool._brief` into
  `ContextPolicy.brief`, unchanged; (d) the summarising call uses the run's model, passes the same
  budget check, is charged to the agent's lease and emits `ModelCalled`; (e) the summary's provenance
  is `ContentProvenance.from_model` over every replaced tool result, the replaced summary and the
  briefed inputs; (f) **a compaction that cannot complete ends the run failed** -- the alternative
  was to run on toward an overflow; (g) example 18 joins `EXPECTED_EXAMPLES`.
- **Carried from M19 (KNOWLEDGE-75464135):** its caveats stand, and the scope and orchestrator code
  M20 now drives are as M19's round 5 approved them.

### Where to attack first

1. **Visibility is enforced, not just displayed.** Under a policy, can an agent execute a tool it
   was not sent -- by naming it, through a criterion, through a child, through a hook, through a
   tool that is in its profile but not registered? Is the refusal exactly an unknown tool's? Does a
   run *without* a policy send precisely what it sent before M20, byte for byte?
2. **The manifest tells the truth.** Do two runs with the same `tools_sent` always have seen the
   same tools? What does it record when a `before_model` hook rewrites `request.tools`?
3. **Compaction never launders.** Look for any path where the summary, a later summary, or a
   request built after a compaction carries a cleaner label than what it stands for: briefed
   inputs, error results, a summary replacing a summary, a hook-modified response.
4. **The record stays whole.** NFR-3: every stored message survives; the artifact holds exactly
   what was replaced and reads back against its hash; `ContextCompacted` names it; the run's usage
   still equals the sum of its `ModelCalled` events (NFR-17); `sequence_no` stays contiguous.
5. **The view.** The request after a compaction must be well formed for an OpenAI-compatible
   provider: every tool result preceded by its call, the task first, one summary. Try parallel
   calls, a re-ask (FR-71), a second and third compaction, a turn larger than the window.
6. **The summarising call is an ordinary call.** Budget refusal, a model error, an empty answer,
   cancellation in flight (P2-D7: the cost becomes unknown), the provider slot, the turn limit.

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-77:** `ContextPolicy` decides what an agent sees, and tool visibility follows execution (P2-D23): an agent is sent only the schemas of the tools its `tool_profile` permits it to execute, reversing DECISION-ca1ad3e0 for orchestrated runs. A call to a tool the agent cannot see is refused exactly as an unknown tool is today. Because two runs with the same manifest must have seen the same tools, the `ExecutionManifest` records the qualified names and `schema_hash` values actually sent, and migration `0009` adds that column, NULL for rows written before it, idempotent per FR-17. The policy is also the single place that decides what of a parent's history a child sees (FR-70), so briefing rules are readable in one type rather than spread through the orchestrator.
- **FR-78:** `ContextCompactor` compacts an agent's history when the assembled prompt passes a configurable fraction of the model's context window, default 0.75 (P2-D24), a separate type from `ContextAssembler` (Phase 0) and `ContextPolicy`. The summary it produces inherits the maximum taint of everything it summarises and keeps the provenance of every source it names, so compaction never launders a tainted input into an untainted summary. Every compaction is recorded as an artifact holding what was replaced and as a `ContextCompacted` run event naming that artifact, the token counts before and after, and the model call that produced the summary, which is charged to the agent's own reservation. A run that has been compacted therefore remains auditable end to end. `scripts/18_context.py` runs a long agent past the threshold and prints the visible tool set, the compaction event and the artifact it wrote.
- **AC-61:** A subagent whose `tool_profile` permits two of five registered tools receives exactly those two schemas in its `ModelRequest`, a call to one of the other three is refused as an unknown tool is, and the `ExecutionManifest` records the qualified names and `schema_hash` values that were sent. Applying migration `0009` twice changes nothing.
- **AC-62:** An agent driven past 0.75 of its context window compacts: the assembled prompt afterwards is smaller, the summary carries the maximum taint of the messages it replaced and their provenance, a `ContextCompacted` event names the artifact holding what was replaced, that artifact reads back with a matching `content_hash`, and the summarising call is charged to the agent's own reservation.
- **NFR-21:** Phase 2's second increment adds no runtime dependency (NFR-6, NFR-13, NFR-18). The plan schema, the price table and the compaction summary all use what the SDK already carries.

NFR-12, NFR-15 and NFR-17 constrain it as well; read them in SPEC.md.

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

**M20 is not committed.** HEAD is `bf7d0d5` (M19). Review it with:

```bash
git diff bf7d0d5 -- agentsdk tests scripts README.md
git status --short
```

- `agentsdk/context_policy.py` (new): `ContextPolicy` -- validation, `visible()`, `window_for()`,
  and `brief()`, moved from `SubagentPool._brief` unchanged.
- `agentsdk/compaction.py` (new): `ContextCompactor` -- the view, the measure, `split()`, the
  summary's provenance and sources, the summarising request, the artifact body.
- `agentsdk/loop.py`: the compaction step before each request; `_compact()`; the `ModelCalled`
  payload moved into `_model_called()` so the summarising call records the same shape, plus
  `"purpose": "compaction"`.
- `agentsdk/context.py`: `summaries` on `build()`, a `compaction_summary` provenance entry.
- `agentsdk/api.py`: `RunConfig.context_policy`; the window refused in `_open`; the visible
  registry for the executor and the loop; `tools_sent` in the manifest; the loop's compactor.
- `agentsdk/manifest.py`, `agentsdk/postgres.py`, `agentsdk/migrations/0009_tools_sent.sql`:
  `tools_sent`.
- `agentsdk/events.py`: `EventType.CONTEXT_COMPACTED` (`run_events.event_type` has no constraint).
- `agentsdk/subagents.py`: the pool's `context_policy`, carried to each child; `_brief` removed.
- `agentsdk/orchestrator.py`: `context_policy` (default `ContextPolicy()`), on its own run and its pool.
- `agentsdk/__init__.py`: exports `ContextPolicy`, `ContextCompactor`.
- `scripts/18_context.py` (new); `scripts/17_orchestrator.py` (an explicit window for its scripted
  offline model, and a comment M20 made untrue); `README.md` (the compaction row, the
  `tool_profile` paragraph, the example 18 row).
- `tests/test_context_policy.py` (new, the gate file, 42 tests).
- **Edits to approved tests:** two, both listed for you to judge.
  - `tests/test_orchestrator.py`'s `setup()` passes `ContextPolicy(context_window=1_000_000)` to
    its `Orchestrator`. The orchestrator's default policy now compacts, its fake models are in no
    ModelRegistry, and decision 3 makes that a configuration error. No test there comes near the
    window. One line and its import.
  - `tests/test_distribution.py` adds `18_context.py` to `EXPECTED_EXAMPLES`, as every example
    milestone has (FR-56).
- **Not changed, stated:** `tests/test_phase2_readiness.py`'s docstring still names `_brief`, which
  is now `ContextPolicy.brief`; it is an approved test and the wording is left as it was.

## What the author ran

1. **The gate file:** 42 tests, covering AC-61 (in memory, and on Postgres through an orchestrator
   whose child is permitted two of five tools), migration `0009` on a scratch schema, the window
   rules, AC-62 on both stores, every request keeping calls with results, a later compaction
   replacing an earlier summary, the measure both ways (a large result caught before it is sent;
   the provider's count deciding when the text alone would not), every failure path (model error,
   empty summary, budget refusal, artifact refusal), cancellation in flight on both stores with the
   cost becoming unknown, a priced model with the registry's window, and the example offline.
2. **Mutation run:** **33 of 33 killed**, each restored by SHA-256 and checked byte-identical. The
   first run left one survivor -- dropping the replaced summary's taint -- because the test's later
   compaction still replaced a tainted page as well. The test now finds a compaction whose only
   tainted source is the summary before it, asserts that premise, and the rerun killed all 33.
3. **Demo command:** `scripts/18_context.py --offline` 6 of 6 and **live 6 of 6**. The first live
   run did not compact: the model read all five pages in parallel, in one turn, and a turn is never
   split (see the limitations). The example now chains the pages by key, so they are read one per
   turn. The live run was read back from Postgres -- completed, two `ContextCompacted` events, eight
   `ModelCalled` (six agent, two summarising), contiguous events, `tools_sent` naming only
   `read_page`, two untrusted JSON artifacts, all 14 messages stored with the two summaries among
   them -- and both live reader runs were removed by their own run ids. `scripts/17_orchestrator.py`
   live 4 of 4: each child's manifest names only its own tool (`gate_open`, `save_note`) and the
   orchestrator's only `run_plan`; removed by its run id (6 runs). example-tenant is at 19 runs and 6
   artifacts, 0 runs `running`, no orphans.
4. **Gates.** Both green on source hash `eae1be3a`, from one `genesis gate --timeout 590000` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_context_policy.py -q`, exit 0, 42 passed
     in 3.53s (15:01:30 to 15:01:34 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1632 passed in 245.79s
     (15:01:34 to 15:05:41 UTC). The baseline before M20 was 1590.

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts in a folder `m20-r1-probes` in your own session's scratchpad, with
shared cleanup in one `common.py` and tenant ids starting `SYN-m20r1`. Delete only rows your probes
wrote, by the run ids they recorded.

1. **Visibility** (item 1 above), in memory and on Postgres, including through the orchestrator
   and a criterion's `tool_succeeds`.
2. **The manifest** (item 2), and a plain run's request payload against HEAD's, byte for byte.
3. **Taint through compaction** (item 3), with a probe that does not choose which results are
   replaced (KNOWLEDGE-14f3ec8b): vary the script and check the property on every compaction.
4. **The record** (item 4) on both stores.
5. **The view** (item 5), and the measure: is the threshold crossed when the spec says it is?
6. **The summarising call** (item 6), cancellation included.
7. **The demo command**, offline and live, checked against the store rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **A turn is never split.** A single turn larger than the window -- several large results from
  parallel calls in one response -- cannot be compacted, and the run meets the provider's own limit.
  The first live run of example 18 is that case.
- **The measure is an estimate** between provider reports: characters/4 for what was added since.
  `tokens_before` and `tokens_after` in `ContextCompacted` are that measure, not a tokenizer's count.
- **The summarising call does not pass through `before_model` or `after_model`.** It is recorded,
  priced and charged like any call, but a hook cannot see, rewrite or halt it.
- **`tools_sent` records what the policy sends**, not what a `before_model` hook may substitute.
- **The view lives in the loop**, for the run's life in one process; nothing resumes a run before
  Phase 6. The stored history holds every message and every summary, so the view can be rebuilt.
- **The summary is stored as a user-role message**, marked as data (FR-84's form), since `Message`
  has no provenance field; its taint rides in the provenance manifest.
- **`sources` lists tool results and the replaced summary**, the things that carry provenance. The
  briefed inputs are folded into the summary's provenance but not listed: they stay in the task.
- **At most one compaction per turn.** If the kept turns alone still pass the threshold, the run
  goes on.
- **A criterion's `tool_succeeds`** runs against the Runner's whole registry with the node's own
  permissions, as M19 built it: visibility is about what a model is sent.
- Everything declared for M19 still stands (KNOWLEDGE-75464135), among them that a `RunScope | None`
  annotation receives no scope and that `ToolScope` exposes the Runner to every scoped tool.

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
  control approve . M20-context-policy-and-compaction --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M20-context-policy-and-compaction --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.
