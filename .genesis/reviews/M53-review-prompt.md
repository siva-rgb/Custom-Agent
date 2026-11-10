You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M53, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M53 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet 5.5 subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M53 is Phase 5A increment 2's second milestone. The ADR-12 comparison harness (`evals/adr12/`) gains
a `delegation` kind of six tasks, each splitting into independent pieces over its files. On them this
SDK's arm runs the delegating general preset and the Claude arm is given its CLI's own subagent tool.
Each row reports whether its run delegated and how many children it ran, never as a condition of
passing. The whole comparison was then rerun live: 42 tasks, three repetitions, both arms.

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table

| Decision | P5A-D21 and P5A-D22 |
|---|---|
| P5A-D21 Eval | ADR-12 gains a `delegation` kind of about 6 tasks. The Claude arm is given its CLI's own subagent tool. Whether a run delegated is measured and reported, never required to pass |
| P5A-D22 Milestones | M52 the reservation and the tool (example 44); M53 the delegation tasks and the comparison rerun; M54 the exit signal (example 45) |

### "#### M53: delegation tasks and the comparison"

- FR-188: The ADR-12 harness gains a `delegation` kind of about 6 tasks (P5A-D21).
  - **The tasks:** each splits into independent pieces over the task's files, and its checker reads the parent's workspace or answer, as a sandbox task's does, with a good, a bad and a tricky outcome.
  - **This SDK's arm** runs `general_agent(..., delegate=True)`.
  - **The Claude arm** is given its CLI's own subagent tool, as the pinned CLI names it.
  - **Budget:** the kind's budget follows FR-179's rule, ten times the sandbox kind's median cost on this SDK's arm in M51.
  - **Reported per row:** whether the run delegated, and how many children it ran (on this SDK's arm from `runs.parent_run_id`; on the Claude arm from its subagent tool calls whose result was not an error). Delegation is never a condition of passing.
- FR-189: **The comparison is rerun:** every task, three repetitions, both arms, as FR-181 states, with the preset's final version. Tuning follows FR-182, on the delegation tasks.

### Non-functional

- NFR-51: Phase 5A adds no runtime dependency (NFR-40). The Claude arm's image, its Dockerfile and its CLI are eval-only, under `evals/adr12/`, and nothing in `agentsdk/` imports, pulls or builds them.
- NFR-53: A run that does not name `delegate_task` behaves exactly as it did before M52: the same requests, events, rows and manifest. So does an orchestrator's plan, whose governor gains `reserve_child` but none of whose reservations change. The only exception is the general preset's instruction version and its `agent_spec_hash` (FR-186). Increment 2 adds no migration and no runtime dependency (NFR-51).

### Acceptance

- AC-111: FR-188 and FR-189: every delegation task's checker accepts its good outcome and rejects its bad and tricky ones; each row reports whether its run delegated and how many children it ran; the report covers every task three times on each arm, with the preset's final version.

## The records

All in `.genesis/project.json`. A choice they record is not a defect; code that differs from them is.
- **DECISION-0a617b3f** (owner): M53's pre-flight -- the six tasks as proposed, tuning only if needed
  and at most two versions, Haiku 4.5 as in M51, live spend capped at USD 6.
- **DECISION-c529a9ae** (owner): **no tuning.** general-v5 ran the six delegation tasks once on each
  tuning model (Haiku 4.5 6/6; gpt-4o-mini 4/6, logged in `evals/adr12/reports/tuning/delegation/`);
  tuning to FR-182's stop rule would have cost about USD 7.6, over the cap, so the comparison runs
  general-v5 as shipped. FR-189's "Tuning follows FR-182, on the delegation tasks" is met to that
  extent by the owner's choice; it is not a finding.
- **DECISION-3f05f648:** the comparison's result, below.
- **DECISION-8c9e432d:** the mutation run and the pre-review's findings, below.
- Standing: KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's
  text; from M52, `pytest.raises(match=)` counts as text-keyed), DECISION-a7d6a1f5, NFR-43.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5a`; HEAD is `d0c795d` (M52). M53 is uncommitted. It changes only `evals/adr12/`
(`harness.py`, `report.py`, `run_comparison.py`, `sandbox.py`, `sandbox_arms.py`, `tune.py`; new
`delegation.py`), adds `tests/test_adr12_delegation.py`, the live report
`evals/adr12/reports/adr12-comparison-2026-11-10.json` and `.md`, the delegation tuning log under
`evals/adr12/reports/tuning/delegation/`, and rewrites the header line of
`evals/adr12/reports/tuning/tuning-log.md`. Nothing under `agentsdk/`; no migration, no dependency.

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
  containers from `python@sha256:6d43704b...` and the local `adr12-claude` image with `--pull never`,
  through the code's own paths, and must remove each. Report how many containers labelled
  `agentsdk.tenant` or `adr12.verifier` remain at the end (it must be 0 of yours).
- **Line endings vary by file,** and a scripted edit can change them. A mutant whose edit silently
  never applies looks exactly like a surviving one: assert the file changed before running its test,
  and restore it byte for byte, verified by SHA-256. **The same holds for a runtime patch: count how
  often it fired, and treat zero as no result.**
- **Live:** a smoke run only, a few cents:
  `python evals/adr12/run_comparison.py --set delegation --only word-counts --repetitions 1` costs about
  USD 0.05 and writes a report you must delete afterwards (it lands in `evals/adr12/reports/` unless
  you pass `--reports-dir` to a folder of your own -- do that).

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. Three python processes (PIDs 27472, 6348, 17824) are the owner's Jupyter kernel: leave them.
  Run with `-p no:cacheprovider`; `pytest.ini` deselects Docker tests unless you pass `-m docker`.
  **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M53-delegation-comparison --timeout 7200000` in the background, kept
  awake: unit, regression (the full suite, about 30 minutes) and docker (about 70 minutes).
