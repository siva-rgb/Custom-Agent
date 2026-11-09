# ADR-12 comparison

- Date: 2026-11-08T15:59:26+00:00
- Model, as the gateway names it: openai.gpt-4o-mini
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
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T15:59:26+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 18 of 24 | 24 | 5.0 | 14478.5 | 0.021779400 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 18 | 75% | 24 | completed 24 | 5.0 | 196002 | 0.021779400 | 14478.5 |
| all | agentsdk | 18 | 75% | 24 | completed 24 | 5.0 | 196002 | 0.021779400 | 14478.5 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | no | completed | 5 | 12635.9 | 0.000815850 | unavailable | I counted the lines in `logs/app.log` containing the word "ERROR" and found 5 occurrences. This count has been written to `answer.txt`. |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 9470.0 | 0.000583350 | unavailable | I created a file named `unique.txt` that contains every distinct name from `names.txt`, sorted alphabetically and with blank lines skipped. The contents of `... |
| largest-file | sandbox | agentsdk | yes | completed | 3 | 7726.0 | 0.000347250 | unavailable | I found the largest file in the data folder, which is `data/gamma.json`. Its path has been written to `answer.txt`. |
| rename-notes | sandbox | agentsdk | yes | completed | 5 | 34236.4 | 0.000953400 | unavailable | I have renamed all the `.txt` files in the `notes` folder to `.md` files, keeping their contents intact. The original `.txt` files have been removed. Here's ... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 6 | 31960.7 | 0.001341750 | unavailable | The files have been successfully moved into their respective year and month folders under `reports`. The directory structure is as follows: - `reports/2024-0... |
| top-words | sandbox | agentsdk | no | completed | 6 | 14541.7 | 0.000905550 | unavailable | I counted the words in `text.txt` and found the three most frequent words along with their counts. They are: ``` the 6 dog 4 fox 4 ``` I wrote this output to... |
| parse-duration | sandbox | agentsdk | yes | completed | 5 | 13501.7 | 0.000814350 | unavailable | I implemented the `seconds` function in `duration.py` to correctly parse durations like '1h30m', '45s', etc., and it worked as expected. After running `check... |
| roman-numerals | sandbox | agentsdk | yes | completed | 4 | 25562.6 | 0.000704250 | unavailable | I created the `roman.py` file with the `to_int(numeral)` function to convert Roman numerals to integers. I then ran `check.py`, which confirmed that all test... |
| isbn10 | sandbox | agentsdk | yes | completed | 4 | 11658.1 | 0.000685200 | unavailable | I created the `isbn.py` file containing the `valid` function for checking ISBN-10 validity. The `check.py` script ran successfully, confirming that all test ... |
| merge-intervals | sandbox | agentsdk | yes | completed | 5 | 14415.3 | 0.000817800 | unavailable | I created the `intervals.py` file with the `merge` function, and it has been successfully verified against all test cases in `check.py`. The results of the t... |
| word-wrap | sandbox | agentsdk | yes | completed | 4 | 12919.1 | 0.000705450 | unavailable | I created the `wrap.py` file with the specified `wrap(text, width)` function, and then ran `check.py`, which successfully passed all cases. However, I'm unab... |
| category-totals | sandbox | agentsdk | yes | completed | 5 | 13190.1 | 0.000805650 | unavailable | I have created the `totals.py` file, which contains a function that reads the `sales.csv` file and calculates the total sales for each category. The `check.p... |
| change-server-port | sandbox | agentsdk | yes | completed | 5 | 10681.4 | 0.000700050 | unavailable | I updated the `settings.ini` file, changing the server port to 9090 while keeping all other settings the same. Currently, I can't export the modified file as... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 10178.8 | 0.000578850 | unavailable | I added the "license" field with the value "MIT" to the `package.json`, maintaining valid JSON and keeping all other fields unchanged. The updated file is no... |
| rename-company | sandbox | agentsdk | yes | completed | 5 | 14644.5 | 0.000801450 | unavailable | I replaced every occurrence of 'ACME Corp' with 'Globex' in the Markdown files under the `docs` directory. The updated files are: - `docs/faq.md`: No changes... |
| changelog-entry | sandbox | agentsdk | yes | completed | 3 | 7228.9 | 0.000478500 | unavailable | I added the version 1.3.0 section to the CHANGELOG.md file including the entry "- Added the export command" right above the 1.2.0 section. The earlier sectio... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 10558.2 | 0.000725250 | unavailable | I updated the `people.csv` file to include a new column named "initials," where each person's initials are represented in capitals. The content of the file i... |
| fetch-page | sandbox | agentsdk | no | completed | 3 | 17458.8 | 0.000478800 | unavailable | The attempts to download the page from `https://example.com/` using `curl` and `wget` both failed. The `curl` command was unavailable in the workspace enviro... |
| fetch-title-into-config | sandbox | agentsdk | no | completed | 7 | 27857.9 | 0.001099950 | unavailable | I fetched the page from the URL in `site.json` and updated the file as follows: - **Title** field set to "Example Domain". - **Bytes** field set to the size ... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 16000.5 | 0.000992250 | unavailable | I found and fixed a fault in the `average` function in `stats.py`. The original implementation incorrectly added 1 to the length of the values, leading to in... |
| repair-date-format | sandbox | agentsdk | yes | completed | 8 | 23301.6 | 0.001363050 | unavailable | I fixed the issue in `dates.py` by modifying the date format in the `strptime` function to correctly interpret the day-first format. Now, the check script ru... |
| repair-dedupe | sandbox | agentsdk | no | completed | 10 | 35658.9 | 0.002246400 | unavailable | The changes made to the `emails.py` function were successful, and all test cases in `check.py` have passed. The distinct function now correctly handles case ... |
| repair-restock | sandbox | agentsdk | yes | completed | 11 | 27823.5 | 0.001754700 | unavailable | The error in the `inventory.py` check function has been fixed. The bug was in the condition that determined which items to order. It was originally checking ... |
| repair-slugify | sandbox | agentsdk | no | completed | 5 | 20843.2 | 0.001080300 | unavailable | The `slugify` function has been successfully fixed. The changes addressed the previously failing test cases, and now all tests have passed, as confirmed by t... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
