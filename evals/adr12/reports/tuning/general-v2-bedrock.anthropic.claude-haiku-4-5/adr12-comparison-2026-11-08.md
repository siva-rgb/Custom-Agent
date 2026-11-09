# ADR-12 comparison

- Date: 2026-11-08T15:14:48+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 1
- Tasks: 24

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v2 | OpenAI-compatible chat completions |

## Tasks and setups

24 tasks: 24 sandbox.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v2.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T15:14:48+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 22 of 24 | 24 | 6.0 | 18336.7 | 0.435798 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 22 | 92% | 24 | budget_exceeded 3, completed 21 | 6.0 | 366510 | 0.435798 | 18336.7 |
| all | agentsdk | 22 | 92% | 24 | budget_exceeded 3, completed 21 | 6.0 | 366510 | 0.435798 | 18336.7 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | yes | completed | 5 | 14691.2 | 0.012637 | unavailable | Done! I've counted the lines in logs/app.log that contain the word "ERROR" and found **7** lines. The count has been written to answer.txt as digits only. |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 15410.7 | 0.010534 | unavailable | Perfect! The task is complete. I've created **unique.txt** with: - All distinct names from names.txt - Sorted alphabetically - Blank lines skipped - One name... |
| largest-file | sandbox | agentsdk | yes | completed | 4 | 10036.3 | 0.009977 | unavailable | Done! I found that **gamma.json** is the largest file in the data folder at 3013 bytes, and its relative path has been written to answer.txt. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 22800.1 | 0.022730 | unavailable | Perfect! I've successfully completed the task. All .txt files in the notes folder have been renamed to .md files: - `budget.txt` → `budget.md` - `design.txt`... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 7 | 21857.0 | 0.023917 | unavailable | Perfect! I have successfully organized all the report files. Here's what was done: **Summary:** - Found 5 report files in the `reports` folder dated from Jan... |
| top-words | sandbox | agentsdk | yes | completed | 5 | 15134.8 | 0.015226 | unavailable | Excellent! The task is complete. Here's what I found: **Result:** - **the**: 6 occurrences - **dog**: 4 occurrences (tied with fox, but comes first alphabeti... |
| parse-duration | sandbox | agentsdk | yes | completed | 6 | 26507.5 | 0.021066 | unavailable | Perfect! The task is complete. **Result:** I've successfully implemented the `seconds(text)` function in `duration.py` that parses duration strings and retur... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 19467.5 | 0.020102 | unavailable | Excellent! The solution is complete. I've successfully created `roman.py` with a `to_int(numeral)` function that converts Roman numerals to integers. The fun... |
| isbn10 | sandbox | agentsdk | yes | budget_exceeded | 7 | 24844.3 | 0.025879 | unavailable | unavailable |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 17044.4 | 0.022085 | unavailable | Excellent! The task is complete. Here's what I did: **Result:** Created `intervals.py` with a `merge(pairs)` function that: - Takes a list of [start, end] pa... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 19289.7 | 0.021899 | unavailable | Excellent! The task is complete. ## Summary I've successfully written `wrap.py` with a `wrap(text, width)` function that: - Breaks text into lines of at most... |
| category-totals | sandbox | agentsdk | yes | completed | 6 | 20175.4 | 0.020514 | unavailable | Perfect! The task is complete. **Summary:** I've successfully created `totals.py` with a `totals(path)` function that: 1. Reads a CSV file with `category` an... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 10234.1 | 0.010577 | unavailable | Perfect! I've successfully changed the port in the [server] section to 9090. All other settings remain unchanged: - [server] host stays as 0.0.0.0 - [metrics... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 11714.7 | 0.010765 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file is now valid JSON with all other fields kept exactly as t... |
| rename-company | sandbox | agentsdk | yes | completed | 6 | 18712.7 | 0.018545 | unavailable | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs folder. Here's a summary: **Changes mad... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 12963.2 | 0.010546 | unavailable | Done! I've added a new section for version 1.3.0 to CHANGELOG.md above the 1.2.0 section with the entry "- Added the export command", matching the existing s... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 14718.6 | 0.014724 | unavailable | Perfect! The task is complete. I have successfully: 1. Added a new `initials` column to people.csv 2. Populated it with each person's first and last initials... |
| fetch-page | sandbox | agentsdk | yes | completed | 4 | 14986.8 | 0.011291 | unavailable | Perfect! I have successfully: 1. **Downloaded** https://example.com/ using Python's urllib 2. **Saved the page** exactly as served to `page.html` 3. **Saved ... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 4 | 15095.5 | 0.011710 | unavailable | Done! I successfully: 1. Fetched the page at https://example.com/ 2. Extracted the title text "Example Domain" from the `<title>` element 3. Calculated the r... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 17960.6 | 0.022382 | unavailable | ## Fixed! The fault was in `stats.py` in the `average` function. It was dividing by `len(values) + 1` instead of `len(values)`. **The bug:** The denominator ... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 8 | 19983.5 | 0.026530 | unavailable | unavailable |
| repair-dedupe | sandbox | agentsdk | yes | completed | 7 | 20909.8 | 0.023697 | unavailable | Excellent! The check now passes successfully. ## Summary The fault in `emails.py` was in the `distinct()` function. It was not normalizing email addresses be... |
| repair-restock | sandbox | agentsdk | no | budget_exceeded | 7 | 21944.7 | 0.025180 | unavailable | unavailable |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 19927.0 | 0.023285 | unavailable | Excellent! The check passes now. ## Summary The fault in `slugs.py` was in the `slugify` function. The original implementation only replaced non-alphanumeric... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
