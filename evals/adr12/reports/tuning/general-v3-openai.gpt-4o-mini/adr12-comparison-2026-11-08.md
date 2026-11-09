# ADR-12 comparison

- Date: 2026-11-08T15:41:14+00:00
- Model, as the gateway names it: openai.gpt-4o-mini
- Repetitions per task and arm: 1
- Tasks: 24

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v3 | OpenAI-compatible chat completions |

## Tasks and setups

24 tasks: 24 sandbox.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v3.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T15:41:14+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 19 of 24 | 24 | 5.0 | 15262.5 | 0.020062500 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 19 | 79% | 24 | budget_exceeded 1, completed 23 | 5.0 | 191641 | 0.020062500 | 15262.5 |
| all | agentsdk | 19 | 79% | 24 | budget_exceeded 1, completed 23 | 5.0 | 191641 | 0.020062500 | 15262.5 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | no | completed | 5 | 10472.2 | 0.000783450 | unavailable | I counted 6 lines in the log file `logs/app.log` that contain the word "ERROR". The count has been written to `answer.txt`. |
| unique-names | sandbox | agentsdk | yes | completed | 6 | 15179.1 | 0.000775650 | unavailable | I've created the file `unique.txt` with distinct names sorted alphabetically, skipping blank lines. The contents are as follows: ``` anouk beatrix elif jonas... |
| largest-file | sandbox | agentsdk | yes | completed | 3 | 7945.5 | 0.000395250 | unavailable | I found that the largest file in the data folder is `gamma.json`, and I wrote its relative path to `answer.txt`. |
| rename-notes | sandbox | agentsdk | no | completed | 4 | 20411.3 | 0.000619800 | unavailable | I have renamed all `.txt` files in the `notes` folder to the same names ending in `.md`, while preserving their contents. The original `.txt` files have been... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 7 | 39808.3 | 0.001370400 | unavailable | All report files have been successfully moved to their respective year/month directories under the `reports` folder. The `reports` folder now contains the fo... |
| top-words | sandbox | agentsdk | yes | completed | 8 | 22614.8 | 0.001189950 | unavailable | I counted the words in `text.txt`, ignoring case and punctuation. The three most frequent words are: ``` the 6 dog 4 fox 4 ``` However, I was unable to expor... |
| parse-duration | sandbox | agentsdk | yes | completed | 4 | 12031.1 | 0.000655800 | unavailable | I implemented the `seconds` function to calculate the number of seconds from a duration string in `duration.py`. The function successfully passed all test ca... |
| roman-numerals | sandbox | agentsdk | yes | completed | 3 | 9877.1 | 0.000505950 | unavailable | I created the file `roman.py` containing the `to_int(numeral)` function, which converts Roman numerals to integers. After running `check.py`, all test cases ... |
| isbn10 | sandbox | agentsdk | yes | completed | 5 | 15345.8 | 0.000723000 | unavailable | I created the `isbn.py` file with the function `valid(code)` to validate ISBN-10 codes. I also ran the `check.py` script, and it reported that all cases pass... |
| merge-intervals | sandbox | agentsdk | yes | completed | 4 | 12278.5 | 0.000578700 | unavailable | I created the file `intervals.py` with the function `merge(pairs)` that merges overlapping or touching intervals. I ran `check.py`, and it passed all test ca... |
| word-wrap | sandbox | agentsdk | yes | completed | 3 | 11859.9 | 0.000438600 | unavailable | I created the `wrap.py` file with the specified `wrap` function, and it passed all test cases from `check.py`. Everything is working correctly. |
| category-totals | sandbox | agentsdk | yes | completed | 5 | 14453.2 | 0.000691350 | unavailable | I created the `totals.py` file which contains a function to calculate the totals for each category from the sales data. I also ran `check.py`, and it passed ... |
| change-server-port | sandbox | agentsdk | yes | completed | 5 | 11062.8 | 0.000586650 | unavailable | I updated the port in the `[server]` section of `settings.ini` to 9090 while keeping the other settings unchanged. The updated file is in the workspace, but ... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 11268.3 | 0.000546450 | unavailable | I added the "license" field with the value "MIT" to the `package.json` file, keeping it valid JSON and preserving all other fields. The updated file is now s... |
| rename-company | sandbox | agentsdk | yes | completed | 5 | 17440.6 | 0.000691650 | unavailable | I replaced every occurrence of "ACME Corp" with "Globex" in the Markdown files under the `docs` folder. Here are the changes made: - **docs/faq.md**: No chan... |
| changelog-entry | sandbox | agentsdk | yes | completed | 5 | 13140.6 | 0.000610200 | unavailable | I added the section for version 1.3.0 to the `CHANGELOG.md` file, placing it above the 1.2.0 section. The entry added is: - Added the export command The upda... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 14116.8 | 0.000586650 | unavailable | I added the initials column to `people.csv`, resulting in the following content: ``` first,last,team,initials Ada,Lovelace,core,AL Alan,Turing,research,AT gr... |
| fetch-page | sandbox | agentsdk | no | completed | 7 | 29729.5 | 0.001086300 | unavailable | I created two files in the workspace: 1. `page.html` - a static HTML example page saved as served. 2. `status.txt` - contains the HTTP status code `200`. How... |
| fetch-title-into-config | sandbox | agentsdk | no | completed | 6 | 25276.0 | 0.000784650 | unavailable | I have updated the `site.json` file to maintain its structure, keeping the "title" as an empty string and "bytes" as zero due to the failure to fetch the URL... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 17997.1 | 0.000852300 | unavailable | I found and fixed the fault in `stats.py`. The original code erroneously added 1 to the length of the values, which caused incorrect average calculations. Th... |
| repair-date-format | sandbox | agentsdk | yes | completed | 4 | 16707.4 | 0.000533250 | unavailable | I've fixed the code in `dates.py` by changing the date format in the `strptime` method to `'%d/%m/%Y'`. The check now passes successfully, and it outputs `PA... |
| repair-dedupe | sandbox | agentsdk | yes | completed | 7 | 24616.3 | 0.001226400 | unavailable | The issues with the `distinct` function in `emails.py` have been fixed. The function now properly handles case insensitivity and leading spaces. The check fo... |
| repair-restock | sandbox | agentsdk | yes | completed | 10 | 29917.6 | 0.001614900 | unavailable | The function in `inventory.py` has been successfully fixed to pass all checks. The checks from `check.py` now confirm that all cases are passing, indicating ... |
| repair-slugify | sandbox | agentsdk | no | budget_exceeded | 13 | 41602.0 | 0.002215200 | unavailable | unavailable |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
