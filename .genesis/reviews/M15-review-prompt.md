You are the independent L4 reviewer for a bounded task in a Genesis-governed repository. You did
not write this code and you must not trust its author's claims about it.

**Use a fresh model.** Across M5 to M14, nearly every defect was found in a region the previous
reviewer had not examined. If you have reviewed this project before, say so and ask for a
different session. M14 was implemented by Opus 5 and reviewed by Opus 5 (round 1) and Fable 5.1
(round 2); M15 was implemented by Opus 5. Re-derive the requirements from SPEC.md rather than
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
- **Install:** `.venv\Scripts\python.exe -m pip install -r requirements.txt`, and for this
  milestone also `-r evals/adr12/requirements.txt` (claude-agent-sdk, pinned) and
  `npm install --prefix evals/adr12` (the Claude Code CLI, pinned).
- **Host:** Windows Developer Mode must be on (D7).

**Only one test session at a time, and nothing else in parallel with one.** Every pytest session
compares the store's run and artifact ids at its start and end (AC-44), the development database
is shared, and M14's timing tests assert lower bounds. Before running tests or gates, check that
no other pytest, gate or comparison process is running. Run your own probes with
`-p no:cacheprovider`: Genesis hashes .pytest_cache, so a cache write can make a gate read stale
with no code change.

**The live comparison spends tokens.** `evals/adr12/run_comparison.py` is the only entry point
that calls the gateway or starts the CLI. The gate never does. Re-run it only if you mean to.

## Task under review

**M15-adr12-comparison**: the first ADR-12 comparison measures this SDK against the raw Claude
Agent SDK on 12 file tasks with the same model and gateway, and records what it found.

First review round. The requirements are FR-61, FR-62, FR-63, NFR-20, AC-48, AC-49 and AC-50,
quoted below. Decisions that bear on it: P2-D13 (the shape of the comparison), DECISION-4d435ed5
(M15 runs before the increment 2 specification), and DECISION-05ee16ad (the Claude Code CLI is
installed locally and pinned). The pre-flight is KNOWLEDGE-d94aee6c, recorded before any live run
as AC-49 requires. D13 applies (DECISION-2bad84bb): in-process caller code is trusted.

**The M15 code is not committed.** HEAD is `6cacf5e` (M14). Review it with:

```bash
git diff 6cacf5e -- evals tests pyproject.toml .gitignore
git status --short   # new: evals/, tests/test_adr12_comparison.py
```

### Requirements it claims to satisfy (verbatim from SPEC.md)

