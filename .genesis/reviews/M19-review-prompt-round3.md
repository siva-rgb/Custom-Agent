You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model: this round is for Fable 5.1**, in a fresh session. M19 was implemented by
Opus 5.5. Round 1 was reviewed by Sonnet 5 and round 2 by Opus 5, each standing in for Fable 5.1,
which is the least recently exposed to this code. If you have reviewed M19 before, say so and ask
for a different session. Re-derive the requirements from SPEC.md
("#### M19") rather than inheriting the framing below.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required: graphizer shells out to python3, and without the venv first on PATH
that hits the Microsoft Store alias and crashes Node.

- **Configuration:** both gates need DATABASE_URL from .env; the regression gate also needs
  BASE_URL and MODEL_API_KEY. **Never print credentials or a connection string.**
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`. M19 adds no library (NFR-21).
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's ids at its start and end (AC-44, AC-63), the development database is shared,
and M14's timing tests assert lower bounds. Before running tests or gates, check that no other
python or node process is running. Run your own probes with `-p no:cacheprovider`: Genesis hashes
.pytest_cache, so a cache write can make a gate read stale with no code change. The full suite
takes about 5 minutes; keep every command in the foreground and under 590 s. **`genesis gate` kills
a gate after 120 s unless you pass `--timeout 590000`**, and on this host that kills only the venv
launcher (KNOWLEDGE-64fa6e18): run `genesis gate . M19-orchestrator --timeout 590000`, then check
for a stray python process. **A probe that can hang** (a mutant that stops cancellation, say) must
run under its own timeout with `taskkill /T /F` on the whole tree, and be followed by a search for
mutation debris and a store residue check: the author's first mutation run hung exactly this way.

**The FR-81 fixture is active in every test.** A store call made from a coroutine on the event
loop fails the test at teardown, so read the store from async probes with `asyncio.to_thread`.

**If the suite hangs, it is not memory** (KNOWLEDGE-9a3abf61). Suspect the two live-gateway tests
in `test_golden_eval.py`; rerun with `-o faulthandler_timeout=120`.

## Task under review

**M19-orchestrator**, **round 3**: an Orchestrator executes a PlanVersion over its DAG with
deterministic acceptance criteria, guarded automatic replanning that never reruns a completed
side-effecting node, and tools that reach their run through RunScope.

### Owner decisions and readings that shape it

Read these before the code:

- **DECISION-c8d0932a** (pre-flight, owner): the orchestrator is one Runner run at depth 0 whose
  agent has one `run_plan` tool; a done node's output is stored as an artifact and briefed to its
  dependents; every node failure replans except `budget_exceeded` and cancellation; one
  `BudgetGovernor` per run re-splits what is left on a replan.
- **KNOWLEDGE-99cf7886** (pre-flight readings, not put to the owner). Two of them changed during
  the build, and the change is the author's, so judge it:
  - **`artifact_exists`**: the pre-flight reading required the artifact to have been written by
    the node. Artifact uris are derived from their ids (`urn:agentsdk:artifact:<id>`), so a planner
    can never name an artifact a node has yet to write. As built, the criterion passes when the uri
    resolves to an artifact in the run's scope whose content verifies against its recorded
    `content_hash` (FR-55); missing and tampered both fail.
  - **FR-76's scope**: the reading said RunScope gains optional fields. RunScope checks every one of
    its fields for storability (DECISION-53461588), and an artifact store is not a value, so
    `ToolScope` is a subclass of `RunScope`. A tool declaring a `RunScope` parameter receives a
    `ToolScope`. It also carries underscored fields (`_events`, `_control`, `_runner`, `_lease`) that
    only the plan tool uses. Decide whether exposing those to every scoped tool is acceptable.
  - Unchanged: roles map `assigned_role` to an AgentSpec, an unknown role failing the node; a node's
    `timeout` and `retry_policy` stay stored with no behaviour.
- **DECISION-6d073ac0**: M16's F2 and F4, owed before anything drives nodes, are fixed here.

### Rounds 1 and 2, and what changed

| round | reviewer | finding | decision |
|---|---|---|---|
| 1 | Sonnet 5 | D1: a failed plan ended the run completed (only CancelledError out of a version set the run unresolved). D2: a failed `tool_succeeds` criterion's text reached the report without its label. D3: a rerun that failed reported the previous version's answer under the new child's clean label. D4 (low): a retired budget record keyed `node@vN` could collide with a planner's node id | DECISION-8d125c3c |
| 2 | Opus 5 | D5: a done side-effecting node was rerun when one version omitted it and a later one named it again -- the run remembered one version deep. Also: `artifact_exists` accepted any artifact of the tenant and project, not the run's | DECISION-aab73fd5 |

Read both decisions. Round 1's repairs, each with a test written first that failed on round 1's code:

- **D1, made total rather than patched on one path.** `RunConfig.pending_failure` (new, set by
  `Orchestrator.config()`) makes an orchestrator run unresolved from its first turn, so a run that
  never completes a plan version cannot end completed -- including one whose model never calls
  `run_plan`, or whose every document is refused by the executor's schema validation before the
  tool runs. `run_plan` marks the run unresolved on entry and on any exception; only a version that
  comes back done clears it. A node is total: a failure inside one node, an unstorable output
  included (`output_not_stored`), fails that node and leaves its siblings running.
- **D2 and D3, repaired at their common cause.** The report and its `taken_in` are now built from
  one record: every label of what a node's report says -- its child's provenance whatever its
  status, and a criterion tool's provenance whenever its text is shown -- and an answer only for a
  node done in this version, stored with its own label. `run.provenance` is gone.
- **D4.** `adopt` checks before it moves anything, gives a retired record the first free key,
  never retires a kept record twice, and refuses a new node whose id is a kept record.

Round 2's repairs, each with a test written first that failed on round 2's code:

- **D5.** The run keeps a record of every node done in any version -- the version, its
  definition then, and whether it had side effects -- written in a `finally`, so a node done
  before a raise or a cancellation is remembered. `_carried` reads that record, not the version
  before: a node that had side effects is carried whenever it reappears, however many versions
  later; a pure node is carried when unchanged since it was done.
- **`artifact_exists` is run-scoped.** The artifact's `source_run` must be this orchestrator run or
  one of its child runs before its hash is checked. Because a planner cannot know a uri before it
  is written, each done node's report now names its output's uri (`artifact`), so a replan can
  require it; AC-59's test was rewritten to that shape.

**This is the M18 shape three times over** -- a property stated of every path and enforced on the paths
someone looked at. Do not stop at D1 to D4: look for any other way a run, a node or a label can
end up where these repairs say it cannot.

### Where to attack first

1. **The run is never a silent success, and never left running.** A failed plan version sets
   `RunControl.failure`; the loop ends the run `failed` with it at the model's final answer unless a
   later version succeeded. Try every way a run with a failed plan could end `completed`, and every
   way an orchestrated run, or one of its children, could be left `running` (cancellation during a
   node, during a criterion check, during a replan, during the output artifact write).
2. **Taint along every edge.** A node's output carries its child's provenance into the artifact
   that dependents are briefed with (FR-70, FR-83), and into the `run_plan` result through the new
   `ToolOutput.taken_in`, which may only raise taint and lower trust. Look for any path where text
   reaches a model, a dependent or the orchestrator with a label cleaner than its source: a carried
   node, a criterion's tool result, a skipped node, a replan.
3. **The budget across versions.** `BudgetGovernor.for_run` and `adopt` are new. The first plan must
   split exactly as the M17 constructor does; a replan must keep every spend inside the run ceiling
   and keep the unallocated reserve. Check the allocation still sums to the ceiling (M17 round 2,
   G2) after replans, carries and nodes that never started.
4. **FR-76 at the executor.** `_with_scope` injects the scope by parameter annotation. Can a model
   supply or forge it, can a tool without one see a change, does any `schema_hash` move, and does
   attribution hold for every way a tool can write an artifact?

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-73:** `Orchestrator` executes a `PlanVersion` over its DAG: a node becomes `ready` when every dependency is `done`, ready nodes run concurrently within `max_concurrent_subagents`, and queueing is FIFO. Cancellation reaches children: cancelling the run cancels every running child and awaits it, and no child is left running once the parent has returned or raised (NFR-17, FR-51). A node that fails does not cancel its siblings; nodes that depend on it become `skipped` unless replanning (FR-75) supersedes them. The orchestrator itself is an agent run with its own reservation, so its planning calls are counted, not free.
- **FR-74:** A node is `done` only when its execution terminated successfully, its output validated against `expected_output_schema` when one is set, and every acceptance criterion is satisfied (P2-D16). This increment evaluates three deterministic kinds: `output_schema`, already covered by FR-71; `artifact_exists`, naming an artifact whose `content_hash` must match what the node recorded (M13); and `tool_succeeds`, naming a registered tool and arguments that must return a non-error outcome, executed through the normal executor with the node's permissions. A `critic` criterion parses and stores, so a plan written now stays valid when Phase 3 builds the critic, but a run that reaches one ends that node `failed` with reason `criterion_not_available`, naming it. A criterion that is not satisfied fails the node with `acceptance_criterion_failed`, naming which.
- **FR-75:** Replanning is automatic while the run's budget remains, the replan count is below `max_replans`, and the failure is replannable (P2-D18). It produces a new `PlanVersion` whose `parent_plan` is the version that failed, inside the same run and the same ceiling. A node already `done` and `side_effecting=True` is carried into the new version as `done` and is never rerun; a `done` node with `side_effecting=False` may be rerun. When the budget is spent, or `max_replans` is reached, the run ends `failed` carrying the last plan version, not a silent success. `scripts/17_orchestrator.py` runs a four-node plan in which one node fails its acceptance criterion, prints the replan and shows that the completed side-effecting node was not rerun.
- **FR-76:** A tool reaches its run through an explicit `RunScope` argument (P2-D26), carrying `run_id`, `tenant_id`, `project_id`, the node id when one applies, and the artifact store bound to that run. A tool that declares no `RunScope` parameter is called exactly as before, so every existing tool and every built-in keeps working unchanged and no `schema_hash` moves. A tool that declares one receives it, and the artifact it writes through that scope is attributed to the run and node that wrote it.
- **NFR-21:** Phase 2's second increment adds no runtime dependency (NFR-6, NFR-13, NFR-18). The plan schema, the price table and the compaction summary all use what the SDK already carries.
- **NFR-22:** Orchestration never corrupts the record. Under parallel subagents, replanning and cancellation, every run's `sequence_no` values stay unique and contiguous on both stores, no child is left `running` once its parent has returned or raised, every child's `parent_run_id` resolves to a run that exists, and a plan version is never mutated after it is stored (NFR-17).
- **AC-58:** A diamond-shaped plan runs its two independent nodes concurrently and its dependent node only after both are `done`, respecting `max_concurrent_subagents`; cancelling the parent mid-flight cancels and awaits every running child, leaves no run `running` on either store, and satisfies NFR-22.
- **AC-59:** Each deterministic criterion is exercised both ways: `output_schema` passing and failing, `artifact_exists` with a matching and a mismatched `content_hash`, and `tool_succeeds` with a succeeding and a failing tool; each failure ends the node `acceptance_criterion_failed` naming the criterion. A plan carrying a `critic` criterion stores and reads back, and reaching it ends the node `criterion_not_available`.
- **AC-60:** A plan whose node fails its criterion replans within budget, produces a new `PlanVersion` whose `parent_plan` is the failed one, and carries a completed `side_effecting=True` node forward as `done` without rerunning it, proven by a counter in the tool that node used. With `max_replans` reached, and separately with the budget spent, the run ends `failed` carrying its last plan version.
- **AC-63:** A full regression run still leaves AC-44's invariant intact with orchestration in place: the set of `run_id`s in `runs`, `messages`, `run_events`, `execution_manifests`, `artifacts`, `plan_versions` and `plan_node_states` is unchanged from before the run.

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve, validate, permission, execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else. (The Orchestrator supplies a tool, an
  agent and a RunConfig; the application still calls `runner.run`. Check that this holds.)
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.
- A cancellation is recorded, then re-raised (INVARIANT-73299c7c).

### What changed

**M19 is not committed.** HEAD is `7af8202` (M18a). Review it with:

```bash
git diff 7af8202 -- agentsdk tests scripts README.md
git status --short
```

- `agentsdk/orchestrator.py` (new): `Orchestrator`, the `run_plan` tool, the scheduler, criteria,
  the report.
- `agentsdk/scope.py` (new): `ToolScope`, `AttributedArtifacts`.
- `agentsdk/budget.py`: `for_run`, `adopt`; the per-node grant split out of `_split_*` unchanged.
- `agentsdk/plan.py`, `agentsdk/postgres.py`: F2 (`refuse_foreign_sink`, sinks gain `scope`), F4
  (the emit inside the transaction), a `reason` on transitions, `source_task` on artifacts.
- `agentsdk/executor.py`, `agentsdk/tools.py`, `agentsdk/primitives.py`: scope injection,
  `ToolOutput.taken_in`, `ContentProvenance.taking_in`.
- `agentsdk/api.py`, `agentsdk/loop.py`, `agentsdk/handle.py`, `agentsdk/subagents.py`,
  `agentsdk/events.py`, `agentsdk/artifacts.py`, `agentsdk/__init__.py`: `RunConfig.node_id`, the
  per-run `ToolScope`, the Runner's in-memory stores, `RunControl.failure`.
- `scripts/17_orchestrator.py` (new), `README.md` (the orchestration row and the example row).
- `tests/test_orchestrator.py` (new, the gate file).
- **Edits to approved tests:** one. `tests/test_distribution.py` adds `17_orchestrator.py` to
  `EXPECTED_EXAMPLES`, as every example milestone has (FR-56, DECISION-40ae2d24).
- **Not changed, stated:** the README's budget and structured-output rows were already stale before
  M19 and are left as they were.

## What the author ran

1. **Round 2's findings first:** two D5 tests (the reviewer's own shape -- a version omitting the
   done node, then one naming it again -- and the same for a pure node) and the rewritten AC-59
   test, all failing on round 2's code, then the repairs. A third D5 test, a version that raises
   after its side-effecting node finished, was added when the mutation run showed nothing killed
   recording the done node only on a version's normal return.
2. **Gate file:** 47 passed. **Full suite:** 1586 passed (1583 at round 2, plus the 3 new tests).
3. **Mutation run:** **38 of 38 killed** on the final code, each restored by SHA-256: rounds 1 and 2's
   31 (four re-pointed to lines the repair rewrote) and seven for round 2's repairs (the record one
   version deep; nothing recorded; recorded only when a version returns; a pure node never
   carried; any run's artifact accepted; children not recorded; no artifact uri in the report).
   The first full run left one survivor, "recorded only when a version returns", which led to
   the third D5 test; the rerun with it killed all 38.
4. **Demo command:** `scripts/17_orchestrator.py --offline` 5 of 5, and live 4 of 4, read back from
   Postgres (completed, 5 children, versions 1 and 2 with parent 1, 22 contiguous events, one
   output artifact per node), then removed by its own run id after checking the count (6 runs).
   example-tenant is at 19 runs and 6 artifacts, no orphans.
5. **Gates.** Both green on source hash `86a352b2`, the hash KICKOFF.md records, from one
   `genesis gate --timeout 590000` run.
   - `unit`: `.venv\Scripts\python.exe -m pytest tests/test_orchestrator.py -q`, exit 0, 47 passed
     in 8.00s (12:58:01 to 12:58:10 UTC).
   - `regression`: `.venv\Scripts\python.exe -m pytest -q`, exit 0, 1586 passed in 243.25s
     (12:58:10 to 13:02:14 UTC).

## Attack these first

**Report a result for every item, `probed: <what you ran and what it showed>` or `not probed:
<why>`.** Save probe scripts in a folder `m19-r3-probes` in your own session's scratchpad, with
shared cleanup in one `common.py` and tenant ids starting `SYN-m19r3`.

1. **Endings**: every way an orchestrated run ends, on both stores, against its stored row, its
   events and its children's rows (attack item 1 above).
2. **Taint** on every edge (item 2).
3. **Budget** across versions, including a node that never starts and a carried node (item 3).
4. **Criteria**, each both ways, plus what happens when a criterion's own check raises, hangs or is
   cancelled.
5. **FR-76** (item 4), and whether `AttributedArtifacts` lets a tool read or delete another node's
   artifacts.
6. **F2 and F4**, on both stores, with sinks that misreport their scope.
7. **The demo command**, offline and live, checked against the stores rather than its printout.

## Declared limitations: known, recorded, NOT findings

- **A node's `timeout` and `retry_policy`** are stored and not acted on (KNOWLEDGE-99cf7886).
- **The plan tool's report reaches the orchestrator model as text**, with every done node's answer.
  Its taint is carried by `taken_in`; scanning the text for instructions is I-03, deferred by
  P2-D25.
- **A node is `running` from when its task starts**, which may be while it waits for a
  `max_concurrent_subagents` slot inside the pool.
- **A replan cannot reuse a node id for an unrelated node** without the carry rule applying to it.
- **Orchestrator state is held in the Orchestrator object** (`plans(run_id)`); the stores hold every
  version and node state, which is the record.
- **Round 1 and 2 caveats, kept in view and not repaired (DECISION-aab73fd5):** a tool that declares
  a RunScope parameter receives the Runner through `ToolScope._runner`, so it can reach **any
  tenant's** artifact store -- in any run, orchestrated or not -- and set `_control.failure`. It is
  in-process code the application wrote (D13) and could build a store directly, but it is wider
  than NFR-2's "multi-tenant by construction" suggests, and a narrower tool scope type should
  replace the underscored fields. `AttributedArtifacts` scopes writes, not reads or deletes.
- **`pending_failure` is caller-settable** on any RunConfig, like `budget_lease` (D13), and a
  hand-built RunConfig without it can reach `run_plan` and end completed with no plan; reachable
  only by bypassing `Orchestrator.config()`.
- Everything declared for M18 and M18a still stands (KNOWLEDGE-8bde2cc5).

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
  control approve . M19-orchestrator --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M19-orchestrator --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each of the seven attack items, and
your verdict.
