# ADR-12 comparison

- Date: 2026-11-10T04:15:31+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 3
- Tasks: 42

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v5 | OpenAI-compatible chat completions |
| claude-agent-sdk | claude-agent-sdk 0.2.152 with Claude Code CLI 2.1.267, in adr12-claude:cli-2.1.267 | Anthropic Messages through the Claude Code CLI |

## Tasks and setups

42 tasks: 1 decline, 6 delegation, 3 long, 4 multi-step, 24 sandbox, 4 single-step.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| decline | 1 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.015310 | 9990 |
| decline | 1 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |
| delegation | 6 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash; the delegating preset (delegate=True), each child a general subagent in a workspace of its own with the files its call named | 0.203690 | 169050 |
| delegation | 6 | claude-agent-sdk | Read, Glob, Grep, Bash, Write and Edit in a disposable container holding the task's files; it runs as uid 1000 (agent, home /home/agent), and its image adds nodejs, npm and bash (for the Bash tool) to the workspace image's base; and its CLI's own subagent tool, Agent | none | none |
| long | 3 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.022370 | 19420 |
| long | 3 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |
| multi-step | 4 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.023940 | 20175 |
| multi-step | 4 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |
| sandbox | 24 | claude-agent-sdk | Read, Glob, Grep, Bash, Write and Edit in a disposable container holding the task's files; it runs as uid 1000 (agent, home /home/agent), and its image adds nodejs, npm and bash (for the Bash tool) to the workspace image's base | none | none |
| single-step | 4 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.021750 | 19250 |
| single-step | 4 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median; the delegation kind 10 times the sandbox kind's median on this SDK's arm in M51 (adr12-comparison-2026-11-09.json), a child's share reserved from its parent's. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v5.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-10T04:15:31+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host, kept in each row's fetch_evidence: on this SDK's arm an allowed connection its workspace's proxy recorded, by id; on the Claude arm, whose network is not recorded, a call whose result was not an error, read as the shell would run it: WebFetch of the host's URL, or a Bash command whose program, after any wrapper, is curl or wget with the host among its arguments, or python or node running code that uses an HTTP client and names the host's URL (inline, a heredoc, or a script the run wrote). The Claude arm's evidence is the command the run gave, not the network: a URL computed at run time or a script edited after it was written is a fetch it does not count (pre-review F1, round-1 F1).
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
- claude-agent-sdk image: adr12-claude:cli-2.1.267 sha256:9cf6ef1c38050aed0cca1abeefa337a986dad460da25e410553a20cdd206a4db

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 122 of 126 | 126 | 5.0 | 17393.8 | 2.396066 | unavailable |
| claude-agent-sdk | 121 of 126 | 126 | 6.5 | 16279.5 | 1.63689940 | 1.6368994000000000204 |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| decline | agentsdk | 3 | 100% | 3 | completed 3 | 1 | 2966 | 0.004438 | 2806.8 |
| decline | claude-agent-sdk | 3 | 100% | 3 | completed 3 | 1 | 9336 | 0.014496 | 3767.9 |
| delegation | agentsdk | 17 | 94% | 18 | completed 18 | 9.0 | 785300 | 0.913448 | 32419.4 |
| delegation | claude-agent-sdk | 16 | 89% | 18 | completed 18 | 11.0 | 1124367 | 0.33355925 | 19957.3 |
| long | agentsdk | 9 | 100% | 9 | completed 9 | 2 | 21585 | 0.025729 | 4613.0 |
| long | claude-agent-sdk | 9 | 100% | 9 | completed 9 | 3 | 75192 | 0.083996 | 5073.5 |
| multi-step | agentsdk | 12 | 100% | 12 | completed 12 | 2.0 | 25512 | 0.030760 | 2997.3 |
| multi-step | claude-agent-sdk | 12 | 100% | 12 | completed 12 | 2.0 | 103937 | 0.117069 | 5570.7 |
| sandbox | agentsdk | 69 | 96% | 72 | budget_exceeded 6, completed 66 | 6.0 | 1172427 | 1.395579 | 18837.9 |
| sandbox | claude-agent-sdk | 69 | 96% | 72 | completed 71, max_turns_exceeded 1 | 8.0 | 3807955 | 0.95286215 | 19443.2 |
| single-step | agentsdk | 12 | 100% | 12 | completed 12 | 2.0 | 23076 | 0.026112 | 2735.7 |
| single-step | claude-agent-sdk | 12 | 100% | 12 | completed 12 | 3.0 | 117841 | 0.134917 | 6886.5 |
| all | agentsdk | 122 | 97% | 126 | budget_exceeded 6, completed 120 | 5.0 | 2030866 | 2.396066 | 17393.8 |
| all | claude-agent-sdk | 121 | 96% | 126 | completed 125, max_turns_exceeded 1 | 6.5 | 5238628 | 1.63689940 | 16279.5 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 2564.2 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 6495.4 | 0.009870 | 0.00987 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 1958.8 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 3 | 6712.2 | 0.011759 | 0.011759000000000002 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 2188.7 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 4 | 9659.6 | 0.012949 | 0.012949 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 2461.5 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 11110.9 | 0.014303 | 0.014303 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 7376.8 | 0.002439 | unavailable | The file that defines the function `apply_fee` is **`src/ledger.py`** (located at line 6). |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 3153.3 | 0.006371 | 0.006371 | The file that defines the function `apply_fee` is: **`src/ledger.py`** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 2916.7 | 0.002409 | unavailable | The test file that exercises `apply_fee` is **tests/test_ledger.py**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 2 | 8347.4 | 0.006667 | 0.006667 | The test file that exercises `apply_fee` is: **tests\test_ledger.py** |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 2 | 3911.9 | 0.002670 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 6 | 19836.0 | 0.014037 | 0.014036999999999999 | The version named in README.md is **2.4.1**, and its entry in docs/changelog.md carries the date: **2026-04-02** |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 3011.2 | 0.002388 | unavailable | The setting that holds the value `eu-west-1` is **`region`**, found in the file `config/settings.toml`. |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 2 | 3253.9 | 0.006546 | 0.006546 | The value `eu-west-1` is held in a setting called **`region`** in the file `config/settings.toml`. |
| every-python-file | long | agentsdk | yes | completed | 2 | 2722.5 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 2909.3 | 0.006245 | 0.006245 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 2848.9 | 0.002288 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 3 | 5073.5 | 0.010168 | 0.010168 | 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 4182.2 | 0.004079 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 6 | 6515.4 | 0.014717 | 0.014717 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2943.6 | 0.001676 | unavailable | I don't have the ability to delete files. The tools available to me only allow me to read files and search for text within them. I can: 1. Read files 2. Sear... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 4723.8 | 0.005472 | 0.0054719999999999994 | I don't have a tool available to delete files. My available tools are: - **Read**: to view file contents - **Grep**: to search within files - **Glob**: to fi... |
| count-error-lines | sandbox | agentsdk | yes | completed | 6 | 16901.5 | 0.018752 | unavailable | Done! I've counted the lines containing "ERROR" in logs/app.log and found **7 lines**. The count has been written to answer.txt with only the digits as reque... |
| count-error-lines | sandbox | claude-agent-sdk | yes | completed | 3 | 11002.2 | 0.01203215 | 0.01203215 | Done! The count of lines containing "ERROR" in logs/app.log is 7, and I've written this to answer.txt. |
| unique-names | sandbox | agentsdk | yes | completed | 5 | 13399.9 | 0.015168 | unavailable | Perfect! I've successfully created **unique.txt** with the following results: - **anouk** - **beatrix** - **elif** - **jonas** - **mara** - **zoe** The progr... |
| unique-names | sandbox | claude-agent-sdk | yes | completed | 5 | 19159.7 | 0.00717575 | 0.007175750000000001 | Done! I've created `unique.txt` with all distinct names from `names.txt`, sorted alphabetically. The file contains: - anouk - beatrix - elif - jonas - mara -... |
| largest-file | sandbox | agentsdk | yes | completed | 4 | 8628.0 | 0.010508 | unavailable | Done! I found the largest file in the data folder: **Result:** The largest file is `data/gamma.json` with 3013 bytes, and its path has been written to `answe... |
| largest-file | sandbox | claude-agent-sdk | yes | completed | 5 | 11152.6 | 0.00843575 | 0.008435749999999999 | Done! I found that the largest file in the data folder is **data/gamma.json** with a size of 3,013 bytes, and I've written this path to answer.txt. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 22280.1 | 0.023653 | unavailable | Perfect! All done. I've successfully renamed every .txt file in the notes folder to .md files: - budget.txt → budget.md - design.txt → design.md - kickoff.tx... |
| rename-notes | sandbox | claude-agent-sdk | yes | completed | 16 | 17400.1 | 0.01431125 | 0.014311250000000001 | Perfect! ✅ All done! I've successfully renamed all 4 .txt files in the notes folder to .md files: - `retro.txt` → `retro.md` - `kickoff.txt` → `kickoff.md` -... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 8 | 31087.8 | 0.027172 | unavailable | Perfect! The task is complete. All report files have been successfully organized: **Summary:** - **reports/2024-01/**: Contains report-2024-01-15.csv and rep... |
| file-reports-by-month | sandbox | claude-agent-sdk | yes | completed | 6 | 16922.0 | 0.01104745 | 0.01104745 | Perfect! ✅ All done. I've successfully organized all the report files: **Summary of changes:** - Created three month folders: `2024-01/`, `2024-02/`, and `20... |
| top-words | sandbox | agentsdk | yes | completed | 5 | 24722.2 | 0.016697 | unavailable | Excellent! The task is complete. Here's what I did: 1. **Read text.txt**: The file contains text about foxes and dogs. 2. **Extracted words**: Used regex `[a... |
| top-words | sandbox | claude-agent-sdk | yes | completed | 9 | 25288.4 | 0.01321590 | 0.0132159 | Perfect! I've completed the task: **Summary:** - Analyzed text.txt, extracting all words (runs of letters only), converting to lowercase - Counted word frequ... |
| parse-duration | sandbox | agentsdk | yes | completed | 5 | 22888.9 | 0.018862 | unavailable | Excellent! The implementation is complete and all test cases pass. ## Summary I've implemented the `seconds(text)` function in `duration.py` that: 1. **Parse... |
| parse-duration | sandbox | claude-agent-sdk | yes | completed | 9 | 23954.0 | 0.01549395 | 0.015493950000000001 | Excellent! ✅ The implementation is complete and all test cases pass. Here's what I did: 1. **Implemented the `seconds(text)` function** to parse duration str... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 19570.4 | 0.022095 | unavailable | Excellent! The task is complete. I have successfully: 1. **Written `roman.py`** with the `to_int(numeral)` function that converts Roman numerals to integers ... |
| roman-numerals | sandbox | claude-agent-sdk | yes | completed | 9 | 19620.1 | 0.01704340 | 0.0170434 | Perfect! ✅ I've successfully completed the task: 1. **Created `roman.py`** with a `to_int(numeral)` function that converts Roman numerals to integers 2. **Th... |
| isbn10 | sandbox | agentsdk | yes | completed | 6 | 17949.5 | 0.022630 | unavailable | Excellent! The task is complete. I've created `isbn.py` with a `valid(code)` function that: 1. **Removes hyphens** from the input code 2. **Validates the for... |
| isbn10 | sandbox | claude-agent-sdk | yes | completed | 9 | 24519.2 | 0.02065365 | 0.02065365 | Excellent! ✓ I've successfully created `isbn.py` with a `valid(code)` function that: 1. **Removes hyphens** from the input code 2. **Validates the format**: ... |
| merge-intervals | sandbox | agentsdk | yes | completed | 7 | 20566.3 | 0.027137 | unavailable | Perfect! **Result:** I've successfully created `intervals.py` with a `merge(pairs)` function that: 1. Takes a list of `[start, end]` pairs 2. Merges overlapp... |
| merge-intervals | sandbox | claude-agent-sdk | yes | completed | 9 | 26780.7 | 0.01932930 | 0.0193293 | Perfect! ✅ All done! I've successfully created `intervals.py` with a `merge(pairs)` function that: - Takes a list of `[start, end]` interval pairs - Merges o... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 20491.6 | 0.022619 | unavailable | Excellent! The task is complete. Here's what I did: **Result:** - Created `wrap.py` with a `wrap(text, width)` function that: - Splits text into words - Gree... |
| word-wrap | sandbox | claude-agent-sdk | yes | completed | 9 | 26192.1 | 0.02050225 | 0.020502249999999996 | Perfect! ✅ I've successfully created `wrap.py` with a `wrap(text, width)` function that: 1. **Breaks text into lines** at spaces, keeping lines to at most `w... |
| category-totals | sandbox | agentsdk | yes | completed | 7 | 23694.7 | 0.025919 | unavailable | Perfect! The solution is complete and working correctly. ## Summary I've successfully created `totals.py` with a `totals(path)` function that: 1. **Reads the... |
| category-totals | sandbox | claude-agent-sdk | yes | completed | 11 | 22098.6 | 0.01744675 | 0.01744675 | Excellent! ✅ All done. Here's what I did: 1. **Analyzed the data format**: The `sales.csv` file has a header row with "category,amount" and data rows with sa... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 13759.2 | 0.011064 | unavailable | Done! I've successfully changed the [server] section's port from 8080 to 9090. All other settings remain unchanged: - [metrics] port is still 9100 - [metrics... |
| change-server-port | sandbox | claude-agent-sdk | yes | completed | 4 | 8127.9 | 0.00614200 | 0.006142 | Done! I've successfully changed the [server] port from 8080 to 9090. All other settings remain unchanged, including: - The [metrics] port remains 9100 - The ... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 11671.4 | 0.011515 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file is valid JSON and all other fields remain exactly as they... |
| add-license-field | sandbox | claude-agent-sdk | yes | completed | 4 | 10053.9 | 0.00674845 | 0.0067484500000000005 | Done! I've added the "license" field with the value "MIT" to package.json. The file remains valid JSON with all other fields preserved exactly as they were: ... |
| rename-company | sandbox | agentsdk | yes | completed | 6 | 20021.0 | 0.019912 | unavailable | Perfect! I've successfully completed the task. Here's what I did: **Summary:** - Found 3 Markdown files in the docs directory (including subfolders): `docs/i... |
| rename-company | sandbox | claude-agent-sdk | yes | completed | 9 | 15215.8 | 0.01130585 | 0.01130585 | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the `docs` directory: **Files updated:** 1. `/wo... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 11565.9 | 0.011071 | unavailable | Perfect! I've successfully added a version 1.3.0 section to CHANGELOG.md above the 1.2.0 section with the entry '- Added the export command'. The new section... |
| changelog-entry | sandbox | claude-agent-sdk | yes | completed | 4 | 15019.7 | 0.00616195 | 0.00616195 | Perfect! I've added the 1.3.0 section to CHANGELOG.md above the 1.2.0 section with the entry "- Added the export command", maintaining the same formatting st... |
| initials-column | sandbox | agentsdk | yes | completed | 4 | 12144.6 | 0.012330 | unavailable | Perfect! I've successfully added the `initials` column to people.csv. The new column contains each person's first and last initials in capitals: - Ada Lovela... |
| initials-column | sandbox | claude-agent-sdk | yes | completed | 5 | 14305.4 | 0.00782145 | 0.00782145 | Perfect! I've successfully added the `initials` column to people.csv. The new column contains: - **AL** for Ada Lovelace - **AT** for Alan Turing - **GH** fo... |
| fetch-page | sandbox | agentsdk | yes | completed | 3 | 16575.6 | 0.010210 | unavailable | Done! I've successfully: 1. **Downloaded** https://example.com/ and saved the page exactly as served to **page.html** (576 bytes) 2. **Saved** the HTTP statu... |
| fetch-page | sandbox | claude-agent-sdk | yes | completed | 7 | 17278.0 | 0.01404345 | 0.014043449999999999 | Perfect! I have successfully completed your request: ✅ **page.html** - Contains the exact HTML page as served by https://example.com/ ✅ **status.txt** - Cont... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 4 | 16010.9 | 0.013869 | unavailable | Excellent! I've successfully completed the task: **Result:** - Fetched the URL from site.json: `https://example.com/` - Extracted the title from the page's `... |
| fetch-title-into-config | sandbox | claude-agent-sdk | yes | completed | 8 | 17795.3 | 0.01259625 | 0.01259625 | Done! I've successfully: 1. ✅ Read site.json and found the URL: `https://example.com/` 2. ✅ Fetched the page and extracted the `<title>` element: "Example Do... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 19057.2 | 0.023903 | unavailable | Perfect! The fault has been fixed. ## Summary **The fault:** In `stats.py`, the `average` function was incorrectly dividing by `len(values) + 1` instead of `... |
| repair-average | sandbox | claude-agent-sdk | yes | completed | 10 | 24928.2 | 0.01636810 | 0.0163681 | ✅ **Fixed!** The fault was in **stats.py** line 4. The `average` function was dividing by `len(values) + 1` instead of just `len(values)`. This caused all th... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 7 | 17934.4 | 0.023764 | unavailable | unavailable |
| repair-date-format | sandbox | claude-agent-sdk | yes | completed | 10 | 22705.7 | 0.01658445 | 0.01658445 | ✅ **Fixed!** The issue was in `/work/dates.py`: **The Problem:** The `iso()` function was using the date format `'%m/%d/%Y'` (month/day/year) when it should ... |
| repair-dedupe | sandbox | agentsdk | yes | budget_exceeded | 7 | 20497.1 | 0.025693 | unavailable | unavailable |
| repair-dedupe | sandbox | claude-agent-sdk | no | completed | 9 | 21712.0 | 0.01432800 | 0.014328 | Excellent! ✅ The check now passes. Here's what was wrong and what I fixed: ## The Problem The `distinct()` function was not handling emails properly: 1. **Ca... |
| repair-restock | sandbox | agentsdk | yes | budget_exceeded | 7 | 23435.7 | 0.027950 | unavailable | unavailable |
| repair-restock | sandbox | claude-agent-sdk | yes | completed | 8 | 31024.1 | 0.01428445 | 0.014284450000000002 | ✅ **Fixed!** The issue was in `/work/inventory.py`. The condition in the function was incorrect: **The fault:** `if count > minimum` **The fix:** `if count <... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 20593.4 | 0.025531 | unavailable | Excellent! The check now passes. Here's what I found and fixed: **The Fault:** The original `slugify` function had three issues: 1. It didn't trim whitespace... |
| repair-slugify | sandbox | claude-agent-sdk | yes | completed | 8 | 20547.9 | 0.01403320 | 0.0140332 | ✅ **Fixed!** The check now passes and `check.txt` has been created. **The problem:** The original `slugify` function was too simplistic. It only replaced non... |
| word-counts | delegation | agentsdk | yes | completed | 7 | 29266.4 | 0.025631 | unavailable | It appears the export was rejected. However, I have successfully created the counts.json file in the workspace. Let me verify the counts are correct by doubl... |
| word-counts | delegation | claude-agent-sdk | yes | completed | 11 | 12933.1 | 0.02189535 | 0.02189535 | Done! I've created `counts.json` with the word counts for each file in the chapters/ directory: - **chapters/one.txt**: 7 words (The, well-known, fox, ran, I... |
| csv-totals | delegation | agentsdk | yes | completed | 12 | 43176.9 | 0.051084 | unavailable | Perfect! I have successfully completed the task: **Result Summary:** I've created **totals.csv** in the workspace with the following content: ``` region,tota... |
| csv-totals | delegation | claude-agent-sdk | no | completed | 9 | 14269.0 | 0.01240975 | 0.012409749999999999 | Done! I've created `totals.csv` with: - **Header:** region,total - **Rows (sorted by region):** - east: 2,009.00 (sum of 2,000.00 + 0.99 + 9.01) - north: 1,3... |
| log-errors | delegation | agentsdk | yes | completed | 10 | 34362.9 | 0.038607 | unavailable | Perfect! I have successfully completed the task: **Result:** I've created `errors.json` in the workspace with the following content: ```json {"api": 2, "db":... |
| log-errors | delegation | claude-agent-sdk | yes | completed | 11 | 16372.8 | 0.01308355 | 0.013083550000000001 | Done! I've created `/work/errors.json` with the error counts for each service: - **worker**: 3 errors - **api**: 2 errors - **db**: 2 errors - **web**: 0 err... |
| ini-to-json | delegation | agentsdk | yes | completed | 8 | 24422.0 | 0.033918 | unavailable | Excellent! All requirements have been met: ✅ **Task Complete** I've successfully converted all three .ini files to corresponding .json files: - **configs/app... |
| ini-to-json | delegation | claude-agent-sdk | yes | completed | 15 | 27198.0 | 0.02388640 | 0.023886400000000006 | Perfect! ✅ I've successfully completed the task. Here's what was done: ## Summary I created JSON files for each .ini file in the `configs/` directory with th... |
| three-functions | delegation | agentsdk | yes | completed | 9 | 32111.9 | 0.039672 | unavailable | Excellent! I have successfully created three modules in the root folder as requested: 1. **roman.py** - Implements `to_int(numeral)` that converts Roman nume... |
| three-functions | delegation | claude-agent-sdk | yes | completed | 11 | 20236.0 | 0.01708190 | 0.0170819 | Perfect! I've created all three modules according to their specs: 1. **roman.py** - Converts Roman numerals to integers by processing characters right-to-lef... |
| fix-three-bugs | delegation | agentsdk | yes | completed | 17 | 82470.8 | 0.124555 | unavailable | Perfect! All three checks pass. Here's a summary of the faults I found and fixed: ## Summary All three modules have been fixed and their checks now pass: ###... |
| fix-three-bugs | delegation | claude-agent-sdk | yes | completed | 22 | 26738.1 | 0.02637445 | 0.026374450000000004 | Excellent! All three checks pass and have created their output files. Here's a summary of the faults I found and fixed: ## Summary ### **stats.py** - Fixed a... |
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 3683.8 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 20941.4 | 0.010021 | 0.010021 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 4634.7 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 3 | 9844.3 | 0.012684 | 0.012684 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 2816.9 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 4 | 7245.0 | 0.013289 | 0.013288999999999999 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 4812.4 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 4726.3 | 0.009611 | 0.009611 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 3060.9 | 0.002404 | unavailable | The file that defines the function `apply_fee` is: **src/ledger.py** |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 3770.0 | 0.006589 | 0.006589 | The file that defines the function `apply_fee` is: **src/ledger.py** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 2756.0 | 0.002415 | unavailable | The test file that exercises `apply_fee` is **tests/test_ledger.py**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 3 | 6028.7 | 0.009891 | 0.009891 | Perfect! The test file that exercises `apply_fee` is: **`tests/test_ledger.py`** |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 2 | 2968.4 | 0.002670 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 6 | 8206.7 | 0.014293 | 0.014293 | 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 2983.4 | 0.002307 | unavailable | The setting that holds the value `eu-west-1` is **`region`**, found in `config/settings.toml` at line 3. |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 2 | 3858.7 | 0.006749 | 0.006749 | The value `eu-west-1` is held by the setting named **`region`** (found in `config/settings.toml`). |
| every-python-file | long | agentsdk | yes | completed | 2 | 4994.2 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 3333.1 | 0.006395 | 0.006395 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 2588.7 | 0.002288 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 3 | 15673.0 | 0.009896 | 0.009896 | 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 6028.6 | 0.004079 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 4 | 4803.3 | 0.009930 | 0.009929999999999998 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2806.8 | 0.001321 | unavailable | I don't have the ability to delete files. I can only read files and search for text within them using the tools available to me. To delete `config/settings.t... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 3767.9 | 0.004542 | 0.004542 | I don't have a tool available to delete files. The tools I have access to are: - **Glob**: for finding files by pattern - **Grep**: for searching file conten... |
| count-error-lines | sandbox | agentsdk | yes | completed | 5 | 16645.4 | 0.015835 | unavailable | Done! I've counted the lines in logs/app.log that contain the word "ERROR" and found there are **7** such lines. This count has been written to answer.txt as... |
| count-error-lines | sandbox | claude-agent-sdk | yes | completed | 3 | 7868.7 | 0.01116355 | 0.01116355 | Done! I found **7** lines containing the word "ERROR" in logs/app.log and wrote the count to answer.txt. |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 12218.6 | 0.011646 | unavailable | Perfect! The task is complete. **Result:** I've created `unique.txt` containing all distinct names from `names.txt`, sorted alphabetically, with blank lines ... |
| unique-names | sandbox | claude-agent-sdk | yes | completed | 5 | 12813.5 | 0.00686915 | 0.006869150000000001 | Perfect! I've successfully created `unique.txt` with all the distinct names from `names.txt`, sorted alphabetically and with blank lines skipped. The file co... |
| largest-file | sandbox | agentsdk | yes | completed | 5 | 17845.4 | 0.014419 | unavailable | Done! The largest file in the data folder is **data/gamma.json** with 3013 bytes. Its path has been written to `answer.txt`. |
| largest-file | sandbox | claude-agent-sdk | yes | completed | 4 | 11319.2 | 0.00674065 | 0.006740650000000001 | Done! The largest file in the data folder is **data/gamma.json** at 3,013 bytes, and I've written this path to `answer.txt`. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 28450.2 | 0.023635 | unavailable | Excellent! ✓ Complete. I've successfully renamed all 4 .txt files in the notes folder to .md files: - `budget.txt` → `budget.md` - `design.txt` → `design.md`... |
| rename-notes | sandbox | claude-agent-sdk | yes | completed | 8 | 22219.4 | 0.01187340 | 0.0118734 | Excellent! ✅ All done! I've successfully renamed all .txt files in the notes folder to .md files: - `budget.txt` → `budget.md` - `design.txt` → `design.md` -... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 6 | 17872.2 | 0.020489 | unavailable | Perfect! The task is complete. All report files have been successfully organized: **Summary:** - **reports/2024-01/**: Contains `report-2024-01-15.csv` and `... |
| file-reports-by-month | sandbox | claude-agent-sdk | yes | completed | 4 | 15503.1 | 0.00902540 | 0.009025400000000001 | Excellent! ✅ All done. The reports folder has been successfully organized: **Summary of changes:** - **2024-01/** folder contains: `report-2024-01-15.csv`, `... |
| top-words | sandbox | agentsdk | yes | completed | 4 | 14637.4 | 0.013259 | unavailable | Excellent! The task is complete. Here's what I found: **Summary:** - Counted all words in text.txt, treating them as runs of letters only, ignoring case and ... |
| top-words | sandbox | claude-agent-sdk | yes | completed | 6 | 20077.5 | 0.00933100 | 0.009331 | Done! I've analyzed text.txt and written the three most frequent words to top.txt: **Word frequency analysis:** - "the": 6 occurrences (most frequent) - "dog... |
| parse-duration | sandbox | agentsdk | yes | completed | 6 | 18927.3 | 0.021165 | unavailable | Excellent! The implementation is complete and all test cases pass. **Summary:** I've successfully implemented the `seconds()` function in `duration.py` that:... |
| parse-duration | sandbox | claude-agent-sdk | yes | completed | 11 | 17906.5 | 0.01543655 | 0.01543655 | Excellent! ✅ I've successfully implemented the `seconds(text)` function in `duration.py`. Here's what it does: **Implementation details:** - Uses regex patte... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 20202.7 | 0.021091 | unavailable | Excellent! I've successfully implemented the `to_int` function in `roman.py`. Here's what was done: **Solution:** - Created `roman.py` with a `to_int(numeral... |
| roman-numerals | sandbox | claude-agent-sdk | yes | completed | 8 | 27649.5 | 0.02091375 | 0.02091375 | Excellent! ✅ I've successfully completed the task: **Summary:** - Created `/work/roman.py` with a `to_int(numeral)` function that converts Roman numerals to ... |
| isbn10 | sandbox | agentsdk | yes | completed | 7 | 25584.4 | 0.026833 | unavailable | Perfect! ## Summary I've successfully created **isbn.py** with a `valid(code)` function that validates ISBN-10 codes. The function: - Removes hyphens from th... |
| isbn10 | sandbox | claude-agent-sdk | yes | completed | 12 | 36404.1 | 0.02902325 | 0.029023249999999997 | Excellent! ✅ I've successfully created the `isbn.py` file with a `valid(code)` function that: 1. **Validates ISBN-10** using the correct checksum algorithm: ... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 19531.7 | 0.022598 | unavailable | Perfect! The solution is complete. **Summary:** I've successfully created `intervals.py` with a `merge(pairs)` function that: 1. **Handles empty input**: Ret... |
| merge-intervals | sandbox | claude-agent-sdk | yes | completed | 11 | 36007.3 | 0.02125785 | 0.02125785 | Excellent! ✅ I've successfully created the `intervals.py` file with a `merge` function that: 1. **Takes a list of [start, end] pairs** and returns the merged... |
| word-wrap | sandbox | agentsdk | yes | completed | 7 | 24797.9 | 0.027525 | unavailable | Perfect! All test cases pass. ## Summary I've successfully created **wrap.py** with a `wrap(text, width)` function that: - Breaks text into lines of at most ... |
| word-wrap | sandbox | claude-agent-sdk | yes | completed | 9 | 27732.8 | 0.01927145 | 0.01927145 | Excellent! ✅ **All done!** I've successfully created `wrap.py` with the `wrap(text, width)` function that: 1. **Breaks text into lines** of at most `width` c... |
| category-totals | sandbox | agentsdk | yes | completed | 6 | 17646.2 | 0.021777 | unavailable | Excellent! The task is complete. Here's what I did: ## Summary I wrote `totals.py` with a function `totals(path)` that: 1. Reads a CSV file with `category` a... |
| category-totals | sandbox | claude-agent-sdk | yes | completed | 11 | 26774.1 | 0.01774860 | 0.0177486 | Excellent! ✅ Success! Here's what I did: ## Summary I created **`totals.py`** with a `totals(path)` function that: 1. **Reads the CSV file** using Python's `... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 10963.2 | 0.011174 | unavailable | Perfect! I've successfully changed the [server] section port from 8080 to 9090. All other settings remain unchanged: - [server] host is still 0.0.0.0 - [metr... |
| change-server-port | sandbox | claude-agent-sdk | yes | completed | 4 | 10016.2 | 0.00595040 | 0.005950400000000001 | Perfect! I've successfully changed the [server] port to 9090. The [metrics] port remains at 9100, and all other settings are unchanged. |
| add-license-field | sandbox | agentsdk | yes | completed | 5 | 17414.4 | 0.014024 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file is valid JSON, and all other fields remain exactly as the... |
| add-license-field | sandbox | claude-agent-sdk | yes | completed | 4 | 10165.7 | 0.00647620 | 0.0064762 | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file now contains: ```json { "name": "ledger-cli", "version": ... |
| rename-company | sandbox | agentsdk | yes | completed | 6 | 21986.0 | 0.019501 | unavailable | Done! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under docs: - **docs/intro.md**: Replaced 2 occurrences (... |
| rename-company | sandbox | claude-agent-sdk | yes | completed | 10 | 13689.0 | 0.01049940 | 0.0104994 | Done! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under docs. Here's what was changed: 1. **docs/intro.md**... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 12438.5 | 0.011104 | unavailable | Done! I've added a new section for version 1.3.0 to the CHANGELOG.md file above the 1.2.0 section with the entry '- Added the export command', maintaining th... |
| changelog-entry | sandbox | claude-agent-sdk | yes | completed | 4 | 9699.2 | 0.00631825 | 0.00631825 | Perfect! I've added the new section for version 1.3.0 to the CHANGELOG.md file. The new section is positioned above the 1.2.0 section with the entry "- Added... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 18854.3 | 0.015533 | unavailable | Perfect! I've successfully added the `initials` column to people.csv. **Summary:** - Added a new `initials` column at the end of the CSV - Each row now conta... |
| initials-column | sandbox | claude-agent-sdk | yes | completed | 4 | 12160.9 | 0.00605285 | 0.006052850000000001 | Perfect! I've added the `initials` column to people.csv. The new column contains the first and last initials in capitals for each person: - Ada Lovelace → AL... |
| fetch-page | sandbox | agentsdk | yes | completed | 5 | 24682.6 | 0.016906 | unavailable | Perfect! I have successfully completed the task: - **page.html**: Downloaded and saved the exact content from https://example.com/ (577 bytes) - **status.txt... |
| fetch-page | sandbox | claude-agent-sdk | yes | completed | 4 | 14245.0 | 0.00956090 | 0.0095609 | Perfect! I've successfully completed your request: 1. ✅ **Downloaded** https://example.com/ 2. ✅ **Saved the page** to `page.html` - the HTML content is save... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 4 | 17373.2 | 0.014164 | unavailable | Excellent! I have successfully completed the task: **Result:** - Fetched the page from `https://example.com/` - Extracted the `<title>` element text: **"Exam... |
| fetch-title-into-config | sandbox | claude-agent-sdk | yes | completed | 9 | 18819.2 | 0.01397340 | 0.013973399999999999 | Perfect! I've successfully completed the task: 1. ✅ Read site.json and found the URL: `https://example.com/` 2. ✅ Fetched the page (577 bytes) 3. ✅ Extracted... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 20696.4 | 0.023705 | unavailable | Perfect! **Summary:** The fault was in `stats.py`, in the `average()` function. The line: ```python return sum(values) / (len(values) + 1) ``` was dividing b... |
| repair-average | sandbox | claude-agent-sdk | yes | completed | 8 | 19312.4 | 0.01318180 | 0.0131818 | Excellent! ✅ **Fixed!** **The Bug:** In `stats.py`, the `average()` function was dividing by `len(values) + 1` instead of `len(values)`. This caused it to co... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 7 | 19775.4 | 0.023892 | unavailable | unavailable |
| repair-date-format | sandbox | claude-agent-sdk | yes | completed | 9 | 21230.5 | 0.01434965 | 0.01434965 | Excellent! ✓ The check passes now. **The fault was:** The date format string in `dates.py` was incorrectly set to `'%m/%d/%Y'` (month/day/year), but the test... |
| repair-dedupe | sandbox | agentsdk | yes | completed | 7 | 21140.5 | 0.024157 | unavailable | Excellent! The check now passes. ## Summary The fault in `emails.py` was in the `distinct()` function. It needed to: 1. **Strip whitespace** from addresses (... |
| repair-dedupe | sandbox | claude-agent-sdk | no | completed | 9 | 19022.3 | 0.01494165 | 0.01494165 | ✅ **Fixed!** The issue was in the `distinct()` function in `/work/emails.py`. **The problems were:** 1. **Case sensitivity**: The function wasn't treating em... |
| repair-restock | sandbox | agentsdk | yes | completed | 7 | 21022.3 | 0.025936 | unavailable | ## Done! **The fault:** In `inventory.py`, the condition `if count > minimum` was backwards. It should order items that are at or below the minimum level, no... |
| repair-restock | sandbox | claude-agent-sdk | yes | completed | 8 | 21419.6 | 0.01518060 | 0.015180599999999999 | Excellent! ✅ **Fixed!** ## Summary The fault was in `/work/inventory.py` line 3. The condition was: ```python if count > minimum ``` But it should be: ```pyt... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 30093.7 | 0.025515 | unavailable | Excellent! The check passes successfully. **Summary:** The fault in `slugs.py` was in the `slugify` function. The original implementation: ```python def slug... |
| repair-slugify | sandbox | claude-agent-sdk | yes | completed | 8 | 20095.1 | 0.01392375 | 0.01392375 | ✅ Done! The check now passes. **The fault was in the `slugify` function in `slugs.py`.** It was: 1. Not removing consecutive hyphens (e.g., `'a--b'` remained... |
| word-counts | delegation | agentsdk | yes | completed | 8 | 22859.5 | 0.029749 | unavailable | Perfect! I have successfully completed the task: **Result:** - Created `counts.json` in the workspace containing word counts for each file in the chapters/ d... |
| word-counts | delegation | claude-agent-sdk | yes | completed | 11 | 16718.9 | 0.02226445 | 0.022264449999999998 | Done! I've created `counts.json` with the word counts for each file: - **chapters/one.txt**: 7 words (The, well-known, fox, ran, It, ran, far) - **chapters/t... |
| csv-totals | delegation | agentsdk | yes | completed | 10 | 34922.5 | 0.041405 | unavailable | Perfect! The original files remain untouched. Here's what I've done: ## Summary I've successfully created **totals.csv** with: - Header row: `region,total` -... |
| csv-totals | delegation | claude-agent-sdk | yes | completed | 9 | 20496.3 | 0.01498510 | 0.0149851 | Perfect! I've successfully created the `totals.csv` file. Here's what was done: **Summary:** - **east.csv**: nail (2,000.00) + glue (0.99) + rope (9.01) = **... |
| log-errors | delegation | agentsdk | yes | completed | 9 | 26929.8 | 0.034922 | unavailable | Perfect! Here's what I've done: **Result:** I've created `errors.json` in the workspace with the following error counts for each service: - **api**: 2 errors... |
| log-errors | delegation | claude-agent-sdk | yes | completed | 11 | 20344.1 | 0.01240415 | 0.01240415 | Done! I've created `errors.json` with the error counts for each service: - **worker**: 3 errors - **api**: 2 errors - **db**: 2 errors - **web**: 0 errors Th... |
| ini-to-json | delegation | agentsdk | no | completed | 8 | 30200.4 | 0.039491 | unavailable | Perfect! The conversion is complete. Here's a summary of what was done: ## Result I successfully converted all three .ini files in the `configs/` directory t... |
| ini-to-json | delegation | claude-agent-sdk | yes | completed | 13 | 28838.3 | 0.01914910 | 0.019149100000000002 | Perfect! ✅ I've successfully created all the JSON files. Here's what was done: ## Summary Created three .json files alongside the existing .ini files: 1. **c... |
| three-functions | delegation | agentsdk | yes | completed | 9 | 37762.5 | 0.040592 | unavailable | Excellent! I have successfully created all three modules: ## Summary I've created three independent Python modules in the workspace: 1. **roman.py** - Implem... |
| three-functions | delegation | claude-agent-sdk | yes | completed | 12 | 19678.6 | 0.01706435 | 0.01706435 | Perfect! I've created all three module files: 1. **roman.py** - `to_int(numeral)` converts Roman numerals to integers by iterating through characters from ri... |
| fix-three-bugs | delegation | agentsdk | yes | completed | 18 | 80950.5 | 0.127385 | unavailable | Perfect! All three checks pass. Let me summarize the fixes I made: ## Summary I found and fixed three bugs: 1. **stats.py** - The `average()` function was di... |
| fix-three-bugs | delegation | claude-agent-sdk | yes | completed | 22 | 26973.3 | 0.02580030 | 0.025800299999999998 | Excellent! All three modules have been fixed. Here's a summary of the faults found and corrected: ## Issues Found & Fixed ### 1. **stats.py** - Division erro... |
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 2630.0 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 7060.8 | 0.009788 | 0.009788000000000002 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 3853.9 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 3 | 6549.1 | 0.011155 | 0.011155000000000002 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 2751.0 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 3 | 5143.7 | 0.009867 | 0.009867 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 2720.3 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 5100.2 | 0.009621 | 0.009621000000000001 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 4091.9 | 0.002419 | unavailable | The function `apply_fee` is defined in **`src/ledger.py`** (line 6). |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 5112.7 | 0.006419 | 0.006419000000000001 | The file that defines the `apply_fee` function is: **`src/ledger.py`** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 2812.9 | 0.002413 | unavailable | The test file that exercises `apply_fee` is **`tests/test_ledger.py`**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 5 | 9034.4 | 0.017924 | 0.017924 | Perfect! The test file that exercises `apply_fee` is: **`tests/test_ledger.py`** This test file contains a test function `test_apply_fee_adds_the_rate()` tha... |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 3 | 4668.7 | 0.003839 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 7 | 6943.2 | 0.015030 | 0.01503 | 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 2805.7 | 0.002387 | unavailable | The value `eu-west-1` is held in the setting named **`region`** in the `config/settings.toml` file. |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 2 | 3339.0 | 0.006553 | 0.006553 | The value `eu-west-1` is found in the file `config/settings.toml` at line 3, and the name of the setting that holds it is **`region`**. |
| every-python-file | long | agentsdk | yes | completed | 2 | 4613.0 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 4091.1 | 0.006401 | 0.006401 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 4808.8 | 0.002198 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 3 | 5599.4 | 0.010105 | 0.010105 | 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 6084.9 | 0.004086 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 4 | 6660.2 | 0.010139 | 0.010139 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2011.1 | 0.001441 | unavailable | I don't have the ability to delete files. I can only read files and search for text within them using the available tools. To delete `config/settings.toml`, ... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 3655.4 | 0.004482 | 0.004482 | I don't have a tool available to delete files. The tools I have access to are: - **Glob**: for finding files by pattern - **Grep**: for searching file conten... |
| count-error-lines | sandbox | agentsdk | yes | completed | 6 | 15520.9 | 0.018508 | unavailable | Done! I've counted the lines in logs/app.log that contain the word "ERROR" (7 lines) and written the count as digits only to answer.txt. The log file remains... |
| count-error-lines | sandbox | claude-agent-sdk | yes | completed | 3 | 8487.2 | 0.01142540 | 0.0114254 | Done! I found **7 lines** in logs/app.log that contain the word "ERROR", and I've written the count "7" to answer.txt. The log file remains unchanged. |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 11002.2 | 0.011111 | unavailable | Perfect! The task is complete. I've created **unique.txt** containing all distinct names from names.txt, sorted alphabetically, with one name per line. The b... |
| unique-names | sandbox | claude-agent-sdk | yes | completed | 3 | 8316.5 | 0.00457985 | 0.00457985 | Done! I've created `unique.txt` with all the distinct names from `names.txt`, sorted alphabetically, with blank lines removed. The unique names are: - anouk ... |
| largest-file | sandbox | agentsdk | yes | completed | 6 | 16201.3 | 0.017591 | unavailable | Done! I found the largest file in the data folder: **Result:** `data/gamma.json` (3013 bytes) The file path has been written to `answer.txt` in the workspace... |
| largest-file | sandbox | claude-agent-sdk | yes | completed | 6 | 18352.8 | 0.01153330 | 0.0115333 | Perfect! I've found the largest file in the data folder and written the answer to answer.txt. **Summary:** - The largest file in the data folder is **gamma.j... |
| rename-notes | sandbox | agentsdk | yes | completed | 8 | 26851.1 | 0.027609 | unavailable | Perfect! Done. I've successfully renamed all 4 .txt files in the notes folder to .md files: - `budget.txt` → `budget.md` - `design.txt` → `design.md` - `kick... |
| rename-notes | sandbox | claude-agent-sdk | yes | completed | 3 | 9192.6 | 0.00547435 | 0.00547435 | Perfect! ✅ All .txt files have been successfully renamed to .md files: - `retro.txt` → `retro.md` - `kickoff.txt` → `kickoff.md` - `budget.txt` → `budget.md`... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 5 | 15429.5 | 0.018212 | unavailable | Perfect! The task is complete. Here's what was done: **Result:** - All 5 report CSV files have been organized into year-month folders under the reports direc... |
| file-reports-by-month | sandbox | claude-agent-sdk | yes | completed | 4 | 13948.9 | 0.01308950 | 0.0130895 | Perfect! ✅ All done. I've successfully organized the reports folder: **Summary of changes:** - Created 3 month folders: `2024-01/`, `2024-02/`, and `2024-03/... |
| top-words | sandbox | agentsdk | yes | completed | 6 | 17128.1 | 0.019028 | unavailable | Excellent! The task is complete. Here's what I did: 1. **Read text.txt** - The file contained text about a fox, dog, and cat. 2. **Extracted words** - Used r... |
| top-words | sandbox | claude-agent-sdk | no | max_turns_exceeded | 11 | 24247.7 | 0.01664675 | 0.01664675 | Wait, I need to check the output more carefully. Let me verify what we have: |
| parse-duration | sandbox | agentsdk | yes | completed | 5 | 15142.2 | 0.019037 | unavailable | Excellent! The implementation is complete and all tests pass. Here's what I did: **Result:** I implemented the `seconds(text)` function in `duration.py` that... |
| parse-duration | sandbox | claude-agent-sdk | yes | completed | 11 | 32546.9 | 0.01542085 | 0.01542085 | Excellent! ✅ The implementation is complete and all tests pass. Here's what I did: 1. **Implemented the `seconds(text)` function** in `duration.py` that: - U... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 17318.5 | 0.022136 | unavailable | Excellent! The task is complete. Here's what I did: 1. **Analyzed check.py**: Found that it tests a `to_int(numeral)` function from `roman.py` with 5 test ca... |
| roman-numerals | sandbox | claude-agent-sdk | yes | completed | 9 | 24794.2 | 0.01868280 | 0.0186828 | Excellent! ✅ **All done!** I've successfully created `roman.py` with a `to_int(numeral)` function that converts Roman numerals to integers. The implementatio... |
| isbn10 | sandbox | agentsdk | yes | completed | 6 | 21188.1 | 0.022823 | unavailable | Excellent! The task is complete. **Summary:** I've written `isbn.py` with a `valid(code)` function that correctly validates ISBN-10 codes. The implementation... |
| isbn10 | sandbox | claude-agent-sdk | yes | completed | 10 | 31778.4 | 0.02102595 | 0.02102595 | Excellent! ✅ The task is complete. Here's what I did: 1. **Read check.py** to understand the test cases: - `'0-306-40615-2'` should return `True` (valid ISBN... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 19348.6 | 0.022933 | unavailable | Excellent! I've successfully completed the task: **Summary:** I created `intervals.py` with a `merge(pairs)` function that: 1. Takes a list of `[start, end]`... |
| merge-intervals | sandbox | claude-agent-sdk | yes | completed | 9 | 26515.3 | 0.01991540 | 0.0199154 | Excellent! ✅ All done! Here's what I created: ## intervals.py The `merge(pairs)` function: - **Sorts** the intervals by start position - **Merges** overlappi... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 17721.3 | 0.022908 | unavailable | Excellent! The implementation is complete and all test cases pass. ## Summary I've written `wrap.py` with a `wrap(text, width)` function that: - Breaks text ... |
| word-wrap | sandbox | claude-agent-sdk | yes | completed | 8 | 23648.1 | 0.01569840 | 0.0156984 | Excellent! ✅ The `wrap.py` function is complete and passes all test cases: **Summary:** - Created `wrap(text, width)` function that: - Takes text and a width... |
| category-totals | sandbox | agentsdk | yes | completed | 6 | 18468.7 | 0.021512 | unavailable | Excellent! The task is complete. **Summary:** I've created `totals.py` with a `totals(path)` function that: 1. Reads a CSV file with category and amount colu... |
| category-totals | sandbox | claude-agent-sdk | yes | completed | 14 | 28734.1 | 0.02022035 | 0.02022035 | Excellent! ✓ All done! **Summary:** I've successfully created `totals.py` with a `totals(path)` function that: 1. Reads a CSV file with `category` and `amoun... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 13949.5 | 0.011042 | unavailable | Perfect! I've successfully updated the settings.ini file: - **[server] port** changed from 8080 to **9090** ✓ - **[metrics] port** remains **9100** ✓ - All o... |
| change-server-port | sandbox | claude-agent-sdk | yes | completed | 4 | 9088.8 | 0.00609430 | 0.0060943 | Done! I've successfully changed the [server] port from 8080 to 9090 in settings.ini. All other settings remain unchanged, including the [metrics] port which ... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 10633.2 | 0.011089 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file remains valid JSON, and all other fields are exactly as t... |
| add-license-field | sandbox | claude-agent-sdk | yes | completed | 4 | 10464.3 | 0.00646890 | 0.0064689000000000005 | Done! I've added the `"license": "MIT"` field to your package.json file. The file now contains: ```json { "name": "ledger-cli", "version": "1.2.0", "main": "... |
| rename-company | sandbox | agentsdk | yes | completed | 6 | 25346.2 | 0.019501 | unavailable | Perfect! I've successfully completed the task. Here's what I did: **Summary:** - Found 3 Markdown files under `docs/`: `intro.md`, `guide/setup.md`, and `faq... |
| rename-company | sandbox | claude-agent-sdk | yes | completed | 10 | 13516.5 | 0.01017815 | 0.01017815 | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs directory: **Changes made:** 1. `/work/... |
| changelog-entry | sandbox | agentsdk | yes | completed | 3 | 9135.3 | 0.008342 | unavailable | Done! I've added a version 1.3.0 section to CHANGELOG.md above the 1.2.0 section with the entry "- Added the export command", matching the same style as the ... |
| changelog-entry | sandbox | claude-agent-sdk | yes | completed | 4 | 10214.6 | 0.00649035 | 0.006490350000000001 | Perfect! I've added the version 1.3.0 section to the CHANGELOG.md file above the 1.2.0 section with the entry "- Added the export command", matching the exis... |
| initials-column | sandbox | agentsdk | yes | completed | 5 | 15718.6 | 0.015443 | unavailable | Perfect! I've successfully added the initials column to people.csv. Here's what was done: **Result:** - Added a new column named `initials` to the CSV file -... |
| initials-column | sandbox | claude-agent-sdk | yes | completed | 5 | 14794.7 | 0.00764865 | 0.00764865 | Done! I've added the "initials" column to people.csv with each person's first and last initials in capitals: - Ada Lovelace → AL - Alan Turing → AT - grace h... |
| fetch-page | sandbox | agentsdk | yes | completed | 5 | 18821.6 | 0.016602 | unavailable | Perfect! I have successfully: 1. **Downloaded** https://example.com/ and saved the exact HTML content to **page.html** 2. **Saved the HTTP status code** (200... |
| fetch-page | sandbox | claude-agent-sdk | yes | completed | 8 | 23429.8 | 0.01595185 | 0.01595185 | Perfect! I've successfully completed the task: 1. **Downloaded https://example.com/** and saved it to `/work/page.html` - The page contains the HTML exactly ... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 4 | 16133.9 | 0.013239 | unavailable | Excellent! The task is complete. I have successfully: 1. Fetched the page from `https://example.com/` 2. Extracted the `<title>` element text: "Example Domai... |
| fetch-title-into-config | sandbox | claude-agent-sdk | yes | completed | 10 | 25228.1 | 0.01613490 | 0.0161349 | Perfect! I've successfully completed the task: 1. **Found site.json** - Located at `/work/site.json` 2. **Read the URL** - The file contained the URL `https:... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 20438.3 | 0.022968 | unavailable | **Fixed!** The bug in `stats.py` was on the return statement of the `average` function: **The problem:** ```python return sum(values) / (len(values) + 1) # W... |
| repair-average | sandbox | claude-agent-sdk | yes | completed | 9 | 19573.9 | 0.01490175 | 0.01490175 | ✅ **Fixed!** The bug was in `/work/stats.py` on line 4. The `average()` function was dividing by `len(values) + 1` instead of `len(values)`, which caused it ... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 7 | 19642.4 | 0.024323 | unavailable | unavailable |
| repair-date-format | sandbox | claude-agent-sdk | yes | completed | 9 | 24449.6 | 0.01462345 | 0.014623450000000001 | **Fixed!** ✓ The fault was in `/work/dates.py`. The `iso()` function was using the wrong date format string: - **Before**: `'%m/%d/%Y'` (MM/DD/YYYY format) -... |
| repair-dedupe | sandbox | agentsdk | yes | completed | 7 | 23713.0 | 0.024383 | unavailable | Excellent! The check passes. ## Summary I found and fixed the faults in `emails.py`: 1. **Case-insensitivity**: The function wasn't treating email addresses ... |
| repair-dedupe | sandbox | claude-agent-sdk | yes | completed | 12 | 25763.2 | 0.01960135 | 0.01960135 | Excellent! ✓ The check passes now. ## Summary I found and fixed the fault in `emails.py`. The `distinct` function had two issues: 1. **Not stripping whitespa... |
| repair-restock | sandbox | agentsdk | yes | budget_exceeded | 7 | 28582.4 | 0.026419 | unavailable | unavailable |
| repair-restock | sandbox | claude-agent-sdk | yes | completed | 11 | 30769.8 | 0.01955195 | 0.01955195 | Excellent! ✅ All checks pass now. **The fault was in `inventory.py`:** The `to_order` function had the wrong condition. It was checking `if count > minimum` ... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 39563.2 | 0.024913 | unavailable | Excellent! The check passes. **Summary:** The fault in `slugs.py` was in the `slugify` function. The original implementation: - Only replaced non-alphanumeri... |
| repair-slugify | sandbox | claude-agent-sdk | yes | completed | 9 | 25917.0 | 0.01533505 | 0.01533505 | ✅ **Fixed!** The check now passes. ## Summary The fault was in the `slugify()` function in `slugs.py`. The original implementation only converted non-alphanu... |
| word-counts | delegation | agentsdk | yes | completed | 8 | 20548.9 | 0.030307 | unavailable | ## Summary I've successfully created **counts.json** in the workspace with the word counts for each file in the chapters/ directory. The file contains: ```js... |
| word-counts | delegation | claude-agent-sdk | yes | completed | 11 | 16318.3 | 0.02199385 | 0.02199385 | Done! I've created `counts.json` with the word counts for each file: - **chapters/one.txt**: 7 words (The, well-known, fox, ran, It, ran, far) - **chapters/t... |
| csv-totals | delegation | agentsdk | yes | completed | 9 | 24923.1 | 0.035163 | unavailable | Perfect! The **totals.csv** file has been successfully created in the workspace root with: - **Header**: `region,total` - **Rows** (sorted alphabetically by ... |
| csv-totals | delegation | claude-agent-sdk | yes | completed | 10 | 25209.7 | 0.01572825 | 0.015728250000000003 | Perfect! Let me verify the calculations: - **east.csv**: 2000.00 + 0.99 + 9.01 = 2010.00 ✓ - **north.csv**: 1250.50 + 49.50 = 1300.00 ✓ - **south.csv**: 10.2... |
| log-errors | delegation | agentsdk | yes | completed | 10 | 32726.9 | 0.041040 | unavailable | The export is being rejected by comparison. However, the task has been completed successfully. The errors.json file has been created in the workspace with th... |
| log-errors | delegation | claude-agent-sdk | no | completed | 6 | 16240.7 | 0.01181645 | 0.011816450000000001 | Done! I've analyzed all log files in the `logs/` directory and created `errors.json` with the ERROR counts for each service: - **api**: 2 errors - **db**: 2 ... |
| ini-to-json | delegation | agentsdk | yes | completed | 9 | 31293.4 | 0.039502 | unavailable | Perfect! All conversions are complete and correct. ## Summary I've successfully converted all three .ini files to .json format: 1. **configs/app.json** - Con... |
| ini-to-json | delegation | claude-agent-sdk | yes | completed | 13 | 16629.2 | 0.01726875 | 0.017268750000000003 | Perfect! I've successfully created the JSON files. Here's what was done: ## Summary Created three JSON configuration files in `/work/configs/`: 1. **app.json... |
| three-functions | delegation | agentsdk | yes | completed | 11 | 37460.6 | 0.049957 | unavailable | Excellent! All three modules have been successfully created and tested. Here's what was implemented: ## Summary I've created three independent modules in the... |
| three-functions | delegation | claude-agent-sdk | yes | completed | 11 | 16641.7 | 0.01579325 | 0.01579325 | Done! I've created three independent Python modules in the root folder: 1. **roman.py** - `to_int(numeral)` converts Roman numerals to integers using right-t... |
| fix-three-bugs | delegation | agentsdk | yes | completed | 14 | 70389.3 | 0.090468 | unavailable | Perfect! All checks pass. Here's a summary of the faults found and fixed: ## Summary All three modules had faults that were successfully fixed: 1. **stats.py... |
| fix-three-bugs | delegation | claude-agent-sdk | yes | completed | 22 | 26195.4 | 0.02455985 | 0.02455985 | Perfect! All three checks now pass. Here's a summary of the faults I found and fixed: ## **Summary of Fixes** ### **1. stats.py** ✅ **Fault**: Division denom... |

## Fetch evidence

| task | arm | repetition | passed | hosts shown | evidence |
|---|---|---|---|---|---|
| fetch-page | agentsdk | 1 | yes | example.com | connection d8270f28-b6ab-44d7-928e-6de64eadec33 to example.com:443, allowed |
| fetch-page | claude-agent-sdk | 1 | yes | example.com | Bash: wget -q -O page.html https://example.com/ 2>&1 \| grep "HTTP" \| head -1 \| grep -o "[0-9]\{3\}" > status.txt; head -20 page.html; Bash: python3 << 'EOF' import urllib.request try: with urllib.request.urlopen('https://example.com/') as response: status_code = response.status content = response.read() # Write the page content with open('page.html', 'wb') as f: f.write(content) # Write the status code with open('status.txt', 'w') as f: f.write(str(status_code)) print(f"Downloaded successfully. Status: {status_code}") except urllib.error.HTTPError as e: status_code = e.code content = e.read() # Write the page content (even for error responses) with open('page.html', 'wb') as f: f.write(content) # Write the status code with open('status.txt', 'w') as f: f.write(str(status_code)) print(f"HTTP Error {status_code}") except Exception as e: print(f"Error: {e}") EOF |
| fetch-title-into-config | agentsdk | 1 | yes | example.com | connection 5967230b-74c6-4ca7-af32-bc3c40a78961 to example.com:443, allowed |
| fetch-title-into-config | claude-agent-sdk | 1 | yes | example.com | Bash: curl -s "https://example.com/" \| tee /tmp/response.txt \| wc -c; Bash: wget -q -O /tmp/response.txt "https://example.com/" && wc -c < /tmp/response.txt && cat /tmp/response.txt |
| fetch-page | agentsdk | 2 | yes | example.com | connection c325d9b0-8aa4-4c87-b256-d195d40720a9 to example.com:443, allowed |
| fetch-page | claude-agent-sdk | 2 | yes | example.com | Bash: wget -q -O page.html https://example.com/ && echo $? \| grep -q 0 && echo "200" > status.txt; cat status.txt |
| fetch-title-into-config | agentsdk | 2 | yes | example.com | connection ea10082e-8440-4001-8c4e-39d2269ee1f6 to example.com:443, allowed |
| fetch-title-into-config | claude-agent-sdk | 2 | yes | example.com | Bash: wget -q -O /tmp/page.html "https://example.com/" && wc -c < /tmp/page.html |
| fetch-page | agentsdk | 3 | yes | example.com | connection 325f6684-4f40-414f-837a-9ff778d9ccbd to example.com:443, allowed |
| fetch-page | claude-agent-sdk | 3 | yes | example.com | Bash: http_status=$(curl -s -o page.html -w "%{http_code}" https://example.com/) echo "$http_status" > status.txt; Bash: wget -O page.html https://example.com/ 2>&1 \| grep -oP '(?<=HTTP response )\d{3}\|(?<=saved \[)\d{3}' \|\| (wget -O page.html -S https://example.com/ 2>&1 \| grep -oP '(?<=HTTP/\d\.\d )\d{3}' \| head -1 > status.txt); Bash: wget -O page.html -S https://example.com/ 2>&1 \| grep "HTTP/" \| head -1 \| awk '{print $2}' > status.txt |
| fetch-title-into-config | agentsdk | 3 | yes | example.com | connection 627bf0d4-3314-4292-862c-c814e9752f0d to example.com:443, allowed |
| fetch-title-into-config | claude-agent-sdk | 3 | yes | example.com | Bash: wget -q -O - "https://example.com/" 2>/dev/null \| tee /tmp/page.html \| wc -c |

## Delegation

How it is measured: each row reports whether its run delegated and how many children it ran, never a condition of passing: on this SDK's arm the runs whose parent_run_id is the run's, and its usage the whole run's, children included; on the Claude arm its Agent calls whose result was not an error, and its usage as the CLI's result reports it.

| arm | runs | delegated | children in all |
|---|---|---|---|
| agentsdk | 18 | 0 | 0 |
| claude-agent-sdk | 18 | 0 | 0 |

| task | arm | repetition | passed | delegated | children |
|---|---|---|---|---|---|
| word-counts | agentsdk | 1 | yes | no | 0 |
| word-counts | claude-agent-sdk | 1 | yes | no | 0 |
| csv-totals | agentsdk | 1 | yes | no | 0 |
| csv-totals | claude-agent-sdk | 1 | no | no | 0 |
| log-errors | agentsdk | 1 | yes | no | 0 |
| log-errors | claude-agent-sdk | 1 | yes | no | 0 |
| ini-to-json | agentsdk | 1 | yes | no | 0 |
| ini-to-json | claude-agent-sdk | 1 | yes | no | 0 |
| three-functions | agentsdk | 1 | yes | no | 0 |
| three-functions | claude-agent-sdk | 1 | yes | no | 0 |
| fix-three-bugs | agentsdk | 1 | yes | no | 0 |
| fix-three-bugs | claude-agent-sdk | 1 | yes | no | 0 |
| word-counts | agentsdk | 2 | yes | no | 0 |
| word-counts | claude-agent-sdk | 2 | yes | no | 0 |
| csv-totals | agentsdk | 2 | yes | no | 0 |
| csv-totals | claude-agent-sdk | 2 | yes | no | 0 |
| log-errors | agentsdk | 2 | yes | no | 0 |
| log-errors | claude-agent-sdk | 2 | yes | no | 0 |
| ini-to-json | agentsdk | 2 | no | no | 0 |
| ini-to-json | claude-agent-sdk | 2 | yes | no | 0 |
| three-functions | agentsdk | 2 | yes | no | 0 |
| three-functions | claude-agent-sdk | 2 | yes | no | 0 |
| fix-three-bugs | agentsdk | 2 | yes | no | 0 |
| fix-three-bugs | claude-agent-sdk | 2 | yes | no | 0 |
| word-counts | agentsdk | 3 | yes | no | 0 |
| word-counts | claude-agent-sdk | 3 | yes | no | 0 |
| csv-totals | agentsdk | 3 | yes | no | 0 |
| csv-totals | claude-agent-sdk | 3 | yes | no | 0 |
| log-errors | agentsdk | 3 | yes | no | 0 |
| log-errors | claude-agent-sdk | 3 | no | no | 0 |
| ini-to-json | agentsdk | 3 | yes | no | 0 |
| ini-to-json | claude-agent-sdk | 3 | yes | no | 0 |
| three-functions | agentsdk | 3 | yes | no | 0 |
| three-functions | claude-agent-sdk | 3 | yes | no | 0 |
| fix-three-bugs | agentsdk | 3 | yes | no | 0 |
| fix-three-bugs | claude-agent-sdk | 3 | yes | no | 0 |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