- **FR-61:** `evals/adr12/` holds a comparison harness outside the `agentsdk` package, so not in the wheel (FR-22), and a set of 12 tasks over files in a local fixture folder, each with a deterministic checker: single-step answers, multi-step tool use, long work within a turn limit, and a task the agent should decline. Web tasks are left out: FR-38 refuses loopback addresses, and the Claude Agent SDK's WebFetch upgrades to HTTPS, sends the host name to Anthropic for a safety check and summarises pages. Each task runs on two arms, both with `bedrock.anthropic.claude-haiku-4-5` through the same gateway and the same turn limit, each confined to its own copy of the fixture folder outside the repository: this SDK with its Tier 1 file tools (M10), and the raw Claude Agent SDK configured with `tools=["Read", "Glob", "Grep"]`, `permission_mode="dontAsk"` and `setting_sources=[]`, pointed at the gateway through `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL` and `ANTHROPIC_DEFAULT_HAIKU_MODEL`, with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (P2-D13).
- **FR-62:** For every task, arm and repetition, the harness records whether the checker passed, the terminal status, turns, the token counts M9 defines (the Claude arm's normalised as prompt = input + cache read + cache creation), wall time, and cost computed from the same `ModelPricing` for both arms; the Claude Agent SDK's own cost estimate is recorded separately and labelled as its estimate. It writes a JSON result file and a Markdown report to `evals/adr12/reports/`, which is not gitignored, naming the model, the gateway model id, both SDK versions, each arm's wire format and the date. Gaps are reported as measured, never as a pass or fail of this SDK: the report is an input to the second increment's specification and to Phase 5A. The harness's own tests live in `tests/`, so the gate collects them.
- **FR-63:** M15 starts with a recorded pre-flight: whether the Claude Agent SDK runs against the configured gateway's Anthropic Messages endpoint (KNOWLEDGE-54523a58) with the existing credential and `claude-haiku-4-5`, including whether the gateway forwards the `anthropic-beta` header the Claude Code CLI sends. If it cannot, M15 stops for an owner decision; no comparison is built from documentation instead.
- **NFR-20:** The comparison is never an SDK dependency: `claude-agent-sdk` appears only in the `eval` extra and in `evals/adr12/requirements.txt`, no module under `agentsdk/` imports it, and nothing the harness writes contains a credential (AC-19).
- **AC-48:** The harness runs its whole task set offline, with a scripted model on this SDK's arm and a scripted stand-in for the other, and writes a report; every checker passes a known-good fixture and fails a known-bad one; the Claude arm's configuration carries exactly FR-61's tools, permission mode, setting sources and environment; both arms are costed from one `ModelPricing`; and no module under `agentsdk/` imports `claude_agent_sdk`.
- **AC-49:** FR-63's pre-flight and its outcome are recorded as a Genesis knowledge record before any live comparison runs.
- **AC-50:** A live report covers 12 tasks x 2 arms x 3 repetitions with every FR-62 measure, a missing measure marked unavailable rather than zero; names the model, versions, wire formats and date; lies where `git ls-files --others --exclude-standard` lists it, so AC-19's credential scan reads it, and passes that scan; and its findings are recorded as a Genesis knowledge record before M15 is approved.

### Standing invariants that constrain every task

- Every `ToolResult` carries exactly one `ContentProvenance`.
- Every persisted row carries non-null `tenant_id` and `project_id`.
- `ToolExecutor` order is fixed: resolve, validate, permission, execute.
- No credential enters model context, a persisted row, or a `RunEvent` payload.
- Application code calls `Runner.run()` and nothing else.
- Only `AgentSDKError` subclasses may escape `ModelClient.send()`.
- A boundary's error path must not itself be able to raise.
- A cancellation is recorded, then re-raised (INVARIANT-73299c7c).

### Files in scope

- `evals/adr12/tasks.py`: the 12 tasks, their checkers, and each task's known-good, known-bad and
  tricky answers.
- `evals/adr12/fixture/`: 7 files the tasks read; copied per run.
- `evals/adr12/arms.py`: both arms, each taking its runner as an argument.
- `evals/adr12/harness.py`: the run loop, the per-row measures, the costing.
- `evals/adr12/report.py`: the JSON and Markdown.
- `evals/adr12/run_comparison.py`: the live command.
- `evals/adr12/requirements.txt`, `package.json`, `package-lock.json`, `README.md`, `reports/`.
- `tests/test_adr12_comparison.py` (new, 34 tests).
- `pyproject.toml`: the `eval` extra. `.gitignore`: `evals/adr12/node_modules/`.
- **No file of an approved milestone was changed.** `agentsdk/` is untouched by M15.

## What the author ran

1. **Pre-flight (FR-63, AC-49), recorded as KNOWLEDGE-d94aee6c before any live run:**
   - **Baseline:** 1199 passed at 6cacf5e.
   - **The gateway, probed directly:** HTTP 200 over the Anthropic Messages endpoint with an
     x-api-key header, with an Authorization Bearer header, and with the anthropic-beta header
     the CLI sends; the plain id `claude-haiku-4-5` is refused with HTTP 400, so the gateway id
     is required. Usage came back with input, output, cache read and cache creation counts.
   - **The Claude Agent SDK itself:** installed 0.2.152; the CLI it spawns was absent from the
     machine, so the owner chose a local pinned install (DECISION-05ee16ad). Through it, the arm
     answered a one-file question correctly in 2 turns, is_error false, its own estimate 0.00607,
     wall time 5.1 s.
2. **Tests first.** Against no harness: 18 failed, 2 passed, 1 skipped. The failures were 12
   missing `evals.adr12.tasks`, 3 missing `evals.adr12.arms`, a missing `eval` extra, a missing
   `reports/`, and a missing live entry point. The 2 passes were guards that were already true.
3. **After the harness:** 32 passed, then 34 after A4's tests.
4. **Full suite:** 1231 passed in 216.43 s (1199 + 32; the two later tests came after).
5. **Mutation run**, 20 mutants against the gate, every file restored and SHA-256 verified, tree
   restored, no mutant text left: 16 of 20 killed, then 19 of 20 after A4. T1 is equivalent (A3).
6. **Live smoke run**, one task, both arms: 2 runs, both checkers passed, 8.9 s.
7. **Live comparison (AC-50).** Run twice; the first run is A10, and the report it wrote was
   replaced. The report under review is `evals/adr12/reports/adr12-comparison-2026-09-16.json`
   and its Markdown, dated 2026-09-16T09:13:04Z, findings recorded as KNOWLEDGE-ca9ae048.
   - **Shape:** 12 tasks x 2 arms x 3 repetitions, 72 rows, 71 checkers passed. No credential and
     no gateway host in either file.
   - **This SDK:** 35 of 36, 69687 prompt and 3473 completion tokens, cost 0.0871, median 2 turns,
     median wall 3723 ms.
   - **The Claude Agent SDK:** 36 of 36, 303691 prompt and 10796 completion tokens, cost 0.3577,
     median 3 turns, median wall 6140 ms, its own estimate agreeing with the computed cost because
     the price list is the model's published rate (A5).
   - **By kind:** both arms passed every single-step, long and decline run. The one failure is a
     multi-step run (A12).
8. **Gates.** Both green on source hash `ee9313ff`, the hash KICKOFF.md records, from one
   `genesis gate` run at 15:32:41Z.
   - `unit`: `.venv/Scripts/python.exe -m pytest tests/test_adr12_comparison.py`, exit 0,
     34 passed in 8.31 s, evidence `.genesis/evidence/M15-adr12-comparison-unit.json`.
   - `regression`: `.venv/Scripts/python.exe -m pytest -q`, exit 0, 1233 passed in 166.01 s,
     evidence `.genesis/evidence/M15-adr12-comparison-regression.json`.
   - Two earlier regression attempts on this same hash were killed at the 600 s timeout with
     SIGTERM while the machine had under 1.3 GB free. Nothing in the tree changed between them and
     this run; the difference was free memory. The evidence files hold only the passing run.

## Issues the author found during M15

| id | finding | disposition |
|---|---|---|
| A1 | The live entry point called `load_dotenv()`, which searches upward from the script's own folder. A test that stripped BASE_URL and MODEL_API_KEY from the environment still found the repository's .env, so the entry point ran a real comparison until the test's 120 s timeout killed it, spending tokens and orphaning a CLI process | fixed: it loads only a .env in the folder it is run from, so running it elsewhere finds no configuration and refuses. The orphaned process and its temp folder were removed; no report had been written. Mutant E1 covers the refusal |
| A2 | The offline stand-ins answered in sequence while the harness runs repetition, then task, then arm, so most rows received another task's answer | fixed in the gate: both stand-ins answer by prompt. No harness change |
| A3 | Mutant T1, removing the checkers' empty-answer guard, survives | equivalent, not a gap: with the guard gone an empty answer still fails, because it contains none of the wanted text. The guard matters only for a checker with no groups at all, which none has. Kept as defence, and declared rather than claimed |
| A4 | Three real gaps the mutation run found: no task's known-bad answer exercised a checker's refusal list (T2), the offline stand-ins never read the fixture so an uncopied run folder went unnoticed (H5), and the report's date was checked in the JSON but not in the Markdown (R1) | four tasks gained a tricky answer that says the right thing and something refused; a test asserts every run folder holds the fixture; the date is asserted in the Markdown. All three mutants then died |
| A5 | The Claude arm's own cost estimate equalled the harness's computed cost exactly in the live smoke run | not a defect: the price list this comparison uses is the model's published rate, so both agree. The gate now asserts the estimate is its own field carrying its own value, rather than asserting the two differ |
| A6 | FR-61 says "this SDK with its Tier 1 file tools (M10)", and M10 ships four: read, list_directory, glob and grep. The Claude arm has Read, Glob and Grep | this SDK's arm is given read_file, glob_files and grep_files, leaving list_directory out, so neither arm has a capability the other lacks. Declared here because it is a fairness judgement, not a requirement |
| A7 | The VSCode extension ships its own claude.exe (2.1.272) in its resources | not used: it is not on PATH and moves with the extension. The comparison pins 2.1.267 locally, and the report names it |
| A8 | The SDK ships no price list (FR-30), so the comparison has to choose one | the prices live in run_comparison.py, are recorded in the report's metadata, and are the model's published rates. Both arms are costed from that one list |
| A9 | The decline task rests on both arms having only read-only tools | that is FR-61's configuration for both arms, and the gate asserts the Claude arm's tools. A future arm with a write tool would need the task revisited |
| A10 | The first live run read every correct decline as a failure: the arms answered "I do not have the ability to delete files" and "I do not have a tool available to delete files", and the checker wanted cannot, unable, no tool or read-only. It measured the checker, not the arms (5 of 72 rows) | the decline checker now accepts the spellings the arms actually used, the known-good answer is one of them, and the whole comparison was rerun so one report reflects one checker version. The claimed-deletion and tricky answers still fail. The first report was replaced, not merged |
| A11 | The mutation run's E1 mutant, which makes the live entry point skip its configuration check, wrote a report into the repository's reports folder during the run | the entry point test now passes a temporary reports folder, so a mutant cannot write there. The debris was overwritten by the live run and the report was checked afterwards |
| A12 | One row failed: this SDK's arm on key-holding-the-region repetition 2, status failed, error ModelProviderUnavailable, with no answer. The same task passed on both arms in the other two repetitions | kept as measured rather than rerun for a cleaner number: it is a transient gateway outage, of the same class as KNOWLEDGE-ea66a3c4 from M13, and AC-50 asks for the measures, not for every checker to pass |
| A13 | That failed run took 2555071 ms, about 42.6 minutes, with one turn recorded, which is why the second live comparison took about 50 minutes rather than the first run's 7.5 | recorded as a finding rather than fixed here: the HTTP client's 60 second timeout is a per read timeout, not a deadline for the whole call, and the retry policy is 3 attempts, so a trickling response can stall a run. Nothing bounds one model call end to end today, and the harness adds no deadline either. This is an input to the second increment (KNOWLEDGE-ca9ae048), not an M15 change |

## Attack these first

**Report every item below as "probed", with what you ran and saw, or "not probed", with why.** An
approval that is silent on an item will be read as not probed.

1. **The checkers.** Are they gameable or too strict? Try answers a real model would give: extra
   prose, a different order, hedging, the right number in a wrong sentence, a refusal phrased as
   a question. Does any checker pass an answer that does not answer the task, or fail one that
   does? Read the fixture and confirm every task's expected answer is what the files actually say.
2. **Fairness between the arms.** Same prompt, same turn limit, same model, same reach over the
   files? Compare the instructions each arm receives, the tools each has, what a turn means on
   each side, and whether either arm can see anything outside its run folder, including the
   repository. Try to show one arm advantaged by the harness rather than by its SDK.
3. **The measures.** Token normalisation against the raw usage each side reports; cost against
   `call_cost` and the recorded prices; wall time against what it includes; turns against the
   events. Does a missing measure ever read as 0? Does a failed run still produce a complete row?
4. **The report.** Does it claim anything the rows do not support? Does it name the model,
   versions, wire formats and date? Does it stay a measurement rather than a verdict? Does it
   carry any credential, host or file content it should not (AC-19, AC-50)?
5. **NFR-20 and packaging.** Nothing under `agentsdk/` imports `claude_agent_sdk`; the pins live
   only in the `eval` extra and the harness requirements; the wheel excludes `evals/`.
6. **The live report (AC-50).** 12 x 2 x 3 rows, every measure, listed by
   `git ls-files --others --exclude-standard`, and passing the credential scan.
7. **The offline path (AC-48).** Does the gate genuinely avoid the gateway and the CLI? Try it
   with no network and with the CLI removed from its folder.

## Declared limitations: known, recorded, NOT findings

- **The offline stand-ins do not exercise a real tool loop.** They answer from a mapping; the live
  run is what exercises the arms end to end.
- **No web tasks** (FR-61), one model, one gateway, one machine.
- **The checkers are substring-based**, deliberately: a model-graded checker would make the
  comparison depend on a third model.
- **The prices are chosen, not authoritative** (A8).
- **The CLI path is Windows-specific** (claude.exe under node_modules), as is the rest of this
  host's setup (D7, DECISION-16fd5eb5).
- **A3, A6, A7, A9** above.
- **Older `independent-review` gates** compute stale because the repository hash moved.

## Rules

- **Do not fix the code.** Report defects; the implementing session repairs them.
- **Gates are computed, never narrated.** Paste real command output for anything you assert.
- **Approve if it is sound.** A defect must be reachable and must matter. Latent, out-of-scope or
  cosmetic findings are caveats in your reason, not blockers.
- **Restore every file you mutate** and verify SHA-256, restoring in a `finally`; kill the whole
  process tree on a timeout (`taskkill /T`).
- **Clean up** every run, report, temp folder and process you create, and say what you removed.
- **Report every probe**, including ones that showed nothing.

## Recording your decision

Use **single quotes** around `--reason`, and keep apostrophes, backticks and `$` out of it.

```bash
# pass
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control approve . M15-adr12-comparison --gate independent-review \
  --human '<your name>' --reason '<what you verified, and any caveat>'

# fail
PATH="$PWD/.venv/Scripts:$PATH" node ~/Desktop/genesis-kit/tools/genesis.mjs \
  control reject . M15-adr12-comparison --human '<your name>' --reason '<the defect>'
```

Then report: what you checked, what you ran, what you found per attack item, and your verdict.
