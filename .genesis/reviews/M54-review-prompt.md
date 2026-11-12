You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M54, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M54 was implemented by Opus 5.5 and its adversarial pre-review run by a Fable 5.2 subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M54 is Phase 5A increment 2's exit signal. Example 45 runs delegating general agents of two tenancies
on one Runner, their starts shuffled with random delays, and checks that each child stays within its
reservation and its files, that no run reaches depth 2, that every spawn under approval waits, that
nothing crosses tenancy, and that a cancelled parent's child is cancelled. A test removes each
subject in turn and requires the check that names it to fail by its own clause. The pre-review found
a library defect on the way, and M54 repairs it (below).

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table

| Decision | P5A-D4 and P5A-D15 to P5A-D22 |
|---|---|
| P5A-D4 Exit signal | The ADR-12 comparison rerun with the general preset on a larger task set, its gaps reported as measured; and every governance property -- budgets, sandbox, approvals, tenancy -- held under contention. Delegation tasks join the set with increment 2's tool |
| P5A-D15 Child budget | Each spawn reserves a fixed share of its parent's remaining reservation, a `BudgetPolicy` default, nested as FR-68 states. What the child does not spend returns to the parent |
| P5A-D16 Child files | The call names paths in the parent's workspace. They are copied into the child's fresh workspace through the checked tar (FR-175's `files`) and recorded by SHA-256. The child's answer returns as text |
| P5A-D17 Nesting | One level. A child has no delegation tool, so hub and spoke holds, and the overshoot stays bounded by `max_concurrent_subagents` |
| P5A-D18 Approval | A spawn is `auto` by default, like `run_command`: the budget, the sandbox and the depth do the containing. A caller may require approval |
| P5A-D19 The call | `delegate_task` blocks until its child ends and returns the child's answer. Calls in one response run concurrently, up to `max_concurrent_subagents` |
| P5A-D20 Records | No migration. A child is a run linked by `parent_run_id`, and its reservation is named in its own manifest's `budget_reservations`, since a manifest is written once, when its run starts (FR-11; corrected 2026-11-09, DECISION-8b115c86's follow-up) |
| P5A-D21 Eval | ADR-12 gains a `delegation` kind of about 6 tasks. The Claude arm is given its CLI's own subagent tool. Whether a run delegated is measured and reported, never required to pass |
| P5A-D22 Milestones | M52 the reservation and the tool (example 44); M53 the delegation tasks and the comparison rerun; M54 the exit signal (example 45) |

### "#### M54: the exit signal"

- FR-190: **The exit signal** (P5A-D4) is a test and an example. `scripts/45_delegation_contention.py` runs delegating general agents of two tenancies at once, their starts shuffled with random delays. The hostile attempts are calls the example issues through scripted parents and children, as FR-183's are, so each subject always happens; live, the gateway model is given each attempt verbatim, and one it declines fails its check. The checks:
  - each child stays within its reservation, and each parent's run within its ceiling, each within one call's or command's cost per running agent;
  - a child's workspace holds only the files its call named, and a path outside the parent's workspace, a link, or a file past a bound is refused;
  - a child's `delegate_task` is refused, so no run reaches depth 2;
  - every spawn under `REQUIRE_APPROVAL` waits for its decision;
  - nothing of one tenancy is read by the other, whether a parent's or a child's workspace, rows or answers;
  - cancelling a parent ends its running children `cancelled`, with nothing remaining after `aclose()`.
  - **Checked against the store, the provider and the commands' own output, on both stores,** each check naming its subject with a witness independent of the property (KNOWLEDGE-1afcf6b2): ten times on `DockerProvider` before review, and on `FakeProvider` in the gates, under DECISION-a00773b0 and DECISION-a7d6a1f5.

### Non-functional

- NFR-53: A run that does not name `delegate_task` behaves exactly as it did before M52: the same requests, events, rows and manifest. So does an orchestrator's plan, whose governor gains `reserve_child` but none of whose reservations change. The only exception is the general preset's instruction version and its `agent_spec_hash` (FR-186). Increment 2 adds no migration and no runtime dependency (NFR-51).
- NFR-54: A spawn never widens what its parent may reach. A child has its parent's tenant, project and principal, egress no wider than its own spec's, no host path, and only the files its call named.

### Acceptance

- AC-112: FR-190, the exit signal: example 45 holds every check ten times on `DockerProvider`, its starts shuffled with random delays, and the gates hold it on `FakeProvider`. Each check fails without its subject: a test removes, in turn, the child's reservation check, the files bound, the child's lack of the delegation tool, the spawn's approval, the tenancy filter, and the cancellation of children, and each removal fails the check that names it.