- M53's own offline tests: `tests/test_adr12_delegation.py -m "not docker"`, about a minute; with the
  other adr12 suites (`tests/test_adr12_*.py`) about five.

## Task under review

### What M53 builds

- **The tasks** (FR-188): `evals/adr12/delegation.py` -- `DELEGATION_TASKS`: word-counts, csv-totals,
  log-errors, ini-to-json (data, checked from the files the run left) and three-functions,
  fix-three-bugs (code, verified by the harness's verifier container on visible and hidden cases
  through `verify/dispatch.py`, a harness file the run never sees). Each checker also requires the
  inputs unchanged.
- **Budget** (FR-188): `evals/adr12/sandbox.py` -- `ceilings("delegation")` is ten times M51's
  sandbox median on this SDK's arm, `M51_SANDBOX_MEDIAN`, recomputed from that report by a test.
- **The arms** (FR-188): `evals/adr12/sandbox_arms.py` -- `PresetArm` runs `general_agent(...,
  delegate=True)` on a delegation task, counts its children from the Runner's parent record and its
  usage through `_Counted`, a wrapper on the shared model client, so the row costs the whole run;
  `ClaudeContainerArm` adds `Agent` to the driver's tools on a delegation task and counts
  `subagents(events)`.
- **The rows and the report** (FR-188): `evals/adr12/harness.py` -- `children` and `delegated` on
  delegation rows, the kind's setup and how it is measured in the metadata; `evals/adr12/report.py` --
  a "## Delegation" section.
- **The commands** (FR-189): `run_comparison.py --set delegation` and `chosen_tasks`; `tune.py --set
  delegation`, logging in its own folder.
- **Tests:** `tests/test_adr12_delegation.py` (new).

### The live work (DECISION-3f05f648)

- The comparison, `evals/adr12/reports/adr12-comparison-2026-11-10.json`: **243 of 252** passed.
  This SDK: M15 36/36, sandbox 69/72 (repair-date-format budget_exceeded three times, as in M51),
  delegation 17/18. Claude arm: M15 36/36, sandbox 69/72, delegation 16/18. USD 4.03; M53 about 4.90
  of its 6.
- **No run delegated on either arm:** `children` 0 on all 36 delegation rows. So the delegation
  measure is exercised offline only, and whether the CLI's reported usage includes its subagents' is
  not measured by this comparison. The pre-review confirmed the pinned CLI names the tool `Agent`.
- The CLI's cost estimate equalled the cost computed from its usage on every Claude row, within float
  rounding.
- **The report predates two pre-review repairs:** its 144 sandbox rows carry `children: 0` and
  `delegated: false` (F3 below), and the comparison ran before F4 and F5. It is kept unchanged as the
  record of that run. The report keeps no workspace, so whether F4 would turn one of its passes into
  a fail cannot be read from it (F4 can only make a checker stricter, on the JSON tasks). F5 can only
  turn a code task's fail into a pass, and every code-task row in it passed, so F5 changes none.

### The mutation run and the pre-review (DECISION-8c9e432d)

- **Mutation run 1** (35 mutants): four survived because no test reached their state -- the report's
  arm total over an uncounted run, the ini expectation, fix-three-bugs' hidden cases, the check
  program's file name; four tests were added and all four killed.
- **The pre-review** (Sonnet 5.5, runtime probes only, no repository edit):

| Finding | Repair |
|---|---|
| F1: `tune.py --set delegation` logged into M51's tuning log by default. | `SET_DIRECTORIES`: each set its own default folder; the log header names both sets; both `tuning-log.md` views rewritten from their JSON (header only). Test drives `tune.main`. |
| F2: no run delegated live. | The comparison's own finding (DECISION-3f05f648), reported as such. |
| F3: sandbox rows carried `children: 0`, `delegated: false` though no delegation tool was given. | Both keys and both arms' counts on delegation tasks alone (NFR-53). |
| F4: the JSON checkers compared with Python `==`, taking `false` for 0 and `7.0` for 7. | Compared as canonical JSON. |
| F5: a correct module importing a sibling of the run failed verification. | The delegation dispatcher appends the run's folder to `sys.path` after the standard library; a test shows a run's `colorsys.py` does not shadow it. |
| F6: FR-188 names `runs.parent_run_id`; the arm reads `runner._parents`. | The same value the Runner writes to that column as it starts each run (`agentsdk/api.py`, `_run`); the arm keeps no store. Stated in the code. |
| Note: a duplicated results event would count a child twice. | `subagents` counts distinct call ids. |

