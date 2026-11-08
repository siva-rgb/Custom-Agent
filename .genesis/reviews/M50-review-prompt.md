You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**This is M50, round 1. Use a fresh session: Fable 5.1, falling back to Opus 5** if Fable 5.1 fails
to start. M50 was implemented by Opus 5.5 and its adversarial pre-review run by a Sonnet 5.5 subagent.
Bring no memory of an earlier review into this one: if your session holds any, say so and ask for a
fresh one.

M50 is Phase 5A's third milestone. It grows the ADR-12 comparison harness (`evals/adr12/`, outside
the `agentsdk` package) with a `sandbox` task kind: 24 tasks, each checked by the workspace it leaves,
run on two arms -- this SDK's general preset (M49) in its own workspace, and the Claude Agent SDK with
the pinned Claude Code CLI in a disposable container built locally from `evals/adr12/Dockerfile`. M51
runs the full three-repetition comparison; M50 builds and proves the harness, and ran every sandbox
task once per arm live.

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
| P5A-D12 Eval | ADR-12 grows with sandbox tasks, each checked by its result in the workspace; 3 repetitions; both arms on the same model and turn limit. The Claude arm runs the pinned CLI in a disposable container with the task's files, its local image built only with the owner's approval. This SDK's arm refuses every export, each refusal recorded; delegation tasks wait for increment 2 |
| P5A-D14 Milestones | M48 budgets for plain runs; M49 the preset and the subagent (example 42); M50 the larger eval; M51 the comparison and tuning, increment 1's exit (example 43) |

### "#### M50: the larger comparison"