## The records

All in `.genesis/project.json`. A choice they record is not a defect; code that differs from them is.
- **DECISION-61bead9f** (owner): M54's pre-flight -- FR-190 as written, gpt-4o-mini live three times
  on Docker and PostgreSQL under tenancies of its own, a USD 1 cap.
- **DECISION-bd482fe5** (owner): live, the nester (parent and child) stays scripted: its child must
  call `delegate_task`, a tool it is not offered, which a live model cannot do.
- **DECISION-7f7247eb** (owner): the live evidence -- run until three consecutive runs hold every
  check, at most nine more; refines DECISION-61bead9f's "every check holds on every run".
- **KNOWLEDGE-5bb6df5d:** the live result, below.
- **DECISION-06dc3b5b:** the mutation run and the pre-review's findings, below.
- Standing: KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's
  text; from M52, `pytest.raises(match=)` counts as text-keyed), DECISION-a7d6a1f5, DECISION-a00773b0,
  DECISION-21e7f6fb, NFR-43.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5a`; HEAD is `f91578b` (M53). M54 is uncommitted. It adds `scripts/45_delegation_contention.py`
and `tests/test_delegation_contention.py`; edits `README.md` (example 45's row) and
`tests/test_distribution.py` (example 45 in `EXPECTED_EXAMPLES`); and changes two library files for
the pre-review's F1: `agentsdk/workspace/manager.py` and `agentsdk/api.py`. No migration, no
dependency.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** the gateway settings (`BASE_URL`, `MODEL_API_KEY`) and DATABASE_URL in `.env`.
  **Never print credentials or a connection string;** on a database error print the exception type
  only.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
  ES_DISPLAY_REQUIRED)`. Before calling any delay real, compare wall time with monotonic time.
  `.venv\Scripts\python.exe` is a launcher that starts the real interpreter as a child: killing it
  alone leaves the child running.
- **Docker Desktop is running.** **Never pull, build, prune, stop or restart anything.** You may run
  containers from `python@sha256:6d43704b...` through the code's own paths, and must remove each.
  Report how many containers, volumes and networks labelled `agentsdk.tenant` remain at the end (it
  must be 0 of yours).
- **Line endings vary by file,** and a scripted edit can change them. A mutant whose edit silently
  never applies looks exactly like a surviving one: assert the file changed before running its test,
  and restore it byte for byte, verified by SHA-256. **The same holds for a runtime patch: count how
  often it fired, and treat zero as no result.**
- **Live:** `python scripts/45_delegation_contention.py` (no flag) runs once on gpt-4o-mini through the
  gateway on Docker and PostgreSQL, about USD 0.01, under tenancies of its own (`example-tenant-<8
  hex>`); it prints its run ids. Remove its rows afterwards, children included, by those tenancies
  only. A run can fail a check because the live model did not make an attempt verbatim (FR-190's
  "one it declines fails its check"); trace any failure in its rows before calling it a defect.

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. The owner's Jupyter kernels run as `ipykernel_launcher` processes (and one from the VS Code
  Jupyter extension); their PIDs change, so identify them by command line and leave them. Run with
  `-p no:cacheprovider`; `pytest.ini` deselects Docker tests unless you pass `-m docker`. **Do not kill
  a gate mid-run.**
- **Gates:** `genesis gate . M54-delegation-contention --timeout 7200000` in the background, kept
  awake: unit (this milestone's file), regression (the full suite, about 45 minutes) and docker (about
  100 minutes).
- M54's own offline tests: `tests/test_delegation_contention.py -m "not docker"`, about 2.5 minutes;
  `-m docker`, 20 cases, about 10 minutes.

## Task under review

### What M54 builds

- **Example 45** (FR-190): `scripts/45_delegation_contention.py`. Two tenancies (`TENANT` and
  `TENANT + "-elsewhere"`), one project, five delegating `general_agent(..., delegate=True, files=FILES)`
  runs each, shuffled with random delays:
  - **piece** (keeps its workspace): delegates naming `data/a.txt`, its child listing its workspace
    (`.` and `data`) and reading the file; makes a link and a 17 MB file; then delegates naming a path
    outside, the link and the big file.
  - **runaway:** delegates a child that works without end, then works without end itself.
  - **nester:** its child calls `delegate_task`; scripted in every mode (DECISION-bd482fe5).
  - **asker:** `delegate_approval=REQUIRE_APPROVAL`; an operator approves one spawn and refuses the
    other.
  - **canceller:** its child runs `sleep 30`; the other tenancy tries to release and snapshot the
    child's workspace while it runs, then the parent is cancelled.
