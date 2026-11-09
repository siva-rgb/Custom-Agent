You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M51, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M51 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet 5.5 subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M51 is Phase 5A increment 1's last milestone and its exit signal. It runs the full ADR-12 comparison
(36 tasks, three times on each arm), tunes the general preset's instructions between comparisons on
two upstream providers, and adds example 43: general agents and general subagents of two tenancies,
at once, each held to its budget, its sandbox, its approvals and its tenancy. It also closes M50's
round-1 note 3 (a page reproduced from memory passed a fetch check).

**The owner's rules.** Every issue is fixed in the milestone that found it; nothing carries to the
next. And the review stopping rule (DECISION-c697f36c): a round that approves with nothing blocking is
the last; its findings are fixed without another round. So report everything you find, and say
clearly which you would block on.

The requirements are quoted below **verbatim from SPEC.md as approved**, copied by a script. Check
each quote against SPEC.md yourself; where this brief and SPEC.md differ, SPEC.md wins and the
difference is a finding.

## The requirements, verbatim

### From the decisions table

| Decision | P5A-D1 to P5A-D14 |
|---|---|
| P5A-D4 Exit signal | The ADR-12 comparison rerun with the general preset on a larger task set, its gaps reported as measured; and every governance property -- budgets, sandbox, approvals, tenancy -- held under contention. Delegation tasks join the set with increment 2's tool |
| P5A-D10 Instructions | Shipped as SDK source with a version (`general-v1`, ...) the manifest records; a change is a new version and a new `agent_spec_hash`. Tuned on at least two upstream providers through the gateway, with no provider-specific wording (NFR-1) |

### "#### M51: the comparison, tuning, and increment 1's exit"

- FR-181: **The comparison** runs every task three times on each arm (P5A-D4), and the report gives, per kind and overall: pass rates, terminal statuses, turns, tokens, cost and wall time, each gap reported as measured, never as a pass or fail of this SDK (FR-62).
- FR-182: **Tuning** happens between comparisons, on the preset alone: its instructions may change between runs of the sandbox tasks on at least two upstream providers; each version is recorded with the providers it ran on, and the final report is one comparison of the final version (P5A-D10).
- FR-183: **The exit signal** (P5A-D4) is a test and an example. `scripts/43_general_contention.py` runs general agents and general subagents of two tenancies at once, starts shuffled with random delays. The hostile attempts are commands the example issues through scripted children, as FR-171's are, so each subject always happens; live, the gateway model is given each attempt verbatim and one that declines fails its check:
  - each run is stopped by its own budget within one call's or command's cost of its ceiling;
  - each workspace reaches nothing but its allowed host: an IP literal, an unlisted host, a privately resolving name, the host's database port and a neighbour are each refused or unreachable, against a control, as example 41's are;
  - every export waits for approval and is recorded with its decision;
  - nothing of one tenancy is read by the other -- its workspace, rows, snapshot or exported artifact -- and nothing remains after `aclose()`.
  - **Checked against the store, the provider and the commands' own output, on both stores,** each check naming its subject with a witness independent of the property (KNOWLEDGE-1afcf6b2), ten times on `DockerProvider` before review and on `FakeProvider` in the gates, under DECISION-a00773b0 and DECISION-a7d6a1f5.

### Non-functional

- NFR-51: Phase 5A adds no runtime dependency (NFR-40). The Claude arm's image, its Dockerfile and its CLI are eval-only, under `evals/adr12/`, and nothing in `agentsdk/` imports, pulls or builds them.
- NFR-52: The preset stays provider-agnostic (NFR-1): its instructions, tools and defaults name no provider, model or provider-specific feature, and it runs on any registry model whose context window is known.

### Acceptance

- AC-109: FR-181 to FR-183, the exit signal: the report covers every task three times on each arm, per kind and overall, with the preset's version, the model and both SDKs' versions; example 43 holds every check ten times on `DockerProvider`, shuffled with random delays, and the gates hold it on `FakeProvider`; each check fails without its subject -- a test removes, in turn, the plain run's budget check, the export's approval, the workspace's egress check, and the tenancy filter (the `WorkspaceManager`'s key by tenant and project, FR-138, and the store's tenant predicate, each removed on its own), and each removal fails the check that names it.

## The records

