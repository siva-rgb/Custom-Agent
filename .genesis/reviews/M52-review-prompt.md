You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M52, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M52 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet 5.5 subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M52 opens Phase 5A increment 2, the model-driven autonomous mode: the general agent hands a piece of
its task to a general subagent through a new tool, `delegate_task`. Each call reserves a share of the
parent's remaining budget for the child, copies the files it names into the child's fresh workspace,
runs the child one level deep, and returns its answer. It adds example 44.

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table

| Decision | P5A-D15 to P5A-D22 |
|---|---|
| P5A-D15 Child budget | Each spawn reserves a fixed share of its parent's remaining reservation, a `BudgetPolicy` default, nested as FR-68 states. What the child does not spend returns to the parent |
| P5A-D16 Child files | The call names paths in the parent's workspace. They are copied into the child's fresh workspace through the checked tar (FR-175's `files`) and recorded by SHA-256. The child's answer returns as text |
| P5A-D17 Nesting | One level. A child has no delegation tool, so hub and spoke holds, and the overshoot stays bounded by `max_concurrent_subagents` |
| P5A-D18 Approval | A spawn is `auto` by default, like `run_command`: the budget, the sandbox and the depth do the containing. A caller may require approval |
| P5A-D19 The call | `delegate_task` blocks until its child ends and returns the child's answer. Calls in one response run concurrently, up to `max_concurrent_subagents` |
| P5A-D20 Records | No migration. A child is a run linked by `parent_run_id`, and its reservation is named in its own manifest's `budget_reservations`, since a manifest is written once, when its run starts (FR-11; corrected 2026-11-09, DECISION-8b115c86's follow-up) |
| P5A-D21 Eval | ADR-12 gains a `delegation` kind of about 6 tasks. The Claude arm is given its CLI's own subagent tool. Whether a run delegated is measured and reported, never required to pass |
| P5A-D22 Milestones | M52 the reservation and the tool (example 44); M53 the delegation tasks and the comparison rerun; M54 the exit signal (example 45) |

### "#### M52: child reservations and the delegation tool"