- **Nine checks:** BOUNDS, FILES, DEPTH, APPROVAL, WORKSPACES, ROWS, ANSWERS, CANCELLED and
  NOTHING_LEFT (after `Runner.aclose()`). A check whose subject never happened reports None and fails
  in every mode. `RunResult.cost_usd` is the run's own calls alone, while a ceiling covers its
  children's too, so BOUNDS sums the children's costs itself.
- **Tests:** `tests/test_delegation_contention.py` (new). The example on both stores and several
  seeds; a model that does nothing fails every check; `without()` removes a subject and `BECAUSE`
  names the clause it must fail by -- every AC-112 removal and more (path pattern, transfer bound, the
  run's budget check, `aclose()`, the children's spend, the child's own reservation, the share moved
  once, the child's files written, only the named files transferred, the parent's cancellation, the
  command's kill, a cancelled run's workspace end, the ceiling on time); `LATER` takes an earlier
  clause out of the example's source to show a later one failing; the library repair's own test; ten
  Docker runs per store, marked `docker`.

### The library repair (pre-review F1)

`RunControl.store` absorbs a cancel that lands while a store write runs, so the run stops at its next
checkpoint (FR-50). `RunWorkspace._run` had no checkpoint between writing `WorkspaceCommandStarted` and
starting the command, so a cancel landing during that write let the command run to its end, and only
then was the run cancelled (the pre-review saw 3 of 186 offline runs take 30 s). Now `RunWorkspace`
takes `stopping` (the Runner passes `lambda: control.requested`), asked after that write: the command
never begins, and is recorded `killed="cancelled"` with its `WorkspaceCommandEnded` before the
cancellation goes on. `test_a_cancel_while_a_commands_start_is_written_never_begins_it` lands the
cancel inside that write on both stores; without the repair it fails at 30 s. **This changes the
behaviour of every run with a workspace, not only delegating ones, in that window; judge it against
NFR-53, FR-50 and FR-148.**

### The live work (KNOWLEDGE-5bb6df5d, DECISION-7f7247eb)

Nine runs on gpt-4o-mini, Docker and PostgreSQL, each under tenancies of its own. The final three held
every check in a row (USD 0.0085, 0.0069, 0.0078). The six before held twice; each failure was traced
in its rows:

| Run(s) | Failure | Cause | Repair |
|---|---|---|---|
| 1, 3 | the runaway ended completed | the model answered below a 0.0015 ceiling | `LIVE_CEILINGS["runaway"]` 0.0006: above a first call (USD 0.00043-0.00052 measured), below it with the child's call (at least 0.00072) |
| 3 | the piece started 2 children | the model repeated the good delegation | each good call's child is its own; the hostile calls must start none |
| 6 | FILES: a listing of `. ` | the child mangled a path; that listing failed | the two listings given decide; a failed one says nothing |
| 5 | a runaway's child made no priced call | the model added an argument to `delegate_task`, refused | none: a model deviation, failing its check under FR-190 |

Live spend for M54: USD 0.073 of its 1. Every run's rows (182 runs) were removed by its own
tenancies; no Docker object of theirs remained.

### The mutation run and the pre-review (DECISION-06dc3b5b)

- **Mutation run 1** (28 mutants of the example's clauses): 11 survived. Six were clauses no removal
  reached; five were later clauses an earlier one always caught first. Six removals and the `LATER`
  test were added; all 11 killed. One new removal's rewrite first never applied (dedented source),
  failing the test by assertion: every kill of that rerun was redone on the fixed test.
- **The pre-review** (Fable 5.2, runtime probes only, no repository edit):

| Finding | Repair |
|---|---|
| F1 (blocking): CANCELLED passed when the child's command ran to its end; a cancel absorbed while `WorkspaceCommandStarted` was written. | The library repair above; CANCELLED also reads the long command's own end (`killed == "cancelled"`); removal "the command's kill on cancellation". |
| F2 (blocking): FILES never looked inside the child's workspace. | The child lists its workspace while it runs; the check reads its listings; removal "the transfer of only the named files" (the record names one file, the transfer writes three). |
| F3: BOUNDS' slack was one call per child ever started. | One call per agent running at once: a parent waits in `delegate_task`, so the most children one turn started, or one (FR-44's parallel batch); removal "the run's ceiling on time"; a test of two delegations in one turn. |
| F4: the transfer bound's FIRED mark was set at patch time. | Not in `PATCHED`; its clause text shows its effect. |
| F5: live used fixed tenancies; nominal ceilings over USD 1. | Tenancies of each run's own; ceilings lowered, three runs within USD 1 if every ceiling were spent; pinned by a test. |
| F6: the approval removal changed the example's argument. | It makes the library's tool spec ignore the approval it is given. |
| F7: SPEC clauses with nothing behind them. | The nester: DECISION-bd482fe5. ROWS' witness needs a child's rows as well as a parent's, with a test where children record none. The rest declared below. |

