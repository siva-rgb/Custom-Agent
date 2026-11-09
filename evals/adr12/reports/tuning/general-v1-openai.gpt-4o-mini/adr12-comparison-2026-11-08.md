# ADR-12 comparison

- Date: 2026-11-08T15:03:10+00:00
- Model, as the gateway names it: openai.gpt-4o-mini
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
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T15:03:10+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 18 of 24 | 24 | 5.0 | 16059.4 | 0.022208250 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 18 | 75% | 24 | completed 24 | 5.0 | 175924 | 0.022208250 | 16059.4 |
| all | agentsdk | 18 | 75% | 24 | completed 24 | 5.0 | 175924 | 0.022208250 | 16059.4 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | no | completed | 5 | 11404.7 | 0.000858600 | unavailable | I found that there are 5 lines in `logs/app.log` that contain the word "ERROR." This count has been written to `answer.txt`. |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 11442.4 | 0.000569550 | unavailable | I created a file named `unique.txt` that contains the distinct names sorted alphabetically, one per line, and excluded blank lines. Here are the names includ... |
| largest-file | sandbox | agentsdk | yes | completed | 3 | 7335.7 | 0.000431700 | unavailable | I found the largest file in the data folder, which is `gamma.json` with a size of 3013 bytes. Its path relative to the folder has been written to `answer.txt`. |
| rename-notes | sandbox | agentsdk | yes | completed | 6 | 35185.0 | 0.001087050 | unavailable | All `.txt` files in the `notes` folder have been successfully renamed to `.md` files with their contents preserved. The following files were created: - `note... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 8 | 40566.4 | 0.001648950 | unavailable | The reports have been successfully organized into monthly folders. The final structure in the `reports` directory is as follows: - `reports/2024-01/` - `repo... |
| top-words | sandbox | agentsdk | no | completed | 10 | 25460.1 | 0.001523550 | unavailable | I counted the words in `text.txt`, ignoring case and punctuation. The three most frequent words are: 1. `the 6` 2. `dog 4` 3. `fox 4` The file `top.txt` cont... |
| parse-duration | sandbox | agentsdk | yes | completed | 6 | 16304.8 | 0.000793950 | unavailable | I implemented the `seconds` function in `duration.py`, which now correctly calculates the number of seconds from a duration string. I ran `check.py`, which r... |
| roman-numerals | sandbox | agentsdk | yes | completed | 5 | 19875.6 | 0.000730200 | unavailable | I created the `roman.py` file with the `to_int` function for converting Roman numerals to integers. I also ran `check.py`, which confirmed that all test case... |
| isbn10 | sandbox | agentsdk | yes | completed | 5 | 12902.6 | 0.000726750 | unavailable | I created the `isbn.py` file with a `valid(code)` function that checks for valid ISBN-10 codes. I ran `check.py`, which passed all test cases successfully. H... |
| merge-intervals | sandbox | agentsdk | no | completed | 6 | 24576.9 | 0.001349100 | unavailable | The `merge` function has been implemented in `intervals.py`, and I've also created a `check.py` that tests it. All test cases passed successfully when runnin... |
| word-wrap | sandbox | agentsdk | yes | completed | 3 | 11534.7 | 0.000604950 | unavailable | The `wrap.py` file has been created with the required functionality for the `wrap` function. I ran `check.py`, and all tests passed successfully. There are n... |
| category-totals | sandbox | agentsdk | yes | completed | 5 | 14671.0 | 0.000792300 | unavailable | I created the `totals.py` file with the function to calculate total amounts per category from `sales.csv`. The function rounds the results to two decimal pla... |
| change-server-port | sandbox | agentsdk | yes | completed | 5 | 12420.7 | 0.000778200 | unavailable | The `settings.ini` file has been updated to change the server port to 9090, while keeping all other settings intact. The file now contains: ``` [server] host... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 11313.2 | 0.000642450 | unavailable | I added a "license" field with the value "MIT" to `package.json`, keeping it valid JSON and preserving all other fields. The updated file is now in place. |
| rename-company | sandbox | agentsdk | yes | completed | 5 | 15814.0 | 0.000862200 | unavailable | I have replaced every occurrence of 'ACME Corp' with 'Globex' in the Markdown files under the `docs` directory, including its subfolders. The changes were ma... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 10390.9 | 0.000602400 | unavailable | I added a section for version 1.3.0 to the CHANGELOG.md file, with the entry '- Added the export command' right above the 1.2.0 section. The earlier sections... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 11373.8 | 0.000765600 | unavailable | I successfully added a column named "initials" to `people.csv`, which contains each person's initials in capitals. The updated contents are: ``` first,last,t... |
| fetch-page | sandbox | agentsdk | no | completed | 1 | 2687.8 | 0.00017865 | unavailable | The environment doesn't have access to the internet, so I can't download pages from external websites like https://example.com/. However, if you have specifi... |
| fetch-title-into-config | sandbox | agentsdk | no | completed | 6 | 22121.2 | 0.000817650 | unavailable | I encountered an issue while trying to fetch the URL specified in `site.json`. The commands for both `curl` and `wget` failed; `curl` is not available, and `... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 17107.3 | 0.001002300 | unavailable | I found and fixed the fault in the code in `stats.py`, which affected the calculation of the average. The original code incorrectly added 1 to the length of ... |
| repair-date-format | sandbox | agentsdk | yes | completed | 7 | 21446.6 | 0.000999450 | unavailable | I fixed the issue in `dates.py` by changing the date format from `'%m/%d/%Y'` to `'%d/%m/%Y'`, which aligns with the test cases in `check.py`. The check now ... |
| repair-dedupe | sandbox | agentsdk | no | completed | 9 | 25914.3 | 0.001374000 | unavailable | The issue in `emails.py` was identified and fixed. The distinct function was modified to strip whitespace and convert email addresses to lowercase. Now, the ... |
| repair-restock | sandbox | agentsdk | yes | completed | 8 | 30335.5 | 0.001738950 | unavailable | The function in `inventory.py` has been successfully fixed. All test cases in `check.py` have passed. The adjustment to the logic was made to include items t... |
| repair-slugify | sandbox | agentsdk | yes | completed | 8 | 20432.5 | 0.001329750 | unavailable | I modified the `slugify` function in `slugs.py` to handle consecutive non-alphanumeric characters correctly by using a regex that replaces sequences of such ... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
