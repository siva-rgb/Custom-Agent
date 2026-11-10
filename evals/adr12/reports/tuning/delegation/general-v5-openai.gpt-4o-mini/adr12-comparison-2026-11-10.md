# ADR-12 comparison

- Date: 2026-11-10T03:49:14+00:00
- Model, as the gateway names it: openai.gpt-4o-mini
- Repetitions per task and arm: 1
- Tasks: 6

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v5 | OpenAI-compatible chat completions |

## Tasks and setups

6 tasks: 6 delegation.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| delegation | 6 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash; the delegating preset (delegate=True), each child a general subagent in a workspace of its own with the files its call named | 0.203690 | 169050 |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median; the delegation kind 10 times the sandbox kind's median on this SDK's arm in M51 (adr12-comparison-2026-11-09.json), a child's share reserved from its parent's. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v5.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 4 of 6 | 6 | 5.0 | 22398.8 | 0.011278200 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| delegation | agentsdk | 4 | 67% | 6 | completed 6 | 5.0 | 103994 | 0.011278200 | 22398.8 |
| all | agentsdk | 4 | 67% | 6 | completed 6 | 5.0 | 103994 | 0.011278200 | 22398.8 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| word-counts | delegation | agentsdk | no | completed | 5 | 14482.2 | 0.000945150 | unavailable | I have counted the words in each file under the "chapters/" directory. Here are the counts: - `chapters/four.txt`: 8 words - `chapters/one.txt`: 7 words - `c... |
| csv-totals | delegation | agentsdk | no | completed | 15 | 88446.8 | 0.005107350 | unavailable | I created `totals.csv` with the total amounts for each region, but the values calculated were all zero, which indicates that the amounts might not have been ... |
| log-errors | delegation | agentsdk | yes | completed | 5 | 13546.4 | 0.000996000 | unavailable | I've counted the lines with the level "ERROR" in each log file and created a JSON object mapping each service to its count. Here are the results: - **api**: ... |
| ini-to-json | delegation | agentsdk | yes | completed | 4 | 18126.5 | 0.000826350 | unavailable | I have created the following JSON files based on the INI files in the `configs/` directory: - `configs/app.json`: Contains the structured data from `app.ini`... |
| three-functions | delegation | agentsdk | yes | completed | 5 | 26671.0 | 0.001189950 | unavailable | I have written three independent modules as specified: 1. **roman.py**: Contains the `to_int(numeral)` function that converts a Roman numeral to an integer. ... |
| fix-three-bugs | delegation | agentsdk | yes | completed | 9 | 48710.0 | 0.002213400 | unavailable | I have fixed the faults in each module: 1. **stats.py**: The calculation in the `average` function was changed from dividing by `len(values) + 1` to `len(val... |

## Delegation

How it is measured: each row reports whether its run delegated and how many children it ran, never a condition of passing: on this SDK's arm the runs whose parent_run_id is the run's, and its usage the whole run's, children included; on the Claude arm its Agent calls whose result was not an error, and its usage as the CLI's result reports it.

| arm | runs | delegated | children in all |
|---|---|---|---|
| agentsdk | 6 | 0 | 0 |

| task | arm | repetition | passed | delegated | children |
|---|---|---|---|---|---|
| word-counts | agentsdk | 1 | no | no | 0 |
| csv-totals | agentsdk | 1 | no | no | 0 |
| log-errors | agentsdk | 1 | yes | no | 0 |
| ini-to-json | agentsdk | 1 | yes | no | 0 |
| three-functions | agentsdk | 1 | yes | no | 0 |
| fix-three-bugs | agentsdk | 1 | yes | no | 0 |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
