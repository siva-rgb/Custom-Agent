# ADR-12 comparison

- Date: 2026-11-08T15:22:33+00:00
- Model, as the gateway names it: openai.gpt-4o-mini
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
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T15:22:33+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 20 of 24 | 24 | 5.0 | 16717.8 | 0.020678400 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 20 | 83% | 24 | budget_exceeded 1, completed 23 | 5.0 | 195276 | 0.020678400 | 16717.8 |
| all | agentsdk | 20 | 83% | 24 | budget_exceeded 1, completed 23 | 5.0 | 195276 | 0.020678400 | 16717.8 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | yes | completed | 3 | 7282.5 | 0.000586800 | unavailable | I counted the lines in `logs/app.log` that contain the word "ERROR", and there are 7 of them. The count has been written to `answer.txt`. |
| unique-names | sandbox | agentsdk | yes | completed | 3 | 9776.5 | 0.000431850 | unavailable | I created a file named `unique.txt` containing every distinct name from `names.txt`, sorted alphabetically and with blank lines skipped. The contents are: ``... |
| largest-file | sandbox | agentsdk | yes | completed | 3 | 7857.4 | 0.000344400 | unavailable | I found the largest file in the data folder, which is `gamma.json` with a size of 3013 bytes. The path has been written to `answer.txt`. |
| rename-notes | sandbox | agentsdk | yes | completed | 6 | 29845.4 | 0.001031850 | unavailable | I have renamed all `.txt` files in the `notes` folder to `.md` files, retaining their content. The `.txt` files have been removed. The final files are: - `no... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 7 | 43335.2 | 0.001577400 | unavailable | I have organized the files in the `reports` folder into year-and-month subdirectories under `reports/`. The resulting structure is: ``` reports/ ├── 2024-01/... |
| top-words | sandbox | agentsdk | yes | completed | 10 | 23642.0 | 0.001440150 | unavailable | The three most frequent words found in `text.txt`, along with their counts, were written to `top.txt` as requested: ``` the 6 dog 4 fox 4 ``` All counts are ... |
| parse-duration | sandbox | agentsdk | yes | completed | 5 | 13653.1 | 0.000780750 | unavailable | I implemented the `seconds(text)` function in `duration.py` to parse duration strings like '1h30m' and return the corresponding number of seconds. After runn... |
| roman-numerals | sandbox | agentsdk | yes | completed | 4 | 11478.7 | 0.000674700 | unavailable | I created the `roman.py` file with the `to_int(numeral)` function that converts Roman numerals to integers. I ran `check.py`, and all tests passed successful... |
| isbn10 | sandbox | agentsdk | yes | completed | 4 | 14538.5 | 0.000683400 | unavailable | I created the `isbn.py` file with the function `valid(code)` that checks the validity of an ISBN-10 code and ignores hyphens. I ran `check.py`, which confirm... |
| merge-intervals | sandbox | agentsdk | yes | completed | 5 | 14822.5 | 0.000795600 | unavailable | I created the `intervals.py` file with the `merge` function that merges overlapping or touching intervals. I then ran `check.py`, and all cases passed succes... |
| word-wrap | sandbox | agentsdk | yes | completed | 5 | 21705.4 | 0.001217550 | unavailable | I created the `wrap.py` file with the specified `wrap` function, which handles text wrapping according to the requirements. After running `check.py`, it pass... |
| category-totals | sandbox | agentsdk | yes | completed | 5 | 21994.8 | 0.000764100 | unavailable | I created `totals.py` with the requested function to calculate category totals from the `sales.csv` file. I ran `check.py`, and it passed all test cases succ... |
| change-server-port | sandbox | agentsdk | yes | completed | 5 | 13515.4 | 0.000783300 | unavailable | The port in the `[server]` section of the `settings.ini` file has been changed to 9090 while keeping all other settings intact. The updated file is saved in ... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 11765.1 | 0.000562650 | unavailable | I added the "license" field with the value "MIT" to `package.json`. The file is now updated and valid JSON, with all other fields unchanged. |
| rename-company | sandbox | agentsdk | yes | completed | 5 | 18860.5 | 0.000779100 | unavailable | All occurrences of 'ACME Corp' have been replaced with 'Globex' in the relevant Markdown files under the `docs` directory. The modified files are: - `docs/fa... |
| changelog-entry | sandbox | agentsdk | yes | completed | 3 | 9313.6 | 0.000453450 | unavailable | I added a section for version 1.3.0 above the 1.2.0 section in the CHANGELOG.md file, with the entry '- Added the export command'. The file has been updated ... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 11924.1 | 0.000627900 | unavailable | I added a new column named "initials" to the `people.csv` file, which contains the initials of each person in capital letters. The updated file now looks lik... |
| fetch-page | sandbox | agentsdk | no | completed | 3 | 19258.1 | 0.000385350 | unavailable | It seems that both `curl` and `wget` are not functioning properly for downloading the specified URL. The address "example.com" appears to be unreachable from... |
| fetch-title-into-config | sandbox | agentsdk | no | completed | 5 | 26392.1 | 0.000655200 | unavailable | The attempts to fetch the URL specified in `site.json` have all failed due to network restrictions; the URL points to a non-existent domain in this sandboxed... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 21096.1 | 0.000913050 | unavailable | The error in the code was due to an incorrect denominator in the `average` function of `stats.py`. The function was initially calculating the average as `sum... |
| repair-date-format | sandbox | agentsdk | yes | completed | 7 | 16379.3 | 0.000878550 | unavailable | I fixed the issue in `dates.py` where the date format was incorrectly set to `'%m/%d/%Y'`. I changed it to `'%d/%m/%Y'`. After the modification, the check pa... |
| repair-dedupe | sandbox | agentsdk | no | completed | 9 | 25194.5 | 0.001477350 | unavailable | The `distinct` function in `emails.py` has been successfully updated to handle case sensitivity and leading spaces in email addresses. As a result, `check.py... |
| repair-restock | sandbox | agentsdk | no | budget_exceeded | 12 | 37179.9 | 0.001950000 | unavailable | unavailable |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 17056.4 | 0.000883950 | unavailable | I fixed the error in `slugs.py` by updating the `slugify` function to properly handle consecutive non-alphanumeric characters and trim the dashes from the st... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
