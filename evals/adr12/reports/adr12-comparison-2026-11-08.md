# ADR-12 comparison

- Date: 2026-11-08T07:09:42+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 1
- Tasks: 24

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v1 | OpenAI-compatible chat completions |
| claude-agent-sdk | claude-agent-sdk 0.2.152 with Claude Code CLI 2.1.267, in adr12-claude:cli-2.1.267 | Anthropic Messages through the Claude Code CLI |

## Tasks and setups

24 tasks: 24 sandbox.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |
| sandbox | 24 | claude-agent-sdk | Read, Glob, Grep, Bash, Write and Edit in a disposable container holding the task's files; it runs as uid 1000 (agent, home /home/agent), and its image adds nodejs, npm and bash (for the Bash tool) to the workspace image's base | none | none |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- Setup text corrected on 2026-11-08 after the run (M50 round 1, note 2): each arm's user was added, read from both images' /etc/passwd; the runs and their results are unchanged.
- The general preset's version: general-v1.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T07:09:42+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
- claude-agent-sdk image: adr12-claude:cli-2.1.267 sha256:f4fbeee64eafdfbdd00a5489749dae25846154e3da8d3a9926353ed94ef83476

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 22 of 24 | 24 | 6.0 | 18400.2 | 0.458231 | unavailable |
| claude-agent-sdk | 24 of 24 | 24 | 8.0 | 17869.3 | 0.32857565 | 0.3285756500000000125 |

## By kind

