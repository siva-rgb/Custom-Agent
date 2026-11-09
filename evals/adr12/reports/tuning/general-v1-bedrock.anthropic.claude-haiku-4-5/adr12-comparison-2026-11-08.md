# ADR-12 comparison

- Date: 2026-11-08T14:54:43+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 1
- Tasks: 24

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v1 | OpenAI-compatible chat completions |

## Tasks and setups

24 tasks: 24 sandbox.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v1.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T14:54:43+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 21 of 24 | 24 | 6.0 | 19315.9 | 0.453707 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 21 | 88% | 24 | budget_exceeded 3, completed 21 | 6.0 | 378695 | 0.453707 | 19315.9 |
| all | agentsdk | 21 | 88% | 24 | budget_exceeded 3, completed 21 | 6.0 | 378695 | 0.453707 | 19315.9 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | yes | completed | 7 | 20577.5 | 0.020533 | unavailable | Done! I've counted the lines containing "ERROR" in logs/app.log. There are **7** lines with the word "ERROR", and I've written this count to **answer.txt** w... |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 10528.4 | 0.009659 | unavailable | Perfect! I've successfully created **unique.txt** with the following results: - **anouk** - **beatrix** - **elif** - **jonas** - **mara** - **zoe** The scrip... |
| largest-file | sandbox | agentsdk | yes | completed | 5 | 12739.1 | 0.012070 | unavailable | Done! The largest file in the data folder is **gamma.json** with 3013 bytes. Its relative path has been written to answer.txt as `data/gamma.json`. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 27085.1 | 0.021433 | unavailable | Perfect! I've successfully renamed all .txt files in the notes folder to .md files: - `budget.txt` → `budget.md` - `design.txt` → `design.md` - `kickoff.txt`... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 8 | 23104.6 | 0.024407 | unavailable | Perfect! I've successfully reorganized the reports folder. Here's what was done: **Result:** - All 5 CSV files have been moved into year-month folders - The ... |
| top-words | sandbox | agentsdk | yes | completed | 6 | 18391.1 | 0.016756 | unavailable | Excellent! The task is complete. Here's what I found: **Result:** The three most frequent words in text.txt have been written to top.txt: 1. **the** - 6 occu... |
| parse-duration | sandbox | agentsdk | yes | completed | 6 | 18216.1 | 0.020012 | unavailable | Excellent! The implementation is complete and all test cases pass. Here's a summary: ## Result I successfully implemented the `seconds(text)` function in `du... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 19836.1 | 0.019603 | unavailable | Excellent! The task is complete. **Summary:** I've created `roman.py` with a `to_int(numeral)` function that converts Roman numerals to integers. The functio... |
| isbn10 | sandbox | agentsdk | yes | completed | 6 | 20394.9 | 0.021509 | unavailable | Excellent! The task is complete. I've successfully created `isbn.py` with a `valid(code)` function that: 1. **Validates ISBN-10 codes** according to the spec... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 16378.8 | 0.021082 | unavailable | Perfect! The implementation is complete and all test cases pass. ## Summary I've successfully created **intervals.py** with a `merge(pairs)` function that: 1... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 19160.4 | 0.022218 | unavailable | Perfect! **Done.** I've created `wrap.py` with a `wrap(text, width)` function that: - Breaks text into lines of at most `width` characters, breaking at space... |
| category-totals | sandbox | agentsdk | yes | completed | 6 | 18564.3 | 0.019898 | unavailable | Excellent! The task is complete. Here's what I've done: **Result:** I successfully wrote `totals.py` with a `totals(path)` function that: 1. Reads a CSV file... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 10863.5 | 0.009973 | unavailable | Perfect! I've successfully changed the [server] section's port from 8080 to 9090. All other settings remain unchanged: - [metrics] port is still 9100 - [metr... |
| add-license-field | sandbox | agentsdk | yes | completed | 5 | 11250.1 | 0.012209 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file is now valid JSON with all other fields preserved exactly... |
| rename-company | sandbox | agentsdk | yes | completed | 7 | 19319.3 | 0.019695 | unavailable | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs folder. **Summary:** - **docs/intro.md*... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 12106.0 | 0.009919 | unavailable | Perfect! I've successfully added a version 1.3.0 section to CHANGELOG.md above the 1.2.0 section. The new section includes the entry "- Added the export comm... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 14950.4 | 0.012985 | unavailable | Perfect! The task is complete. I've successfully added an "initials" column to people.csv with each person's first and last initials in capitals: - Ada Lovel... |
| fetch-page | sandbox | agentsdk | no | budget_exceeded | 7 | 30019.8 | 0.024300 | unavailable | unavailable |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 6 | 21786.9 | 0.017219 | unavailable | Perfect! I have successfully completed the task: **Result:** - Fetched https://example.com/ - Extracted the `<title>` element text: "Example Domain" - Calcul... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 19312.6 | 0.020916 | unavailable | **Fixed!** The fault was in `stats.py` line 3. The `average` function was incorrectly dividing by `len(values) + 1` instead of just `len(values)`. The bug ca... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 8 | 23792.7 | 0.024953 | unavailable | unavailable |
| repair-dedupe | sandbox | agentsdk | yes | completed | 7 | 21303.2 | 0.022087 | unavailable | Excellent! The check passes. ## Summary The fault in `emails.py` was that the `distinct()` function wasn't: 1. **Normalizing case**: Email addresses like 'a@... |
| repair-restock | sandbox | agentsdk | no | budget_exceeded | 8 | 27429.7 | 0.028173 | unavailable | unavailable |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 26007.8 | 0.022098 | unavailable | Perfect! The check passes successfully. ## Summary The fault was in the `slugify` function in `slugs.py`. The original implementation: - Replaced non-alphanu... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