- **Final mutation run:** 37 mutants (appendix), every one killed, one exact edit each,
  every file restored byte for byte; nine are the repairs', three of them in the library.

### How the author checked it

- **Tests:** tests/test_delegation_contention.py 46 offline + 20 Docker; with tests/test_distribution.py 54 passed; older suites touching the changed modules 1283 passed.
- **Gates** on source hash `df3db8b29a33e875936a6dd5021abb7b2d96660045151b3e425e092f992f1226`: unit 46 passed, 20 deselected in 141.50s (0:02:21); regression 3905 passed, 218 deselected in 2543.01s (0:42:23); docker 218 passed, 1329 deselected in 5910.54s (1:38:30), each exit 0 with empty stderr and no warnings, kept awake. Check `git status --untracked-files=all` yourself.

### Edits to approved tests

`tests/test_distribution.py` gains `"45_delegation_contention.py"` in `EXPECTED_EXAMPLES` (FR-56, as
each example milestone has); nothing else: check `git diff f91578b -- tests/`. The new file is
`tests/test_delegation_contention.py`. No new table, so `tests/conftest.py`'s `_TRACKED` (NFR-38) needs
no entry.

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **The library repair.** Is the window closed on every path from the started write to the command
   (Docker and fake, memory and PostgreSQL)? Is the stopped command recorded once, with its row and
   its end event, before the run's terminal event? Does anything else change for a run that is not
   cancelled? Is the reports queue kept consistent (`_Forget`)?
2. **Vacuous checks.** Can any of the nine pass while its property is false: its subject satisfied by
   the wrong runs, a clause that always compares equal, a witness that a parent alone satisfies?
3. **The removals.** Does each `without()` subject take away what its comment says, and does
   `BECAUSE` match only that clause? Mind dedented rewrites and patches that never fire.
4. **Contention and flakiness.** Random delays and shuffled starts; the canceller's timing; cleanup
   on failure paths; anything left on Docker or in the store.
5. **Live mode.** Spend against the ceilings; tenancies; the verbatim attempts; the repairs made on
   the live runs (table above) -- does any weaken a check beyond what the live failure needed?
6. **SPEC conformance.** Every FR-190 bullet and AC-112 removal, and NFR-53 and NFR-54, against the
   wording.
7. **The tests.** Anything vacuous, text-keyed, or passing for the wrong reason.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M53,** and the README's rows.
- **The owner's choices:** DECISION-61bead9f, DECISION-bd482fe5, DECISION-7f7247eb.
- **No single link removal:** an absolute link is refused by three independent layers (the transfer's
  member kinds, its path filter, the tool's one-regular-file check), so no one removal reaches FILES'
  link clause.
- **ROWS reads workspaces, commands and interruptions:** runs, run events and manifests have no
  tenant-scoped read in the Runner's API; they are read by run id inside the library. No child asks
  for approval, so interruptions have only parents' rows.
- **No snapshot exists in an unmodified run;** NOTHING_LEFT's label names snapshots for any a run
  makes.
- **WORKSPACES on a running child:** `release` refuses any workspace not kept, so the child's running
  workspace tests tenancy through the snapshot attempt; the kept workspace tests both.
- **Live, a model deviation fails its check** (FR-190): run 5 above.

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
  control approve . M54-delegation-contention --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M54-delegation-contention --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- parent ceiling unchecked
- parent ceiling checks own cost only
- child reservation unchecked
- child's reservation name unchecked
- runaway child's ending unchecked
- runaway's own ending unchecked
- runaway reaching its ceiling unchecked
- child's files unchecked
- child's read unchecked
- hostile refusals unchecked
- hostile refusal type unchecked
- piece's children count unchecked
- child's delegate refusal unchecked
- depth 2 unchecked
- spawn interruptions unchecked
- spawn decision order unchecked
- approved child count unchecked
- refused spawn's error unchecked
- workspace reach unchecked
- other's rows unchecked
- rows not own unchecked
- other's answers unchecked
- parent cancelled unchecked
- child cancelled unchecked
- child's workspace removal unchecked
- aclose leftovers unchecked
- absent subject passes
- one tenancy only
- F2 child's listing unchecked
- F2 child lists nothing
- F1 command's end unchecked
- F3 slack per child started
- F3 slack ignores fan-out
- F7 rows child witness dropped
- F1 library: stop never asked
- F1 library: stop not passed
- F1 library: stopped command not recorded