| kind | arm | passed | runs | terminal statuses | median turns | total cost |
|---|---|---|---|---|---|---|
| sandbox | agentsdk | 22 | 24 | budget_exceeded 3, completed 21 | 6.0 | 0.458231 |
| sandbox | claude-agent-sdk | 24 | 24 | completed 24 | 8.0 | 0.32857565 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | yes | completed | 7 | 23593.3 | 0.020366 | unavailable | Done! I've counted the lines containing "ERROR" in logs/app.log. There are **7** lines with the word "ERROR", and this count (digits only) has been written t... |
| count-error-lines | sandbox | claude-agent-sdk | yes | completed | 3 | 9488.2 | 0.01162440 | 0.011624400000000002 | Done! I found that logs/app.log contains **7 lines with the word "ERROR"** and wrote that count to answer.txt. |
| unique-names | sandbox | agentsdk | yes | completed | 3 | 8934.7 | 0.007313 | unavailable | Perfect! I've successfully created unique.txt with all distinct names sorted alphabetically, skipping blank lines. The result contains: - anouk - beatrix - e... |
| unique-names | sandbox | claude-agent-sdk | yes | completed | 5 | 15060.8 | 0.00752850 | 0.0075285000000000005 | Done! I've created `unique.txt` with the following results: - **Removed duplicates**: mara, jonas, and elif appeared multiple times and are now listed only o... |
| largest-file | sandbox | agentsdk | yes | completed | 5 | 11922.3 | 0.011845 | unavailable | Done! I found that **gamma.json** is the largest file in the data folder with 3013 bytes. Its relative path has been written to **answer.txt** as: `data/gamm... |
| largest-file | sandbox | claude-agent-sdk | yes | completed | 7 | 13638.8 | 0.01102830 | 0.0110283 | Done! The largest file in the data folder is **gamma.json** with **3013 bytes**. The relative path `data/gamma.json` has been written to `answer.txt`. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 20847.9 | 0.021327 | unavailable | Excellent! ✓ All done. I successfully renamed all 4 .txt files in the notes folder to .md files: - budget.txt → budget.md - design.txt → design.md - kickoff.... |
| rename-notes | sandbox | claude-agent-sdk | yes | completed | 3 | 8130.2 | 0.00551560 | 0.0055156 | Perfect! All .txt files in the notes folder have been successfully renamed to .md files: - `budget.txt` → `budget.md` - `design.txt` → `design.md` - `kickoff... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 7 | 19874.8 | 0.020574 | unavailable | Excellent! The task is complete. I've successfully: 1. **Organized all reports** - Moved each `report-YYYY-MM-DD.csv` file into a folder named `YYYY-MM` 2. *... |
| file-reports-by-month | sandbox | claude-agent-sdk | yes | completed | 8 | 20073.1 | 0.01324885 | 0.01324885 | ✅ Done! Your reports folder has been reorganized successfully. Here's what was organized: - **2024-01/** folder: Contains 2 reports (01-15 and 01-31) - **202... |
| top-words | sandbox | agentsdk | yes | completed | 7 | 16401.9 | 0.019593 | unavailable | The export was rejected, but the file has been created in the workspace. Let me verify the results one more time to make sure everything is correct: **Result... |
| top-words | sandbox | claude-agent-sdk | yes | completed | 6 | 13667.8 | 0.00936995 | 0.009369950000000002 | Done! I've analyzed the text.txt file and written the three most frequent words to top.txt: **Word frequencies found:** - "the" appears 6 times - "dog" appea... |
| parse-duration | sandbox | agentsdk | yes | completed | 6 | 16767.8 | 0.020017 | unavailable | Excellent! The task is complete. **Summary:** I implemented the `seconds(text)` function in `duration.py` that: - Parses duration strings with hours (h), min... |
| parse-duration | sandbox | claude-agent-sdk | yes | completed | 10 | 17755.8 | 0.01565655 | 0.015656549999999998 | Perfect! ✅ The implementation is complete and all tests pass! **Summary:** - Implemented the `seconds(text)` function in duration.py that parses duration str... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 15807.6 | 0.019394 | unavailable | Excellent! The task is complete. Here's what I did: **Result:** Created `roman.py` with a `to_int(numeral)` function that converts Roman numerals to integers... |
| roman-numerals | sandbox | claude-agent-sdk | yes | completed | 10 | 23043.1 | 0.02076535 | 0.020765350000000002 | Excellent! ✅ I've successfully completed the task: ## Summary 1. **Created `/work/roman.py`** with a `to_int(numeral)` function that converts Roman numerals ... |
| isbn10 | sandbox | agentsdk | yes | budget_exceeded | 7 | 24382.2 | 0.029326 | unavailable | unavailable |
| isbn10 | sandbox | claude-agent-sdk | yes | completed | 9 | 25038.8 | 0.02276705 | 0.022767050000000004 | Excellent! ✅ All test cases passed! Here's what I created: ## Summary I wrote `isbn.py` with a `valid(code)` function that: 1. **Removes hyphens** from the i... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 17905.9 | 0.020338 | unavailable | Perfect! The solution is complete. I've written `intervals.py` with a `merge` function that: 1. Takes a list of [start, end] pairs 2. Sorts them by start pos... |
| merge-intervals | sandbox | claude-agent-sdk | yes | completed | 10 | 22549.5 | 0.02004450 | 0.0200445 | Excellent! ✅ All done. Here's what I created: ## Solution I wrote `intervals.py` with a `merge(pairs)` function that: 1. **Handles empty input** - returns an... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 20206.9 | 0.020001 | unavailable | Excellent! The task is complete. **Summary:** - Created `wrap.py` with a `wrap(text, width)` function that breaks text into lines of at most `width` characte... |
| word-wrap | sandbox | claude-agent-sdk | yes | completed | 8 | 22589.6 | 0.02075210 | 0.0207521 | Perfect! ✅ I've successfully created `wrap.py` with the `wrap(text, width)` function. Here's what it does: **Function behavior:** - Takes text and a maximum ... |
| category-totals | sandbox | agentsdk | yes | completed | 6 | 23593.7 | 0.020382 | unavailable | Perfect! The task is complete. Let me summarize: **Result:** I created `totals.py` with a function `totals(path)` that: 1. Reads a CSV file with `category` a... |
| category-totals | sandbox | claude-agent-sdk | yes | completed | 10 | 21797.1 | 0.01885095 | 0.01885095 | Excellent! ✅ I've successfully completed the task: 1. **Created `totals.py`** with a `totals(path)` function that: - Reads a CSV file with `category` and `am... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 9449.6 | 0.009788 | unavailable | Perfect! I've successfully changed the [server] section port from 8080 to 9090. All other settings remain unchanged: - The [metrics] port stays at 9100 - The... |
| change-server-port | sandbox | claude-agent-sdk | yes | completed | 4 | 11939.3 | 0.00598860 | 0.0059886 | Perfect! I've successfully changed the port in the [server] section from 8080 to 9090. All other settings remain unchanged: - The [metrics] port stays at 910... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 11008.6 | 0.009788 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The JSON is valid, and all other fields remain exactly as they were. |
| add-license-field | sandbox | claude-agent-sdk | yes | completed | 4 | 9967.8 | 0.00671195 | 0.00671195 | Perfect! I've successfully added the "license": "MIT" field to your package.json file. The file now contains: ```json { "name": "ledger-cli", "version": "1.2... |
| rename-company | sandbox | agentsdk | yes | completed | 6 | 16791.8 | 0.017435 | unavailable | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under docs: **Results:** - **docs/intro.md**: 2 occurr... |
| rename-company | sandbox | claude-agent-sdk | yes | completed | 9 | 13183.6 | 0.01088550 | 0.010885500000000001 | Perfect! I've successfully replaced every occurrence of 'ACME Corp' with 'Globex' in the Markdown files under the docs directory. Here's what was changed: **... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 10304.4 | 0.009903 | unavailable | Perfect! I've successfully added version 1.3.0 to CHANGELOG.md. The new section is positioned above the 1.2.0 section with the entry "- Added the export comm... |
| changelog-entry | sandbox | claude-agent-sdk | yes | completed | 4 | 11061.5 | 0.00620070 | 0.0062007 | Done! I've added a new version 1.3.0 section to the CHANGELOG.md file above the 1.2.0 section with the entry "- Added the export command" in the same style a... |
| initials-column | sandbox | agentsdk | yes | completed | 6 | 15135.0 | 0.016739 | unavailable | The task has been completed successfully. The file **people.csv** has been updated with a new **initials** column that contains each person's first and last ... |
| initials-column | sandbox | claude-agent-sdk | yes | completed | 5 | 13797.0 | 0.00788905 | 0.007889050000000002 | Perfect! I've added the "initials" column to people.csv. The new column contains each person's first and last initials in capitals: - Ada Lovelace → AL - Ala... |
| fetch-page | sandbox | agentsdk | yes | completed | 6 | 19943.8 | 0.015255 | unavailable | Perfect! I've successfully completed the task: 1. **Downloaded the page**: Downloaded https://example.com/ and saved it exactly as served to `page.html` (577... |
| fetch-page | sandbox | claude-agent-sdk | yes | completed | 10 | 22043.0 | 0.01804510 | 0.0180451 | Perfect! ✅ I've successfully completed your request: 1. **Downloaded** https://example.com/ and saved the exact page to **page.html** 2. **Extracted** the HT... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 8 | 29062.9 | 0.024916 | unavailable | Excellent! The task is complete. I have: 1. Fetched the page from the URL specified in site.json (https://example.com/) 2. Extracted the text from the `<titl... |
| fetch-title-into-config | sandbox | claude-agent-sdk | yes | completed | 10 | 21426.7 | 0.01609555 | 0.01609555 | Done! I've successfully: 1. **Read site.json** - Found the URL `https://example.com/` 2. **Fetched the page** - Downloaded the HTML content (577 bytes) 3. **... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 18894.4 | 0.020632 | unavailable | ## Result **Fixed!** The bug was in `stats.py` line 3 of the `average` function: - **Fault**: `return sum(values) / (len(values) + 1)` — dividing by one more... |
| repair-average | sandbox | claude-agent-sdk | yes | completed | 12 | 23396.9 | 0.01856880 | 0.0185688 | ## Fixed! ✓ **The bug:** In `stats.py`, the `average()` function was dividing by `(len(values) + 1)` instead of `len(values)`. **The fix:** Changed line 4 fr... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 8 | 19692.6 | 0.027152 | unavailable | unavailable |
| repair-date-format | sandbox | claude-agent-sdk | yes | completed | 8 | 18214.9 | 0.01276750 | 0.0127675 | Excellent! ✅ The check now passes. **The fault was:** The `iso()` function in `dates.py` was using the wrong date format string. It was using `'%m/%d/%Y'` (M... |
| repair-dedupe | sandbox | agentsdk | yes | completed | 8 | 19461.8 | 0.024925 | unavailable | Excellent! The check now passes. ## Summary The fault in the `emails.py` code was in the `distinct()` function. It needed to: 1. **Normalize whitespace**: St... |
| repair-dedupe | sandbox | claude-agent-sdk | yes | completed | 7 | 14607.3 | 0.01169120 | 0.011691199999999999 | Excellent! The check now passes. Here's what I fixed: **The problem:** The `distinct()` function in `emails.py` was: 1. Not normalizing email addresses (case... |
| repair-restock | sandbox | agentsdk | no | budget_exceeded | 8 | 19274.6 | 0.028269 | unavailable | unavailable |
| repair-restock | sandbox | claude-agent-sdk | yes | completed | 13 | 30014.6 | 0.02171730 | 0.021717300000000002 | ✅ **Fixed!** The issue was in the `inventory.py` file's `to_order()` function. **The Problem:** - The condition `if count > minimum` was backwards - it shoul... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 17234.9 | 0.022953 | unavailable | Excellent! The check now passes. The fault in the original `slugs.py` code was that the `slugify` function: 1. **Did not strip whitespace** from the beginnin... |
| repair-slugify | sandbox | claude-agent-sdk | yes | completed | 9 | 17982.9 | 0.01486230 | 0.0148623 | Excellent! ✅ **Fixed!** The issue was in the `slugify` function in `slugs.py`. The original implementation: - Simply replaced all non-alphanumeric characters... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