- FR-184: **Nested reservations** (P5A-D15, building FR-68's nesting).
  - **The share:** `BudgetPolicy.delegation_fraction`, a fraction in (0, 1), default 0.25.
  - **Reserving:** `BudgetGovernor.reserve_child(parent_node_id, child_id)` moves that share of the parent reservation's remainder, in each unit with a ceiling, into a new reservation `child_id`, and returns its lease. The remainder is the reservation minus what the parent has spent and what its open children hold.
  - **Refused:** `BudgetExceeded` when the share would be zero in a unit with a ceiling (below one token, or below 0.000001 USD), or when the parent's lease may not call (FR-68).
  - **Releasing:** a child's lease, when released, returns its unspent amount to its parent's reservation, never to the run's unallocated pool.
  - **What the parent sees:** the child's spend is charged to the child's reservation, and through it to the run's spend. The parent's own `may_call` sees only the parent's remainder, so a parent whose children hold its budget stops before it overspends.
  - **Overshoot:** at most one call or command per running agent, the parent included, as FR-68 states.
- FR-185: **The `delegate_task` tool** (P5A-D16, P5A-D17, P5A-D19).
  - **Arguments:** `objective` (a non-empty string) and `files`, a list of at most 64 relative paths in the parent's workspace, default empty.
  - **Where it comes from:** like the workspace tools (FR-146), it is added to a run whose spec has a workspace and names `delegate_task` in its `tool_profile`. The SDK's `RESERVED` names include it.
  - **Its configuration:** `AgentSpec.delegate`, a `Delegation` holding the child's `AgentSpec` (a `general_subagent` spec, or any spec with a workspace and without `delegate_task`), the child's `max_turns` (default 20), and the spawn's `approval` (default `AUTO`).
  - **Refused at construction, `ValueError`:** a `tool_profile` naming `delegate_task` without `delegate`; `delegate` without a workspace; and a child spec whose profile names `delegate_task`.
  - **Refused before the run starts, `ValueError`:** `delegate_task` on a run with no budget lease, or on a run the `SubagentPool` places, since a child has no delegation tool.
  - **One call:**
    1. The `files` are read from the parent's workspace through the provider's checked tar (FR-143): regular files only, FR-175's path, size and count bounds, counted against the child's `max_workspace_bytes`.
    2. A reservation is made (FR-184).
    3. The child runs through the parent's `SubagentPool` at depth 1, briefed with the objective and those files, in a fresh workspace started with them (FR-175). The child has the parent's tenant, project and principal.
    4. The child's lease is released when the child ends.
  - **What the parent receives:** the child's answer, its run id and its terminal status. The answer is labelled with the child's `SubagentResult.provenance` (ADR-26), so a child cannot launder taint.
  - **Refusals are tool errors, never run failures:** a path outside the workspace or past a bound, the pool's `max_tasks_per_run`, and a reservation refused by FR-184. A call refused before step 3 spawns nothing; only a call refused by step 1 or step 2 also takes no task count. A child that fails or ends `budget_exceeded` returns its status as the tool's result.
  - **Cancellation:** cancelling the parent cancels each running child, which ends `cancelled` with its record complete (FR-50) and its workspace released.
- FR-186: **The general agent delegates.**
  - **Turning it on:** `general_agent(..., delegate=True)` adds `delegate_task` to the preset's profile, with a `general_subagent` built from the preset's model, image, registry and egress as its child, and `delegate_approval` (default `AUTO`) as the spawn's approval.
  - **Instructions:** the preset's instructions gain a section on when to delegate, as a new version, `general-v5`. Without `delegate=True`, the profile and spec are unchanged, though the shipped instruction text is the new version.
  - **Requiring approval:** with `delegate_approval=REQUIRE_APPROVAL`, each spawn is an interruption the approval manager decides before step 3. A refusal returns a tool error and spawns nothing.
- FR-187: **Example 44** (`scripts/44_delegation.py`) runs the delegating preset offline (`FakeProvider`, a scripted model), on Docker, and live with the gateway model. Each check names its subject with a store witness and fails when the subject never happened (KNOWLEDGE-1afcf6b2, DECISION-a7d6a1f5):
  - **a delegated piece is solved by the child:** the child's `runs` row with the parent's id, its workspace holding only the named files by SHA-256, its commands, and its answer in the parent's tool result;
  - **the child's reservation:** the child's own manifest's `budget_reservations` name it, its `runs` row names the parent by `parent_run_id`, and the child's spend stays within it plus one call;
  - **a child cannot delegate:** a scripted child's `delegate_task` call is refused as an unknown tool;
  - **approval:** a spawn under `REQUIRE_APPROVAL` waits for its decision, and a refused one spawns no run.

### Non-functional

- NFR-53: A run that does not name `delegate_task` behaves exactly as it did before M52: the same requests, events, rows and manifest. So does an orchestrator's plan, whose governor gains `reserve_child` but none of whose reservations change. The only exception is the general preset's instruction version and its `agent_spec_hash` (FR-186). Increment 2 adds no migration and no runtime dependency (NFR-51).
- NFR-54: A spawn never widens what its parent may reach. A child has its parent's tenant, project and principal, egress no wider than its own spec's, no host path, and only the files its call named.

### Acceptance

- AC-110: FR-184 to FR-187, on both stores:
  - a child's reservation is `delegation_fraction` of its parent's remainder, and its unspent amount returns to the parent;
  - a parent whose children hold its budget makes no call past its own remainder;
  - each refusal of FR-185 at construction and before the run raises before anything is created;
  - a refused call returns a tool error, and one refused by its files or reservation spawns no run and takes no task count;
  - the child's workspace holds only the named files, recorded by SHA-256;
  - a child's answer carries its inputs' highest taint;
  - a child's `delegate_task` is refused;
  - cancelling the parent ends its children `cancelled`;
  - a spawn under `REQUIRE_APPROVAL` waits for its decision;
  - a run without `delegate_task` records what it did before M52;
  - example 44 holds every check offline, on Docker and live, and each of its checks fails without its subject.
- AC-111: FR-188 and FR-189: every delegation task's checker accepts its good outcome and rejects its bad and tricky ones; each row reports whether its run delegated and how many children it ran; the report covers every task three times on each arm, with the preset's final version.
- AC-112: FR-190, the exit signal: example 45 holds every check ten times on `DockerProvider`, its starts shuffled with random delays, and the gates hold it on `FakeProvider`. Each check fails without its subject: a test removes, in turn, the child's reservation check, the files bound, the child's lack of the delegation tool, the spawn's approval, the tenancy filter, and the cancellation of children, and each removal fails the check that names it.

## The records

All in `.genesis/project.json`. A choice they record is not a defect; code that differs from them is.
- **DECISION-8b115c86** (owner): the four choices of increment 2 -- a child's budget a fixed share of
  the parent's remainder, the call's named files copied into the child's fresh workspace, one level,
  spawns `auto` by default.
- **DECISION-764be0d8** (owner): the spec correction at M52's start -- a child's reservation is named
  in its own manifest, not its parent's, since a manifest is written once at its run's start.
- **DECISION-12da8165** (owner): M52's live spend capped at USD 1.
- **DECISION-83758f9e:** the live runs and the pre-review's findings, as below.
- Standing: KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's
  text), DECISION-a7d6a1f5 (no vacuous live check), NFR-43.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5a`; HEAD is `188ba43` (M51). M52 is uncommitted. Under `agentsdk/` it adds `delegation.py`
and changes `budget.py`, `api.py`, `presets.py` (instructions now `general-v5`) and
`workspace/tools.py`; no migration, no dependency.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** the gateway settings (`BASE_URL`, `MODEL_API_KEY`) and DATABASE_URL in `.env`.
  **Never print credentials or a connection string;** on a database error print the exception type
  only. The tests remove their rows (`SYN-m52-`); the author's live example-44 runs left theirs under
  tenant `examples`, projects `delegation-*`, as the examples' live runs do.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
  ES_DISPLAY_REQUIRED)`. Before calling any delay real, compare wall time with monotonic time.
  `.venv\Scripts\python.exe` is a launcher that starts the real interpreter as a child: killing it
  alone leaves the child running.