### How the author checked it

- **Tests:** tests/test_adr12_delegation.py 53 offline + 2 Docker; adr12 and distribution suites 344 passed.
- **Mutation:** 44 mutants, one exact edit each, every file restored byte for byte
  (appendix); one more survived the final run (the dispatcher's path inserted first) because the
  shadow test used `json`, which the verifier loads before the dispatcher; rewritten on `colorsys`,
  killed.
- **Gates** on source hash `995ae8431205e1a8868ad85fc6a29139b439207c41eb2a89d2940ea6f1a5c031`: unit 323 passed, 5 deselected in 200.70s (0:03:20); regression 3847 passed, 198 deselected in 2733.24s (0:45:33); docker 198 passed, 1271 deselected in 6040.76s (1:40:40), each exit 0 with empty stderr and no warnings, kept awake. Check `git status --untracked-files=all` yourself.
  These are the second run on the same hash. The first run's docker gate failed one case,
  `test_on_docker_example_41_holds_every_check[postgres-9]`: its control found that the host itself
  could not reach 172.66.147.243, so a blocked connection inside proved nothing. Rerun alone, the 10
  postgres cases passed (755 s); then all three gates were rerun on the unchanged tree. M53 touches
  nothing on that test's path; judge that claim yourself.

### Edits to approved tests

None: `git diff d0c795d -- tests/` is empty. The new file is `tests/test_adr12_delegation.py`. No new
table, so `tests/conftest.py`'s `_TRACKED` (NFR-38) needs no entry.

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **The checkers.** Can a wrong workspace pass any of the six, or a right one fail? Formatting, key
   spelling, ordering, line endings, a changed input, a forged `check_<module>.txt`, a module the
   verifier cannot load. Do the prompts and the checkers agree?
2. **The measure.** Is `children` exactly the run's direct children on this SDK's arm, and the
   successful `Agent` calls on the Claude arm, in every end state (failed child, refused spawn, failed
   or cancelled parent, budget exceeded)? Is it never a condition of passing?
3. **Usage and cost.** Does a delegation row cost the whole run, children included, with nothing
   counted twice or missed? Are the ceilings right, and recomputed from M51's report?
4. **NFR-53 and the earlier tasks.** Do the 36 earlier tasks run, row and report exactly as at
   `d0c795d`, on both arms, apart from the delegation section's absence?
5. **FR-189 and AC-111.** Does the report cover every task three times on each arm with the preset's
   final version? Is the report consistent with its rows, and with DECISION-3f05f648?
6. **The commands.** `run_comparison.py --set`/`--only`, `tune.py --set`/`--directory`: does each
   select and log what it says?
7. **The tests.** Anything vacuous, text-keyed, or passing for the wrong reason.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M52,** and the README's rows.
- **Owner's choices:** DECISION-0a617b3f and DECISION-c529a9ae (no tuning).
- **No run delegated live** (DECISION-3f05f648): the per-row measure is exercised offline; the Claude
  arm's subagent usage is unmeasured.
- **The 2026-11-10 report predates F3 to F5** and is kept as the run's record.
- **The verifier runs each module from the run's top folder;** a module placed in a subfolder is not
  found, as with the sandbox code tasks (the prompts say "in this folder").

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
  control approve . M53-delegation-comparison --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M53-delegation-comparison --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- delegation ceilings from M15 multi-step
- M51 median usd off
- M51 median tokens off
- the arm never delegates
- the arm always delegates
- the client is not counted
- the counter drops usage
- usage is the parent's only
- children counts every run
- children never counted
- children counted on every kind
- children not passed on
- Claude never gets Agent
- Claude always gets Agent
- Claude children not counted
- Claude children on every kind
- subagents counts error results
- subagents counts a result twice
- row keys on every kind
- json compared by ==
- dispatch path not extended
- dispatch path shadows stdlib
- tune delegation into M51's log
- tune ignores --directory
- subagents counts every tool
- rows report no children
- delegated true on zero
- delegation setup is sandbox's
- measure never stated
- measure always stated
- no Delegation section
- delegated counted as runs
- children total ignores unavailable
- delegation set is empty
- all leaves out delegation
- only ignored on workspace tasks
- word-counts inputs unchecked
- csv totals unsorted accepted
- ini comments kept
- three-functions unverified
- fix-three-bugs no hidden cases
- fix-three-bugs checks may change
- dispatch ignores the module
- check program writes check.txt