All in `.genesis/project.json`. A choice they record is not a defect; code that differs from them is.
- **DECISION-e66f1af4, DECISION-2cdd97da, DECISION-abd83872:** Phase 5A's decisions and the spec
  review's amendments.
- **DECISION-ce409a10** (M50's close): M50 round-1 note 3 sent to M51.
- **DECISION-7830ca10** (pre-flight, owner): tuning on `bedrock.anthropic.claude-haiku-4-5` and
  `openai.gpt-4o-mini`, at most three versions after general-v1, stopping at the first that passes
  every task its predecessor passed on both, the final comparison running the last version; live
  spend capped at USD 20; proof of fetch on both arms; example 43's shape (per tenancy two general
  agents and one plan of two general subagents), live on gpt-4o-mini three runs per store, ten Docker
  runs per store before review.
- **DECISION-2685735a** (the live work as measured), **DECISION-8fc29ec4** (the pre-review's
  findings and their repairs, below).
- Standing: KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's
  text), DECISION-a7d6a1f5 and DECISION-a00773b0 (no vacuous live check; a quoting model), NFR-43.

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5a`; HEAD is `deb47a6` (M50). M51 is uncommitted. Under `agentsdk/` it changes only
`presets.py` (the general preset's instructions, now `general-v4`); no migration, no dependency.

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** the gateway settings (`BASE_URL`, `MODEL_API_KEY`) and DATABASE_URL in `.env`.
  **Never print credentials or a connection string;** on a database error print the exception type
  only. Example 43's Postgres runs write rows under their own tenancies; the tests remove theirs
  (`SYN-m51-`); the author's live runs left theirs under `SYN-m51live-`, as M49's did.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
  ES_DISPLAY_REQUIRED)` (DECISION-08a56b57). Before calling any delay real, compare wall time with
  monotonic time. `.venv\Scripts\python.exe` is a launcher that starts the real interpreter as a
  child: killing it alone leaves the child running.
- **Docker Desktop is running; the Claude arm's image `adr12-claude:cli-2.1.267` is built** (rebuilt
  twice in M51 with the owner's approval, for the driver's tool calls and results; an older untagged
  build is left in place). **Never pull, build, prune, stop or restart anything,** and do not rebuild
  that image. You may run containers from it and from `python@sha256:6d43704b...` with
  `--pull never`, through the code's own paths or named `r1-...`, and must remove each. In Git Bash,
  prefix a docker command that names an absolute container path with `MSYS_NO_PATHCONV=1`. Report how
  many containers labelled `adr12.claude-arm`, `adr12.verifier` or `agentsdk.tenant` remain at the
  end (it must be 0 of yours).
- **Line endings vary by file,** and a scripted edit can change them. A mutant whose edit silently
  never applies looks exactly like a surviving one (it happened twice in M51): assert the file
  changed before running its test, and restore it byte for byte, verified by SHA-256. **The same holds
  for a runtime patch: count how often it fired, and treat zero as no result.**
