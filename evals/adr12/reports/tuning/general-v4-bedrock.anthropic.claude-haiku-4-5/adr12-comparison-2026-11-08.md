# ADR-12 comparison

- Date: 2026-11-08T15:52:14+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 1
- Tasks: 24

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v4 | OpenAI-compatible chat completions |

## Tasks and setups

24 tasks: 24 sandbox.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v4.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T15:52:14+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 23 of 24 | 24 | 5.5 | 16220.4 | 0.439739 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 23 | 96% | 24 | budget_exceeded 2, completed 22 | 5.5 | 367583 | 0.439739 | 16220.4 |
| all | agentsdk | 23 | 96% | 24 | budget_exceeded 2, completed 22 | 5.5 | 367583 | 0.439739 | 16220.4 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | yes | completed | 5 | 12975.8 | 0.015327 | unavailable | Done! I've counted the lines in logs/app.log that contain the word "ERROR" and found **7** occurrences. This count has been written to answer.txt as digits o... |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 11889.9 | 0.011042 | unavailable | Done! I've created **unique.txt** with 6 distinct names from names.txt, sorted alphabetically and one per line: - anouk - beatrix - elif - jonas - mara - zoe... |
| largest-file | sandbox | agentsdk | yes | completed | 5 | 11887.5 | 0.012786 | unavailable | Done! The largest file in the data folder is **gamma.json** with 3013 bytes. Its relative path `data/gamma.json` has been written to `answer.txt`. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 21776.9 | 0.023101 | unavailable | Perfect! I've successfully renamed all .txt files in the notes folder to .md files. Here's what I did: 1. **Found 4 .txt files**: budget.txt, design.txt, kic... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 5 | 16296.2 | 0.016228 | unavailable | Perfect! The task is complete. Here's what was done: **Summary:** - All 5 report CSV files have been organized into month-based folders - Files are now struc... |
| top-words | sandbox | agentsdk | yes | completed | 5 | 14510.3 | 0.015742 | unavailable | Perfect! The task is complete. **Result:** I've counted all words in text.txt (extracting only runs of letters, ignoring case and punctuation) and written th... |
| parse-duration | sandbox | agentsdk | yes | completed | 6 | 16144.6 | 0.022286 | unavailable | Excellent! The task is complete. Here's what I did: **Result**: Successfully implemented the `seconds(text)` function in `duration.py` that parses duration s... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 15970.0 | 0.020605 | unavailable | Excellent! The task is complete. Here's what I did: ## Summary I wrote **roman.py** with a `to_int(numeral)` function that converts Roman numerals to integer... |
| isbn10 | sandbox | agentsdk | yes | completed | 6 | 17878.5 | 0.022573 | unavailable | Perfect! The task is complete. Here's what I did: 1. **Created `isbn.py`** with a `valid(code)` function that validates ISBN-10 codes 2. **The function:** - ... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 17297.2 | 0.022005 | unavailable | Excellent! The solution is complete and all tests pass. ## Summary I've written `intervals.py` with a `merge(pairs)` function that: 1. Takes a list of `[star... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 16461.5 | 0.021857 | unavailable | Excellent! The task is complete. I've written `wrap.py` with a `wrap(text, width)` function that: 1. **Breaks text into lines** of at most `width` characters... |
| category-totals | sandbox | agentsdk | yes | completed | 5 | 15463.3 | 0.019260 | unavailable | Excellent! The task is complete. Here's what I did: 1. **Read the requirements**: Examined `sales.csv` (which contains category and amount columns) and `chec... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 9067.3 | 0.010682 | unavailable | Perfect! I've successfully changed the [server] section's port from 8080 to 9090. All other settings remain unchanged: - The [metrics] port stays at 9100 - T... |
| add-license-field | sandbox | agentsdk | yes | completed | 5 | 13754.0 | 0.013866 | unavailable | Perfect! I've successfully added the "license" field with value "MIT" to package.json. The file is valid JSON and all other fields remain exactly as they wer... |
| rename-company | sandbox | agentsdk | yes | completed | 7 | 20184.1 | 0.021457 | unavailable | Done! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs directory and its subfolders: - **docs/int... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 13394.6 | 0.010880 | unavailable | Perfect! I've successfully added a new section for version 1.3.0 to CHANGELOG.md above the 1.2.0 section. The new section follows the same style with: - A le... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 14212.0 | 0.014250 | unavailable | Excellent! The task is complete. I've successfully added an "initials" column to people.csv that contains each person's first and last initials in capitals: ... |
| fetch-page | sandbox | agentsdk | yes | completed | 3 | 11580.9 | 0.008439 | unavailable | Done! I've successfully: 1. **Downloaded** https://example.com/ and saved the page exactly as served to **page.html** 2. Written the HTTP status code **200**... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 5 | 18636.3 | 0.014927 | unavailable | Done! I've successfully: 1. Fetched the page at https://example.com/ 2. Extracted the title "Example Domain" from the `<title>` element 3. Determined the res... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 18993.8 | 0.023260 | unavailable | **Done!** The fault was in `stats.py` line 3. The `average` function was dividing by `(len(values) + 1)` instead of `len(values)`. This caused: - `[2, 4, 6]`... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 7 | 21567.0 | 0.023589 | unavailable | unavailable |
| repair-dedupe | sandbox | agentsdk | yes | completed | 7 | 19000.5 | 0.023821 | unavailable | Excellent! The check passes now. ## Summary I found and fixed the faults in `emails.py`: **Issues:** 1. The `distinct()` function was case-sensitive but shou... |
| repair-restock | sandbox | agentsdk | yes | budget_exceeded | 7 | 21491.4 | 0.027723 | unavailable | unavailable |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 18887.0 | 0.024033 | unavailable | Excellent! The task is complete. ## Summary I found and fixed the fault in `slugs.py`. The original `slugify` function had two issues: 1. **Multiple consecut... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