- **Docker Desktop is running.** **Never pull, build, prune, stop or restart anything.** You may run
  containers from `python@sha256:6d43704b...` with `--pull never`, through the code's own paths, and
  must remove each. Report how many containers labelled `agentsdk.tenant` remain at the end (it must
  be 0 of yours).
- **Line endings vary by file,** and a scripted edit can change them. A mutant whose edit silently
  never applies looks exactly like a surviving one: assert the file changed before running its test,
  and restore it byte for byte, verified by SHA-256. **The same holds for a runtime patch: count how
  often it fired, and treat zero as no result.**
- **Live:** the gateway may be used for a smoke run only, a few cents: `python scripts/44_delegation.py`
  costs under one cent.

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. Three python processes (PIDs 27472, 6348, 17824) are the owner's Jupyter kernel: leave them.
  Run with `-p no:cacheprovider`; `pytest.ini` deselects Docker tests unless you pass `-m docker`.
  **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M52-delegation-tool --timeout 7200000` in the background, kept awake:
  unit (`tests/test_delegation.py`, under a minute), regression (the full suite, about 30 minutes),
  and `docker` (every Phase 5 Docker-marked test plus example 44's, about 70 minutes).
- M52's own offline tests: `tests/test_delegation.py`, about 15 seconds.

## Task under review

### What M52 builds

- **Nested reservations** (FR-184): `agentsdk/budget.py` -- `BudgetPolicy.delegation_fraction`,
  `BudgetGovernor.reserve_child`, `BudgetLease.parent`, `_release` returning a child's unspent amount
  to its parent, `_may_call_locked`.
- **The tool** (FR-185): `agentsdk/delegation.py` -- `Delegation`, `named_files`, `child_spec`,
  `delegate_tool`; `agentsdk/api.py` -- `AgentSpec.delegate` and its construction checks,
  `_refuse_delegation` (from `Runner._open`), the tool's registration in `Runner._run`, and
  `delegation_fraction` in the manifest's `budget_policy` only on a delegating run;
  `agentsdk/workspace/tools.py` -- `RESERVED` names `delegate_task`.
- **The preset** (FR-186): `agentsdk/presets.py` -- `general_agent(..., delegate=..., delegate_approval=...)`,
  instructions `general-v5`.
- **Example 44** (FR-187): `scripts/44_delegation.py`; its README row.
- **Tests:** `tests/test_delegation.py` (new).

### The live work and the pre-review (DECISION-83758f9e)

- **Example 44 live**, four attempts on the gateway's default model, together under USD 0.01:
  1. The child's live model exported a file; the preset's export asks for approval, which a child
     inherits, and nothing answered, so the run waited. Stopped, its two rows removed by id. The
     example now refuses every export of checks 1 and 2, as the comparison does (pinned by a test).
  2. Check 1 failed: the child ran `python3 data/sum.py`, which names no file, though the program it
     wrote reads data/a.txt. The check now counts a command running a file the child wrote whose text
     names the file (pinned).
  3. Check 1 failed again: the child read data/a.txt with `read_workspace_file`, then ran a program
     holding the numbers. The check's clause demanded a command reading the file, narrower than
     FR-187's subject ("a delegated piece is solved by the child"); it now counts a read call of the
     named file as well (pinned, with a control that reads another file).
  4. 4 of 4 checks passed.
- **The pre-review** (Sonnet 5.5, runtime probes only):

| Finding | Repair |
|---|---|
| F1: a child's reservation was named `delegated:<tool-call id>`; a provider repeating ids across turns made every later call fail. | Named `delegated:<fresh uuid>`; a test with a provider that numbers calls per response. |
| F2: a hand-built child spec with a `scope="plan"` workspace shared the parent's workspace, files and all. | `Delegation` refuses a child workspace that is not `scope="run"` or starts from a snapshot (NFR-54). |
| N1: a hand-built child could have egress wider than its parent's. | A delegating `AgentSpec` refuses a child egress its own does not allow (NFR-54's "a spawn never widens what its parent may reach"). |
| N2: `agent_spec_hash` does not see the delegation. | Not changed: the delegation sits beside the hash as the workspace does (comment in `AgentSpec`); the profile naming the tool is hashed. |
| N3: a registered tool named `delegate_task` is now refused beside a workspace provider. | Not changed: FR-185 puts the name in `RESERVED`. |
| N4: a parent released before its child would take the child's return. | Not changed: unreachable through the tool, whose `finally` releases the child first; the allocation still sums. |

### How the author checked it

- **Tests:** 59 non-Docker and 2 Docker delegation tests; unit 59, regression 3794, docker 196.
- **Mutation:** 36 mutants, one exact edit each, every file restored byte for byte
  (appendix). Two survived the first run because no test reached their state (a parent at the run's
  ceiling while it still holds a remainder; a child's unspent amount returning to its parent); both
  were killed once tests reached them.
- **Older suites:** every file touching the changed modules, 748 passed and one expected failure
  (`test_budget.py` pins `BudgetPolicy`'s fields), then the full suite in the regression gate.
- **Gates** on source hash `056b6401b11097305a5d2ab8518fdbe7e594c7ac12efc07a447a9fd21049e8c4`: unit 59 passed, 2 deselected in 10.54s; regression 3794 passed, 196 deselected in 2588.96s (0:43:08); docker 196 passed, 1218 deselected in 6062.38s (1:41:02), each exit 0 with empty stderr and no warnings, kept awake. Check `git status --untracked-files=all` yourself.

### Edits to approved tests

Check with `git diff 188ba43 -- tests/`.
- `tests/test_general_preset.py`: the shipped version's literal, `general-v4` to `general-v5` (FR-176:
  a changed text is a new version).
- `tests/test_adr12_sandbox.py`: a report needle now names `GENERAL_VERSION` rather than `general-v4`.
- `tests/test_budget.py`: `POLICY_FIELDS` gains `delegation_fraction` (FR-184).
- `tests/test_distribution.py`: example 44 added to `EXPECTED_EXAMPLES`.
- No new table, so `tests/conftest.py`'s `_TRACKED` (NFR-38) needs no entry.

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **Budget.** Can a child, several concurrent children, or the parent take the run past its ceiling
   by more than one call or command per running agent? Is a child's reservation exactly
   `delegation_fraction` of the parent's remainder, and does its release return exactly its unspent
   amount, in USD, tokens or both, and with an unpriced spend?
2. **Files and reach (NFR-54).** Can a child see anything of its parent's workspace it was not named,
   reach a host its parent cannot, or run with another tenant, project or principal? Paths, links,
   folders, the bounds; specs built by hand.
3. **Depth.** Can any run reach depth 2, by any spec, policy or configuration?
4. **Refusals.** Is every refusal a tool error, and does one refused by its files or its reservation
   spawn nothing and take no task count? A refusal after the reservation: is it released?
5. **Cancellation and approval.** Cancel the parent at each phase of a call (copying, reserving,
   awaiting approval, running, ending); does every child end with its record complete, its lease
   released, its workspace removed? Under `REQUIRE_APPROVAL`, can a child start before the decision?
6. **NFR-53.** A run that does not name the tool, and an orchestrator's plan: the same requests,
   events, rows and manifest as at `188ba43`?
7. **Example 44.** Any check that passes without its subject or fails for another reason; each
   removal the tests make, and whether it fails the check that names it.
8. **Cleanup.** Every exit of example 44 and the tests leaves no container, workspace or row of its
   own.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M51,** and the README's rows.
- **Owner's choices:** DECISION-8b115c86's four, and the corrected P5A-D20.
- **Checks 3 and 4 of example 44 are scripted in every mode,** so the child's attempt and the refused
  spawn always happen; checks 1 and 2 use the gateway model live.
- **A delegating run's parent has no reserve of its own:** its children's shares come out of its one
  reservation, and FR-68's overshoot applies per running agent.
- **The README's example table has no row for example 43** (an M51 omission found here); M52 adds 44's.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. List every finding, and mark
  which you would block on.
- **Save every probe script** under a folder of your own in the system temp directory, and say where.
  Leave nothing in the repository, not even a scratch file at its root.
- **Clean up** every container, folder, process, row and lock you create, and say what you removed.
  Stop every process you started before you report.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M52-delegation-tool --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M52-delegation-tool --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- the share is the whole remainder
- the share ignores the parent's spend
- the token share ignores the parent's spend
- the parent keeps its USD
- the parent keeps its tokens
- a share of nothing is allowed
- a parent that may not call delegates
- a duplicate reservation replaces
- a child's release goes to the pool
- the fraction is unchecked
- a path's raw export is the file
- the child gets no files
- the briefing names no files
- the child's lease is not released
- the answer carries no taint
- the spawn's approval is ignored
- calls run one at a time
- a delegating child is allowed
- a child without a workspace is allowed
- the principal is not passed
- no budget is allowed
- a pool child may delegate
- the tool is not registered
- a profile naming the tool needs no delegate
- a delegate needs no workspace
- the fraction is recorded on every run
- the fraction is never recorded
- the preset never adds the tool
- the child is briefed with 4 turns
- delegate_approval is ignored
- the child loses the parent's egress
- the child loses the parent's limits
- a plan-scope child is allowed
- a snapshot child is allowed
- a wider child egress is allowed
- the reservation is named for the call id