- **Live:** the gateway may be used for a smoke run only, a few cents, for example
  `python evals/adr12/tune.py --model openai.gpt-4o-mini --directory <a folder of yours>` (about 2
  cents, 10 minutes) or example 43 through its functions under tenancies of your own. Never write into
  `evals/adr12/reports/`: pass a folder of yours. **Do not run the full comparison** (about USD 3).

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. Three python processes (PIDs 27472, 6348, 17824) are the owner's Jupyter kernel: leave them.
  Run with `-p no:cacheprovider`; `pytest.ini` deselects Docker tests unless you pass `-m docker`.
  **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M51-general-contention --timeout 7200000` in the background, kept awake:
  unit (`tests/test_general_contention.py`, about 1 minute), regression (the full suite, about 40
  minutes), and `docker` (every Phase 5 Docker-marked test plus example 43 twenty times, about 100
  minutes).
- M51's own offline tests: `tests/test_general_contention.py`, `tests/test_adr12_tuning.py`,
  `tests/test_adr12_sandbox.py`, about 3 minutes together.

## Task under review

### What M51 builds

- **Proof of fetch** (M50 note 3, DECISION-7830ca10, pre-review F1): `evals/adr12/sandbox.py`
  (`Workspace.fetched`; both fetch checkers require it), `sandbox_arms.py` (`reached_hosts`: this SDK's
  allowed connections of the run, read after the release; `named_hosts`: the Claude arm's fetching
  calls whose results were not errors), `claude_driver.py` (each call's id, name and input, and each
  result's error flag, never its content), `harness.py` (`FETCH_PROOF`, the row's `fetched`).
- **Tuning** (FR-182): `evals/adr12/tune.py` (the run, the log and its lock, the stop rule),
  `run_comparison.py` (`credentials()`, `gateway()`), `agentsdk/presets.py` (`general-v4`), and
  `evals/adr12/reports/tuning/` (the log and every tuning run's report).
- **The comparison** (FR-181): `report.py` (pass rate, statuses, median turns, tokens, cost and median
  wall time per kind and overall; a report never replaces another) and the final report
  `evals/adr12/reports/adr12-comparison-2026-11-08-2.{json,md}`.
- **The exit signal** (FR-183, AC-109): `scripts/43_general_contention.py` and
  `tests/test_general_contention.py` (twelve checks; each removal AC-109 names, and seven more).
- **Tests:** `tests/test_adr12_sandbox.py`, `tests/test_adr12_tuning.py` (new),
  `tests/test_general_preset.py` (the shipped version), `tests/test_distribution.py` (example 43).

### The live work (DECISION-2685735a, DECISION-8fc29ec4)

- **Tuning**, the 24 sandbox tasks once per version on this SDK's arm (Haiku 4.5 / gpt-4o-mini):
  general-v1 21/18, general-v2 22/20, general-v3 22/19, general-v4 23/18. No version held under the
  stop rule; tuning stopped at general-v4, which ships.
- **The final comparison** on general-v4 and Haiku 4.5, every task three times on each arm, under the
  strict proof of fetch: this SDK 102 of 108, the Claude arm 105 of 108. A first run under the earlier
  rule overwrote M50's report of the same date; M50's was restored from its commit, and that run was
  superseded by the rerun (kept outside the repository).
- **Example 43 live** on gpt-4o-mini through the author's driver over the example's own functions:
  the first six runs failed (a live model stopped its runaway before a ceiling of a few calls), then
  with the live ceiling below one call's cost, 6 of 6 held; after the pre-review's twelfth check, 6 of
  6 again. On Docker, scripted, 20 of 20 before review.
- Live spend for M51 about USD 7.36.

### The pre-review's findings, answered (DECISION-8fc29ec4)

| Finding | Repair |
|---|---|
| F1: the Claude arm's proof of fetch was a substring match; `echo example.com` counted. | **Owner:** a fetching call on the URL whose result was not an error; the driver reports results' error flags; the image rebuilt and the comparison rerun. |
| F2: no check tried a cross-tenant `decide`; removing that store guard went unnoticed. | Example 43's twelfth check: each tenancy tries to decide the other's interruption and list it by its run; removals on both stores fail it. |
| F3: the spend check passed with no run pressing its ceiling. | Its subject is a run whose spend reached its ceiling. |
| F4: a tuning run could spend and then be refused; the log had no lock. | Refused before the run; written whole under a lock. |
| F5: `write_report` checked then wrote. | Each file claimed exclusively. |
| N1, N3, N4 | The presets comment says why general-v4 ships; the operator retries a failed decision; Postgres connections and interruptions removals. |

### How the author checked it

- **Tests:** tests/test_general_contention.py 46 (26 offline, 20 Docker), tests/test_adr12_tuning.py 13, tests/test_adr12_sandbox.py 209 (206 offline, 3 Docker), tests/test_general_preset.py 126 (124 offline, 2 Docker).
- **Mutation:** 43 mutants after the repairs, one exact edit each, every file restored
  byte for byte (appendix). Two survivors were killed once tests reached them (a non-fetching tool
  given a URL; a listing leak with the target interruption chosen exactly); two first failed to apply
  on CRLF files and were run again.
- **Older suites:** every file touching the changes, 411 passed; the full suite in the regression gate.
- **Gates** on source hash `b8790e16824f4ca78cc7e060653a67b958c5d30a3930e236be22436ec6315d28`: unit 26 passed, 20 deselected in 39.76s; regression 3703 passed, 194 deselected in 2327.17s (0:38:47); docker 194 passed, 1127 deselected in 6193.78s (1:43:13), each exit 0 with empty stderr and no warnings, kept awake. Check `git status --untracked-files=all` yourself. An earlier run of the same gates on the same hash passed unit and regression, but its Docker gate ended by SIGTERM after 157 tests: the laptop, on battery, entered Modern Standby (Kernel-Power 506) at 03:33 IST and woke at 08:08, past the timeout. The test then running was `test_on_docker_example_41_holds_every_check[postgres-7]`, which passed in the rerun. The rerun had no standby events.

### Edits to approved tests

- `tests/test_general_preset.py`: the shipped version's literal, `general-v1` to `general-v4`, where a
  test pins it (FR-176: a changed text is a new version). Check with `git diff deb47a6 -- tests/`.
- `tests/test_adr12_sandbox.py`: M50's fetch tests now pass evidence of a fetch; the report needle
  names `general-v4`. `tests/test_distribution.py`: example 43 added to `EXPECTED_EXAMPLES`.
- No new table, so `tests/conftest.py`'s `_TRACKED` (NFR-38) needs no entry.

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **Proof of fetch.** Can either arm pass a fetch task without reaching the host -- this SDK's
   connection rows (whose run, allowed, settled after the release), the Claude arm's calls and
   results (a fetcher that fails silently, a URL in a quoted string, case, a WebFetch of another
   page)? Does a real fetch ever fail it?
2. **Example 43's checks.** Any check that passes without its subject, or fails for a reason other
   than the one it names. Is each removal real (the code path the example takes), and does each fail
   the check that names it, by that check's own clause?
3. **Tenancy.** Same project for both tenancies: is there anything of one tenancy -- a row, a
   workspace, a snapshot, an artifact, an interruption, a command's output -- the other can read or
   act on that no check covers?
4. **Budgets.** Each run stopped within one call's or command's cost of its ceiling (FR-183): the
   runaway, the plan and its children; the live ceiling below one call's cost.
5. **Tuning.** The stop rule against DECISION-7830ca10 and the log; a version logged under the wrong
   text, model or price; NFR-52 in `general-v4`; NFR-51 (nothing in `agentsdk/` names the harness).
6. **The report.** FR-181's measures per kind and overall, recomputed from the JSON; every task three
   times on each arm (AC-109); the model and both SDKs' versions; a report replacing another.
7. **The credential.** In the driver's new events, the tuning reports, the final report, any error.
8. **Cleanup.** Every exit of example 43 and its tests leaves no container, volume, network, snapshot
   or row of its own; nothing outside its tenancies is touched.

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M50,** and the README's rows.
- **Owner's choices:** the decisions above, including the stop rule (general-v4 ships though
  general-v2 passed one task more across both providers) and the budget rule.
- **Tuning uses the comparison's own 24 tasks:** there is no held-out set (P5A-D10 names none).
- **The Claude arm's network is not recorded:** its evidence of a fetch is a call that succeeded, not
  a connection (stated in the report).
- **Single tuning runs are noisy:** a task lost between versions may be variance; the rule reads it
  as written.
- **The first live batch's Postgres run 3,** in which no run acted, is unexplained: that driver did
  not print run statuses.
- **The author's live example-43 rows** remain under `SYN-m51live-`, as M49's do.

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
  control approve . M51-general-contention --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M51-general-contention --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- page fetch without proof
- site fetch without proof
- a refused connection counts
- another run's connection counts
- this SDK's evidence dropped
- any tool counts
- the Claude arm's evidence dropped
- the driver drops tool calls
- the harness assumes a fetch
- the row omits the evidence
- no overall row
- tokens counted in only
- a count for a rate
- no wall time
- a lost task still holds
- one model is enough
- no cap on versions
- a changed text under its version
- logged as another version
- a third model
- allowed fetch unchecked
- refusals unchecked
- runaway unchecked
- exports need not wait
- rows unchecked
- kept workspace unchecked
- snapshot unchecked
- artifact unchecked
- anything may remain
- the keeper's export approved
- an error result counts
- any Bash command counts
- the host as a prefix
- the driver drops results
- the driver reports result content
- cross-tenant decide unchecked
- cross-tenant listing unchecked
- the spend check needs no pressure
- refused only after spending
- the log written in place
- a live lock removed
- a report claims a taken name
- a skipped name's file left