- FR-179: The ADR-12 harness grows (P5A-D12, FR-61 to FR-63 amended). The report states the task count; every acceptance criterion covers every task present.
  - **Tasks:** a new kind, `sandbox`, of about 24 tasks: shell work, code that must run and pass the task's own check, files written and edited, a fetch through the workspace's egress to an allowed host, and a multi-step repair. The 12 earlier tasks stay. A sandbox task carries its files, a turn limit and a checker that reads the workspace after the run -- files and outputs, not answer text alone -- and, as before, a good, a bad and a tricky outcome that test the checker. Delegation tasks join with increment 2's tool (P5A-D4).
  - **This SDK's arm** runs the general preset with the task's files (FR-175), the task's allowed host as its egress, `keep=True`, tenant `adr12` and project `comparison`. Its approver refuses every export at once, each refusal recorded. The checker reads the kept workspace through the provider's `export` (FR-143's checked tar) -- the host never opens a workspace path (NFR-43) -- then releases it; runs and workspaces are removed by their recorded ids.
  - **Budget per task:** one rule for every task of a kind -- a USD ceiling of ten times that kind's median cost on this SDK's arm in M15 (sandbox tasks: ten times the multi-step median), and a token ceiling likewise -- recorded in the report; a run that ends `budget_exceeded` is reported as its own terminal status.
  - **The Claude arm** runs the Python `claude_agent_sdk` driving the CLI pinned in `evals/adr12/package.json`, both inside a disposable container: the task's files copied in, tools `Read`, `Glob`, `Grep`, `Bash`, `Write` and `Edit`, the task's turn limit, its working folder read back for the checker, and the container removed after. Its image is built locally from a Dockerfile in `evals/adr12/`, by the owner or with the owner's approval; nothing pulls or builds it in a gate, and it holds no credential. The gateway's key reaches only the container's environment, never the image or a command line. It runs with ordinary networking, to reach the gateway and the fetch task's host, while this SDK's arm reaches only its allow list; the report states that asymmetry.
  - **Same model and turns:** both arms use one model through the gateway and the task's turn limit; the report names the model, both SDK versions, the preset's version and the image digests.
- FR-180: The harness's own tests, offline, cover every new task's checker against its good, bad and tricky outcomes, and both arms' construction, as M15's do.

### Non-functional

- NFR-51: Phase 5A adds no runtime dependency (NFR-40). The Claude arm's image, its Dockerfile and its CLI are eval-only, under `evals/adr12/`, and nothing in `agentsdk/` imports, pulls or builds them.

### Acceptance

- AC-108: FR-179 and FR-180: every task's checker accepts its good outcome and rejects its bad and tricky ones; this SDK's arm runs a sandbox task with its files and egress, refuses its exports, reads the kept workspace through `export` and releases it; the Claude arm runs a sandbox task in its container and is read back, with no credential in its image; nothing in a gate pulls or builds an image.

## The records

All in `.genesis/project.json`. A choice they record is not a defect; code that differs from them is.
- **DECISION-e66f1af4, DECISION-2cdd97da, DECISION-abd83872:** Phase 5A's decisions and the spec
  review's amendments.
- **DECISION-d0269633** (pre-flight, owner): the 12 M15 tasks keep their M15 setup (read-only file
  tools over a host copy of the fixture) and only the sandbox kind runs on the preset and in the
  container; the fetch tasks allow `example.com:443`; the Claude image is built FROM the workspace
  image's digest, by the owner's approval; model `bedrock.anthropic.claude-haiku-4-5` with the
  harness's price list, as M15; every sandbox task once per arm live in M50; the budget rule exactly as
  FR-179 states it, with M15's medians and the resulting ceilings named.
- **DECISION-b944d3ab** (the pre-review's findings, below; F2 and F8 mechanisms chosen by the owner).
- **DECISION-02d5d160** (the repairs as built: a pipe drained after a kill, a certifi-verified
  witness, the final live pass).
- Standing: KNOWLEDGE-1afcf6b2 (no vacuous check), DECISION-94bfee7c (no check keys on an error's
  text), DECISION-a7d6a1f5 (no vacuous live check), NFR-43 (the host never opens a workspace path).

## Repository

`<repo>` is the folder that holds this file's repository; run everything from it. The branch is
`phase-5a`; HEAD is `8d93f8d` (M49). M50 is uncommitted. It changes nothing under `agentsdk/`, adds
no migration, and adds one eval-only pin (`certifi` in `evals/adr12/requirements.txt`).

```bash
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs <command> .
```

The PATH prefix is required. **Never pass an unrecognised flag to a genesis command:** `--help`,
`--check` and `--dry-run` are all swallowed as arguments and the command runs. **Do not record
anything with genesis while a gate runs.**

- **Configuration:** the gateway settings (`BASE_URL`, `MODEL_API_KEY`) and DATABASE_URL in `.env`.
  **Never print credentials or a connection string.** M50's arms use the in-memory store; nothing of
  M50 writes the database.
- **Host:** Windows 11 Home, Python 3.11. **This laptop sleeps when idle:** run anything over a few
  minutes under a wrapper holding `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
  ES_DISPLAY_REQUIRED)` (DECISION-08a56b57). Before calling any delay real, compare wall time with
  monotonic time. `.venv\Scripts\python.exe` is a launcher that starts the real interpreter as a
  child: killing it alone leaves the child running.
- **Docker Desktop is running, and the Claude arm's image `adr12-claude:cli-2.1.267` is built.**
  **Never pull, build, prune, stop or restart anything,** and do not rebuild that image. You may run
  containers from it and from `python@sha256:6d43704b...` with `--pull never`, through the arm's own
  code or named `r1-...`, and must remove each. In Git Bash, prefix a docker command that names an
  absolute container path with `MSYS_NO_PATHCONV=1`. Report how many containers labelled
  `adr12.claude-arm` or `adr12.verifier` remain at the end (it must be 0).
- **Line endings vary by file.** A mutant whose edit silently never applies looks exactly like a
  surviving one: assert the file changed before running its test, and restore it byte for byte,
  verified by SHA-256. **The same holds for a runtime patch: count how often it fired, and treat zero
  as no result.**
- **The live gateway** may be used for a smoke run, for example
  `python evals/adr12/run_comparison.py --set sandbox --only fetch-page --repetitions 1 --reports-dir <a folder of yours>`:
  a few cents. Always pass `--reports-dir` outside the repository, so the committed report stays as
  it is.

**Running tests**
- **One test session at a time.** Check that no other python or node process of yours is running
  first. Three python processes (PIDs 27472, 6348, 17824) are the owner's Jupyter kernel: leave them.
  Run with `-p no:cacheprovider`; `pytest.ini` deselects Docker tests unless you pass `-m docker`.
  **Do not kill a gate mid-run.**
- **Gates:** `genesis gate . M50-adr12-sandbox --timeout 7200000` in the background, kept awake:
  unit (`tests/test_adr12_sandbox.py` and `tests/test_adr12_comparison.py`, about 2 minutes),
  regression (the full suite, about 40 minutes), and `docker` (every Phase 5 Docker-marked test plus
  M50's three, about 90 minutes).
- M50's own tests: `pytest tests/test_adr12_sandbox.py -p no:cacheprovider`, about 90 seconds; its
  Docker tests with `-m docker`, about 20 seconds.

## Task under review

### What M50 builds

- **`evals/adr12/sandbox.py`** (new): the 24 sandbox tasks in five groups (shell, code, files, fetch,
  repair), each with its files, turn limit, egress, checker and good/bad/tricky outcomes; the code
  tasks' visible cases (in the shipped `check.py`) and hidden ones; the verifier program; the budget
  rule (`M15_MEDIANS`, `ceilings`).
- **`evals/adr12/sandbox_arms.py`** (new): `PresetArm` (the general preset, an approver refusing every
  export, the kept workspace read back through `export` and released); `ClaudeContainerArm` (create,
  copy in, start the driver, copy out, remove); `DockerCli` (an allow list of docker calls, output
  bounded); `Verifier`; `sweep`; `fetch_witness`; `read_back` (the checked tar, parsed in memory).
- **`evals/adr12/claude_driver.py`, `Dockerfile`, `.dockerignore`** (new): the program the Claude
  container runs, and its image.
- **`evals/adr12/harness.py`, `report.py`, `arms.py`, `run_comparison.py`:** sandbox rows, the
  verifier and witness, each run guarded, a partial report on a stop, a closing sweep; the report's
  task count, setups, budget rule and ceilings, networking asymmetry, preset version, image digests,
  verification and witness; the budget rule applied to the M15 kinds on this SDK's arm;
  `budget_exceeded` as its own status; `--set`.
- **`evals/adr12/README.md`, `requirements.txt`**, and the live report
  `evals/adr12/reports/adr12-comparison-2026-11-08.{json,md}`.

### The pre-review's findings, answered (DECISION-b944d3ab, DECISION-02d5d160)

A fresh Sonnet 5.5 subagent probed M50 at runtime; F1, F2 and F5 were reproduced by the author
first-hand. Each repair has a test and a mutant (appendix).

| Finding | Repair |
|---|---|
| F1 (blocking): the export approver died on a raising `decide` or `interruptions`; the run waited for the approval timeout, then the arm raised. | The approver retries each failed read or refusal and sets a request aside only once its refusal is recorded; its end never replaces the run's. |
| F2 (blocking): `check.txt` could be forged (by hand, at import, through a shadowing package) and a table of the visible answers passed `check.py`. | **Owner:** a verifier container. After a code task, the files read back and a pristine verifier go into a fresh container of the workspace image (`--network none`, `--cap-drop ALL`, no new privileges, bounded pids and memory); each case, visible and hidden, loads the named file by its path in a child process; the result carries a nonce no workspace code sees. `check.txt` is not evidence. |
| F3: the Claude arm read docker output whole. | Output bounded per call. Repairing it found a hang: a pipe left unread past its bound stays paused, so a killed process's wait never returned; both pipes are drained after a kill. |
| F4: a cancel during `create` could leave a container. | Removal shielded from cancellation and retried after a create that may finish late; a closing sweep by label. |
| F5: a key printed in the container reached the report. | The arm redacts its key from every error and answer. |
| F6: one raising run lost every row. | Each run guarded into a failed row; a release has its own guard; a stopped comparison writes what it measured. |
| F7: `--pull always` and `rm` of any name were accepted; the static scan looked at list heads only. | Exactly `--pull never`; `rm -f` only of the comparison's own names; a listing only of its own labels; every list element scanned. |
| F8: a page or a title typed from memory passed the fetch checkers. | **Owner:** a witness. The harness fetches `https://example.com/` itself once per comparison (verified against certifi); `fetch-page` must hold that body, `fetch-title-into-config` its title and byte length. |

### How the author checked it

- **Tests:** `tests/test_adr12_sandbox.py`, 184 tests: 181 offline and 3 Docker-marked; `tests/test_adr12_comparison.py`'s 34 M15 tests unchanged. The harness code was written before its tests, against the
  specification; the mutation runs are what show each test catches what it claims.
- **Mutation:** 35 mutants after the repairs, one exact edit each, every file restored
  byte for byte (appendix); a hang counts as a kill only under a 420 s bound with the process tree
  ended. The first run's survivors and how they were closed: the release (its test could not see it,
  since `aclose()` removes a kept workspace anyway; the test now observes the release) and the drained
  pipes (its test now bounds the call's time).
- **Docker:** the image built twice with the owner's approval, the second time adding `bash`: live
  run 1 showed Claude Code's Bash tool needs it, every Claude-arm command having failed without it.
- **Live, every sandbox task once per arm** on `bedrock.anthropic.claude-haiku-4-5`: the committed
  report is the final pass on the final code -- 46 of 48 checkers passed (Claude arm 24 of 24, this
  SDK's arm 22 of 24); three of this SDK's runs ended `budget_exceeded` under the FR-179 rule, two of
  them unfinished. Earlier passes on earlier code are not in the repository.
- **Older suites:** both harness test files, 215 passed; the full suite in the regression gate.
- **Gates** on source hash `937e7a6552724faab59237e893e422198ac9dae3841832bc754213235f9be084`: unit 215 passed, 3 deselected in 99.98s (0:01:39); regression 3638 passed, 174 deselected in 2333.30s (0:38:53); docker 174 passed, 1076 deselected in 4882.46s (1:21:22), each exit 0 with empty stderr and no warnings, kept awake. Check `git status --untracked-files=all` yourself.

### Edits to approved tests

- None: `tests/test_adr12_comparison.py` (M15) is unchanged; check with `git diff --stat HEAD -- tests/`.
- No new table, so `tests/conftest.py`'s `_TRACKED` (NFR-38) needs no entry.

## Attack these first

Report each as "probed: <result>" or "not probed: <why>".

1. **The verifier.** Can any workspace a run could leave make it report `pass` without the named
   module computing every case -- the nonce, the child processes, loading by path, the hidden cases,
   files the verifier itself reads, a `/work` that is huge, odd or empty? Does a correct solution ever
   fail it (types after JSON, floats, a module that prints, one that reads its own folder)?
2. **The checkers.** Any checker that accepts a wrong workspace or refuses a reasonable right one;
   any good, bad or tricky outcome that does not test what its comment says.
3. **The witness.** Is a fetch judged fairly on both arms, and can either pass without fetching?
4. **Both arms' read-back.** NFR-43 on this SDK's arm; the checked tar on both; bounds; a workspace
   left with links or special files.
5. **The credential.** The key in argv, the image, the report, any recorded text, any exception.
6. **Containers.** Every exit from both arms and the verifier -- timeout, exception, cancellation at
   each step -- leaves no container; the sweep removes only the comparison's own.
7. **Nothing pulls or builds.** Any path, including the live command.
8. **The budget rule and the report.** FR-179's rule, ceilings and statuses on every kind; the
   report states everything FR-179 names, and every acceptance criterion covers every task present.
9. **Fairness.** Both arms on the same model, turn limits and files; the asymmetries the report
   states (networking, budget, `bash` in the Claude image, the M15 kinds' setup) -- is any unstated?

## Declared limitations: known, recorded, NOT findings

- **Everything declared for M20 to M49,** and the README's rows.
- **Owner's choices:** the decisions above, including the budget rule as written and the M15 kinds'
  setup.
- **Tuning the preset's texts is M51's** (P5A-D10, FR-182): a live run failing a task by the model's
  own choice -- `fetch-page` giving up when `curl` is absent, a repair left unfinished at its budget --
  is a measurement.
- **The Claude image has `bash`; the workspace image does not:** Claude Code's Bash tool requires it,
  this SDK's `run_command` runs argv directly. Stated in the report's setup.
- **The harness runs its own verifier program and the test-authored outcomes on the host in the
  offline tests:** never a model's workspace; live, the verifier runs in its container.
- **Runs are not persisted:** the arms use the in-memory store, so FR-179's "removed by their recorded
  ids" applies to the workspaces (released by id) and containers (removed by name).

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. List every finding, and mark
  which you would block on.
- **Save every probe script** under a folder of your own in the system temp directory, and say where.
  Leave nothing in the repository, not even a scratch file at its root.
- **Clean up** every container, folder, process and lock you create, and say what you removed. Stop
  every process you started before you report.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M50-adr12-sandbox --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M50-adr12-sandbox --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, the result for each attack item, and your verdict. Do
not commit or publish anything.

## Appendix: the author's mutants

- approver approves
- approver dies on an error
- approver's end replaces the run's
- read_back unchecked
- not kept
- no egress
- not released
- release unguarded
- budget status lost
- create may pull
- rm of anyone's
- build allowed
- output unbounded
- pipes not drained (killed once its test bounded the call's time)
- key to every call
- no retry after a cancelled create
- no redaction
- silent driver
- files owned by root
- no Bash
- verifier with network
- verifier container kept
- checker trusts anything
- any nonce
- no hidden cases
- check.py may change
- harness skips verification
- fetch by title only
- ceiling nine times
- checker unguarded
- checked by answer only
- a raising run crashes the comparison
- no partial report
- M15 arm ignores budget
- driver leaks the message
